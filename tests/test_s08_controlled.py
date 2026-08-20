"""S08 domain and integration contract tests (I/M/F/R evidence classes).

The seams under test are the PolicyGovernanceService command surface, the
durable policy worker, the Registry artifacts and the Governance Ledger over
the public SQLite adapter.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
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
    PolicyNotFound,
    PolicyUnavailable,
    PolicyGovernanceService,
    PolicyInvalidTransition,
    PolicyPrincipal,
    S08_SCOPE,
    SOURCE_BUNDLE_ID,
    canonical_bytes,
    content_digest,
)
from task4_consistency.controlled.s09_impact import DEPENDENCY_INDEX_VERSION

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
    # S09 approval contract: every approval binds an immutable impact
    # preview and envelope, so S08-only tests wire the read-only Lifecycle
    # snapshot seam to an empty complete universe (no S09 wiring here).
    service._lifecycle_snapshot_provider = _empty_impact_snapshot
    bootstrap = service.bootstrap_once()
    if expect_bootstrap:
        assert bootstrap["status"] == "activated", bootstrap
    return service, rules_path, kb_path


def _empty_impact_snapshot(
    owner: Any, final_impact_digest: str | None = None
) -> dict[str, Any]:
    """The S08-test impact snapshot: a complete but empty application
    universe, so the S09 preview/envelope/final-impact pipeline stays
    well-formed without any S01 wiring."""
    return {
        "scope": S08_SCOPE,
        "complete": True,
        "lifecycle_watermark": 0,
        "dependency_index_digest": "0" * 64,
        "dependency_index_version": DEPENDENCY_INDEX_VERSION,
        "applications": [],
        "universe": {
            "complete": True,
            "count": 0,
            "digest": hashlib.sha256(b"[]").hexdigest(),
        },
    }


def preview_and_approve(
    service: PolicyGovernanceService,
    *,
    candidate_id: str,
    activation_time: int,
    recovery_release_id: str,
    idempotency_key: str,
    approver: PolicyPrincipal = APPROVER,
) -> dict[str, Any]:
    """Preview the conservative impact of a candidate and bind its exact
    digest in the approval (the S09 approval contract)."""
    if service._lifecycle_snapshot_provider is None:
        service._lifecycle_snapshot_provider = _empty_impact_snapshot
    preview = service.preview_impact(
        principal=ADMIN,
        candidate_id=candidate_id,
        idempotency_key=f"pv-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    return service.approve(
        principal=approver,
        candidate_id=candidate_id,
        activation_time=activation_time,
        recovery_release_id=recovery_release_id,
        preview_manifest_id=preview["manifest_id"],
        idempotency_key=idempotency_key,
        expected_governance_revision=governance_revision(service),
    )


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


def test_graph_same_as_projection_is_frozen_with_explicit_alias_priority() -> None:
    """S08 imports graph same_as edges into the executable release, while
    explicit alias tables remain the authoritative override."""
    from task4_consistency.controlled.s01_checker import TargetRelease

    graph = {
        "nodes": [
            {"id": "addr:explicit", "label": "显式全称"},
            {"id": "addr:explicit-target", "label": "图目标"},
            {"id": "addr:graph-only", "label": "图别名"},
            {"id": "addr:graph-target", "label": "图标准名"},
        ],
        "edges": [
            {"src": "addr:explicit", "rel": "same_as", "dst": "addr:explicit-target"},
            {"src": "addr:graph-only", "rel": "same_as", "dst": "addr:graph-target"},
        ],
    }
    knowledge = {
        "address_aliases": {"显式全称": "显式标准名"},
        "org_aliases": {},
        "plate_prefixes": {},
        "graph": graph,
    }
    release = TargetRelease.compile(
        load_rules(DEFAULT_RULES), "f" * 64, knowledge=knowledge
    )
    sections = dict(release.knowledge)
    aliases = dict(sections["address_aliases"])
    assert aliases["图别名"] == "图标准名"
    assert aliases["显式全称"] == "显式标准名"


def test_unsupported_graph_semantics_are_recorded_and_block_validation(
    tmp_path: Path,
) -> None:
    """Unknown nested fields, conflicting aliases and unrecognized graph
    relations are never hidden by projection or a passing corpus differential."""
    service, _, kb_path = make_policy_service(tmp_path)
    knowledge = json.loads(kb_path.read_bytes())
    knowledge["graph"]["nodes"][0]["runtime_magic"] = "hidden"
    knowledge["graph"]["edges"][0]["runtime_magic"] = "hidden"
    conflict_index = len(knowledge["graph"]["edges"])
    knowledge["graph"]["edges"].append(
        {
            "src": "addr:gxq_full",
            "rel": "same_as",
            "dst": "addr:nanjing",
        }
    )
    unknown_index = len(knowledge["graph"]["edges"])
    knowledge["graph"]["edges"].append(
        {
            "src": "addr:gaoxin",
            "rel": "runtime_magic",
            "dst": "addr:nanjing",
        }
    )
    kb_path.write_text(json.dumps(knowledge, ensure_ascii=False), encoding="utf-8")

    draft_id = import_draft(service)
    draft = next(
        item
        for item in service.query_drafts(ADMIN)["drafts"]
        if item["draft_id"] == draft_id
    )
    ledger = ledger_content(service, draft["mapping_ledger_id"])
    by_pointer = {item["source_pointer"]: item for item in ledger["items"]}
    for pointer in (
        "/graph/nodes/0",
        "/graph/edges/0",
        f"/graph/edges/{conflict_index}",
        f"/graph/edges/{unknown_index}",
    ):
        assert by_pointer[pointer]["classification"] == "unsupported"
        assert by_pointer[pointer]["target_ref"] is None

    _, worker = freeze_and_validate(service, draft_id)
    assert worker["outcome"] == "rejected"
    assert service.query_active(ADMIN)["active_generation"] == 1


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
    source_locations = [
        (item["source_ref"], item["source_pointer"]) for item in ledger["items"]
    ]
    assert {source_ref for source_ref, _ in source_locations} == {
        "rules",
        "knowledge",
    }
    assert len(source_locations) == len(set(source_locations))
    for item in ledger["items"]:
        assert item["source_pointer"]
        assert item["importer_version"] == "s08-importer/1"
        assert item["result_digest"] and len(item["result_digest"]) == 64

    rules_data = yaml.safe_load(rules_path.read_bytes())
    rule_ids = {str(rule["id"]) for rule in rules_data["rules"]}
    ledger_rule_targets = {
        item["target_ref"].rsplit("/", 1)[-1].split(".", 1)[0]
        for item in ledger["items"]
        if item["target_ref"] and item["target_ref"].startswith("checker.rules/")
    }
    assert ledger_rule_targets == rule_ids
    # Every rule field is traversed at its JSON pointer and bound to its
    # compiled target value.
    field_targets = {
        item["target_ref"].rsplit("/", 1)[-1]
        for item in ledger["items"]
        if item["target_ref"]
        and item["target_ref"].startswith("checker.rules/")
        and "." in item["target_ref"].rsplit("/", 1)[-1]
    }
    for index, rule in enumerate(rules_data["rules"]):
        for field in rule:
            assert f"{rule['id']}.{field}" in field_targets, (
                index,
                field,
            )
        field_pointers = [
            item["source_pointer"]
            for item in ledger["items"]
            if item["source_pointer"].startswith(f"/rules/{index}/")
        ]
        assert field_pointers == sorted(field_pointers)
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
    from task4_consistency.kb.store import project_graph_to_aliases

    graph_edges = [
        item
        for item in ledger["items"]
        if item["source_pointer"].startswith("/graph/edges/")
    ]
    assert [item["classification"] for item in graph_edges] == [
        "explicit_transform",
        "non_runtime_excluded",
        "explicit_transform",
        "non_runtime_excluded",
        "non_runtime_excluded",
    ]
    assert all(item["classification"] != "unsupported" for item in graph_edges)
    assert project_graph_to_aliases(kb_data["graph"]) == {
        "address_aliases": {"高新技术产业开发区": "高新区"},
        "org_aliases": {"中国人民财产保险股份有限公司": "人保财险"},
    }
    # 1b. Digests bind the resolved values, never the pointer text: mutating
    #     a rule's content or an option's value changes the ledger digest.
    first_rule_item = next(
        item for item in ledger["items"] if item["source_pointer"] == "/rules/0"
    )
    assert first_rule_item["source_digest"] == content_digest(
        ("rule_source", 0, rules_data["rules"][0])
    )
    assert first_rule_item["result_digest"] == content_digest(
        ("rule_compiled", str(rules_data["rules"][0]["id"]), rules_data["rules"][0])
    )
    version_item = next(
        item
        for item in ledger["items"]
        if item["source_ref"] == "rules" and item["source_pointer"] == "/version"
    )
    assert version_item["source_digest"] == content_digest(
        ("option", "/version", rules_data["version"])
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
    assert outcomes["runtime_behavior_identity"] == "pass"
    assert outcomes["determinism"] == "pass"
    assert outcomes["corpus_zero_diff"] == "pass"

    # 3. Determinism: two fresh-process runs over the frozen corpus agree.
    determinism = bundle["results"]["determinism"]
    assert determinism["runs"] == 2
    assert determinism["equal"] is True
    assert len(determinism["digest"]) == 64

    # 4. Zero behavior difference against the bootstrap anchor: applicable
    #    checks, selection, normalization, findings and derived route are
    #    identical over the whole frozen corpus, and the digest-bound
    #    server-owned corpus manifest is fixed in the bundle inputs.
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
    corpus_manifest = bundle["inputs"]["corpus"]
    assert corpus_manifest["track"] == "C-DEV-REG"
    assert corpus_manifest["count"] >= 1
    assert len(corpus_manifest["digest"]) == 64
    assert corpus_manifest["digest"] == corpus_diff["corpus_digest"]
    assert corpus_manifest["count"] == len(corpus_manifest["items"])
    raw_outcomes = bundle["results"]["raw_outcomes"]
    schema = raw_outcomes["schema"]
    assert schema == {
        "check": [
            "rule_id",
            "verdict",
            "severity",
            "reason_codes",
            "evidence_links",
        ],
        "evidence_link": [
            "document_id",
            "document_role",
            "field",
            "value_state",
            "raw_masked",
            "observation_id",
            "source_object_ref",
            "source_sha256",
            "provenance_manifest_digest",
            "source_page",
            "source_region",
            "evidence_eligible",
            "eligibility_reason",
        ],
        "selection": [
            "rule_id",
            "observation_id",
            "document_id",
            "document_role",
            "field",
            "selected",
            "reason_code",
        ],
        "normalization": [
            "rule_id",
            "observation_id",
            "document_id",
            "document_role",
            "field",
            "normalized",
            "notes",
            "ocr_fix",
            "pre_ocr",
        ],
    }
    candidate_digest = raw_outcomes["runs"]["candidate"]["outcome_set_digest"]
    candidate_outcomes = raw_outcomes["outcome_sets"][candidate_digest]
    assert set(raw_outcomes["runs"]) == {"anchor", "candidate", "again"}
    assert all(
        run["outcome_set_digest"] in raw_outcomes["outcome_sets"]
        for run in raw_outcomes["runs"].values()
    )
    complete_outcomes = [
        outcome for outcome in candidate_outcomes if "skipped" not in outcome
    ]
    assert complete_outcomes
    assert complete_outcomes[0]["checks"]
    assert len(complete_outcomes[0]["checks"][0]) == len(schema["check"])
    selections = [
        selection
        for outcome in candidate_outcomes
        for selection in outcome.get("selection", [])
    ]
    normalizations = [
        normalization
        for outcome in candidate_outcomes
        for normalization in outcome.get("normalization", [])
    ]
    selected_index = schema["selection"].index("selected")
    selection_reason_index = schema["selection"].index("reason_code")
    normalized_index = schema["normalization"].index("normalized")
    assert selections and any(
        selection[selected_index] is True for selection in selections
    )
    assert all(
        selection[selection_reason_index] != "PROVENANCE_INELIGIBLE"
        for selection in selections
    )
    assert normalizations and any(
        normalization[normalized_index] is not None
        for normalization in normalizations
    )

    # 5. The bundle and its candidate are immutable; a second validation run
    #    of the same candidate produces the identical bundle.
    assert service.query_active(ADMIN)["status"] == "active"
    state = service._candidate_state(service._store.policy_governance_events, candidate_id)
    assert state["status"] == "validated"

    # 6. Unknown source items are never silently dropped: an unrecognized
    #    top-level rules option is an explicit unsupported ledger entry and
    #    the candidate is rejected by the mapping completeness check.
    service2, rules_path2, _ = make_policy_service(tmp_path / "unknown-field")
    rules2 = yaml.safe_load(rules_path2.read_bytes())
    rules2["unknown_runtime_option"] = {"affects": "mystery"}
    rules_path2.write_bytes(
        yaml.safe_dump(rules2, sort_keys=False).encode("utf-8")
    )
    draft2 = import_draft(service2)
    drafts2 = service2.query_drafts(ADMIN)["drafts"]
    draft2_record = next(item for item in drafts2 if item["draft_id"] == draft2)
    ledger2 = ledger_content(service2, draft2_record["mapping_ledger_id"])
    unknown_entries = [
        item
        for item in ledger2["items"]
        if item["source_pointer"] == "/unknown_runtime_option"
    ]
    assert len(unknown_entries) == 1
    entry = unknown_entries[0]
    assert entry["classification"] == "unsupported"
    assert entry["target_ref"] is None
    assert entry["source_digest"] == content_digest(
        ("unknown_rules_item", "unknown_runtime_option", rules2["unknown_runtime_option"])
    )
    assert entry["result_digest"] == entry["source_digest"]
    candidate2, worker2 = freeze_and_validate(service2, draft2)
    assert worker2["outcome"] == "rejected"
    bundle2 = validation_bundle(service2, worker2["validation_bundle_id"])
    assert check_outcomes(bundle2)["mapping_ledger"] == "fail"
    assert service2.query_active(ADMIN)["active_generation"] == 1

    # 6b. Unknown nested rule fields are never silently dropped: a mystery
    #     field inside rules[0] is an explicit unsupported ledger entry at
    #     its exact JSON pointer, and the candidate is rejected.
    service2b, rules_path2b, _ = make_policy_service(tmp_path / "unknown-rule-field")
    rules2b = yaml.safe_load(rules_path2b.read_bytes())
    rules2b["rules"][0]["mystery_runtime_field"] = {"affects": "unknown"}
    rules_path2b.write_bytes(
        yaml.safe_dump(rules2b, sort_keys=False).encode("utf-8")
    )
    draft2b = import_draft(service2b)
    drafts2b = service2b.query_drafts(ADMIN)["drafts"]
    draft2b_record = next(
        item for item in drafts2b if item["draft_id"] == draft2b
    )
    ledger2b = ledger_content(service2b, draft2b_record["mapping_ledger_id"])
    mystery_entries = [
        item
        for item in ledger2b["items"]
        if item["source_pointer"] == "/rules/0/mystery_runtime_field"
    ]
    assert len(mystery_entries) == 1
    mystery = mystery_entries[0]
    assert mystery["classification"] == "unsupported"
    assert mystery["target_ref"] is None
    assert mystery["result_digest"] == content_digest(
        (
            "rule_field_unknown",
            str(rules2b["rules"][0]["id"]),
            "mystery_runtime_field",
            rules2b["rules"][0]["mystery_runtime_field"],
        )
    )
    # Accepted rule fields bind their compiled target values.
    compiled_entry = next(
        item
        for item in ledger2b["items"]
        if item["source_pointer"] == f"/rules/0/severity"
    )
    assert compiled_entry["classification"] == "exact"
    assert compiled_entry["result_digest"] == content_digest(
        (
            "rule_field_compiled",
            str(rules2b["rules"][0]["id"]),
            "severity",
            str(rules2b["rules"][0].get("severity") or "major").lower(),
        )
    )
    candidate2b, worker2b = freeze_and_validate(service2b, draft2b)
    assert worker2b["outcome"] == "rejected"
    bundle2b = validation_bundle(service2b, worker2b["validation_bundle_id"])
    assert check_outcomes(bundle2b)["mapping_ledger"] == "fail"
    assert service2b.query_active(ADMIN)["active_generation"] == 1

    # 7. Source-byte mutation never replays the previous result under the
    #    same key: the import fingerprint binds the raw bytes.
    service3, rules_path3, _ = make_policy_service(tmp_path / "byte-mutation")
    import_key = f"mutation-import-{time.time_ns()}"
    first_import = service3.import_legacy(
        principal=ADMIN,
        source_bundle_id=SOURCE_BUNDLE_ID,
        idempotency_key=import_key,
        expected_governance_revision=governance_revision(service3),
    )
    before = governance_revision(service3)
    mutated_bytes = DEFAULT_RULES.read_bytes().replace(
        b'version: "1.9.0"', b'version: "9.9.9"'
    )
    assert mutated_bytes != DEFAULT_RULES.read_bytes()
    rules_path3.write_bytes(mutated_bytes)
    with pytest.raises(PolicyConflict):
        service3.import_legacy(
            principal=ADMIN,
            source_bundle_id=SOURCE_BUNDLE_ID,
            idempotency_key=import_key,
            expected_governance_revision=governance_revision(service3),
        )
    assert governance_revision(service3) == before
    # The same key with the new bytes is a new identity, not a replay.
    fresh_import = service3.import_legacy(
        principal=ADMIN,
        source_bundle_id=SOURCE_BUNDLE_ID,
        idempotency_key=f"mutation-import-{time.time_ns()}",
        expected_governance_revision=governance_revision(service3),
    )
    assert fresh_import["draft_id"] != first_import["draft_id"]


@pytest.mark.parametrize("duplicate_source", ["rules", "knowledge"])
def test_duplicate_nested_source_keys_fail_closed_before_state_mutation(
    tmp_path: Path, duplicate_source: str
) -> None:
    rules_bytes = DEFAULT_RULES.read_bytes()
    kb_bytes = DEFAULT_KB.read_bytes()
    if duplicate_source == "rules":
        rules_bytes = rules_bytes.replace(
            b"  - id: R_VIN_CROSS\n",
            b"  - id: R_VIN_CROSS\n    id: R_VIN_CROSS\n",
            1,
        )
    else:
        kb_bytes = kb_bytes.replace(
            '    "南京市": "南京",\n'.encode(),
            ('    "南京市": "南京",\n' * 2).encode(),
            1,
        )

    def state_counts(service: PolicyGovernanceService) -> tuple[int, ...]:
        store = service._store
        return (
            len(store.policy_governance_events),
            len(store.policy_artifacts),
            len(store.audit_events),
            len(store.outbox),
            len(store.idempotency),
        )

    bootstrap, _, _ = make_policy_service(
        tmp_path / "bootstrap",
        rules_bytes=rules_bytes,
        kb_bytes=kb_bytes,
        expect_bootstrap=False,
    )
    assert bootstrap.bootstrap_once()["status"] == "blocked"
    assert state_counts(bootstrap) == (0, 0, 0, 0, 0)

    service, rules_path, kb_path = make_policy_service(tmp_path / "import")
    before = state_counts(service)
    rules_path.write_bytes(rules_bytes)
    kb_path.write_bytes(kb_bytes)
    with pytest.raises(PolicyInvalidTransition, match="cannot be parsed"):
        service.import_legacy(
            principal=ADMIN,
            source_bundle_id=SOURCE_BUNDLE_ID,
            idempotency_key=f"duplicate-{duplicate_source}",
            expected_governance_revision=governance_revision(service),
        )
    assert state_counts(service) == before


def test_corpus_comparison_preserves_duplicate_application_items() -> None:
    def outcome(item_id: str, applicable: list[str]) -> dict[str, Any]:
        return {
            "corpus_item_id": item_id,
            "application_id": "APP-DUPLICATE",
            "checks": [{"rule_id": rule_id} for rule_id in applicable],
            "applicable": applicable,
            "selection": [],
            "normalization": [],
            "verdicts": [],
            "route": "auto_complete",
        }

    anchor = [outcome("fixture-a.json", ["R_A"]), outcome("fixture-b.json", ["R_B"])]
    candidate = [
        outcome("fixture-a.json", ["R_CHANGED"]),
        outcome("fixture-b.json", ["R_B"]),
    ]
    compared = PolicyGovernanceService._compare_corpus(anchor, candidate)
    assert compared["compared"] == 2
    assert compared["checks_equal"] is False


def test_mapping_ledger_content_address_is_rechecked_before_validation(
    tmp_path: Path,
) -> None:
    import sqlite3 as sqlite3_module

    from task4_consistency.controlled.s01_store import _encode, _integrity_digest

    service, rules_path, _ = make_policy_service(tmp_path)
    rules = yaml.safe_load(rules_path.read_bytes())
    rules["rules"][0]["mystery_runtime_field"] = "must-not-disappear"
    rules_path.write_bytes(yaml.safe_dump(rules, sort_keys=False).encode("utf-8"))
    draft_id = import_draft(service)
    draft = next(
        item
        for item in service.query_drafts(ADMIN)["drafts"]
        if item["draft_id"] == draft_id
    )
    freeze = service.freeze_candidate(
        principal=ADMIN,
        draft_id=draft_id,
        idempotency_key=f"ledger-freeze-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    service.request_validation(
        principal=ADMIN,
        candidate_id=freeze["candidate_id"],
        idempotency_key=f"ledger-validate-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )

    ledger_id = draft["mapping_ledger_id"]
    with sqlite3_module.connect(service._store.state_path) as connection:
        payload = connection.execute(
            "SELECT payload FROM policy_artifacts WHERE item_id = ?",
            (ledger_id,),
        ).fetchone()[0]
        artifact = json.loads(payload)
        ledger = json.loads(artifact["canonical_json"])
        unsupported = [
            item
            for item in ledger["items"]
            if item.get("classification") == "unsupported"
        ]
        assert unsupported
        ledger["items"] = [
            item
            for item in ledger["items"]
            if item.get("classification") != "unsupported"
        ]
        artifact["canonical_json"] = canonical_bytes(ledger).decode("utf-8")
        encoded = _encode(artifact)
        integrity = _integrity_digest("policy_artifacts", ledger_id, encoded)
        connection.execute(
            "UPDATE policy_artifacts SET payload = ?, integrity_sha256 = ? "
            "WHERE item_id = ?",
            (encoded, integrity, ledger_id),
        )
        connection.execute(
            "UPDATE s01_immutable_catalog SET integrity_sha256 = ? "
            "WHERE table_name = 'policy_artifacts' AND item_id = ?",
            (integrity, ledger_id),
        )
        connection.commit()

    result = service.process_next_policy_job()
    assert result["status"] == "failed"
    state = service._candidate_state(
        service._store.policy_governance_events, freeze["candidate_id"]
    )
    assert state["status"] == "candidate"
    assert state.get("validation_bundle_id") is None
    assert service.query_active(ADMIN)["active_generation"] == 1


def test_checker_artifact_recomputes_declared_semantic_digests(
    tmp_path: Path,
) -> None:
    from task4_consistency.controlled.s01_checker import (
        ProtectedInvariantError,
        TargetRelease,
    )

    service, _, _ = make_policy_service(tmp_path)
    release = service.resolve_run_pin(S08_SCOPE, int(time.time()))["release"][
        "target_release"
    ]
    artifact = release.to_artifact()

    waiver_drift = copy.deepcopy(artifact)
    waiver_drift["rules"] = [
        {
            **rule,
            "waivable": True,
            "waiver_reasons": ["DOCUMENTED_BRAND_VARIANCE"],
            "waiver_scope": "one_application_cycle_run_finding",
            "waiver_ttl_seconds": 900,
        }
        if rule["rule_id"] == "R_NAME_FUZZY"
        else rule
        for rule in waiver_drift["rules"]
    ]
    normalizer_drift = {**artifact, "vin_fix_ioq": not artifact["vin_fix_ioq"]}
    knowledge_drift = copy.deepcopy(artifact)
    knowledge_drift["knowledge"] = [
        [section, [list(pair) for pair in values]]
        for section, values in artifact["knowledge"]
    ]
    knowledge_drift["knowledge"][0][1][0][1] = "drifted-canonical-value"

    duplicate_rule = copy.deepcopy(artifact)
    critical = next(
        rule for rule in duplicate_rule["rules"] if rule["rule_id"] == "R_VIN_CROSS"
    )
    duplicate_rule["rules"] = [
        {**critical, "severity": "minor"},
        *duplicate_rule["rules"],
    ]
    waiver_material = {
        "schema_version": "c-demo-waiver-policy/1",
        "policy_id": duplicate_rule["waiver_policy_id"],
        "checks": [
            {
                "rule_id": rule["rule_id"],
                "waivable": rule["waivable"],
                "allowed_reasons": (
                    list(rule["waiver_reasons"]) if rule["waivable"] else []
                ),
                "scope": rule["waiver_scope"] if rule["waivable"] else None,
                "maximum_ttl_seconds": (
                    rule["waiver_ttl_seconds"] if rule["waivable"] else 0
                ),
            }
            for rule in duplicate_rule["rules"]
        ],
    }
    duplicate_rule["waiver_policy_digest"] = content_digest(waiver_material)
    for rule in duplicate_rule["rules"]:
        rule["waiver_policy_digest"] = duplicate_rule["waiver_policy_digest"]

    for drifted in (
        waiver_drift,
        normalizer_drift,
        knowledge_drift,
        duplicate_rule,
    ):
        with pytest.raises(ProtectedInvariantError):
            TargetRelease.from_artifact(drifted)


def test_validation_rejects_runtime_behavior_identity_change(
    tmp_path: Path,
) -> None:
    service, rules_path, _ = make_policy_service(tmp_path)
    rules = yaml.safe_load(rules_path.read_bytes())
    name_rule = next(rule for rule in rules["rules"] if rule["id"] == "R_NAME_FUZZY")
    name_rule["threshold"] = 0.99
    rules_path.write_bytes(yaml.safe_dump(rules, sort_keys=False).encode("utf-8"))

    candidate_id, worker = freeze_and_validate(service, import_draft(service))
    assert worker["outcome"] == "rejected"
    candidate = next(
        item
        for item in service.query_candidates(ADMIN)["candidates"]
        if item["candidate_id"] == candidate_id
    )
    outcomes = check_outcomes(
        validation_bundle(service, candidate["validation_bundle_id"])
    )
    assert outcomes["runtime_behavior_identity"] == "fail"
    assert service.query_active(ADMIN)["active_generation"] == 1


def test_cross_city_alias_blocks_bootstrap_and_import_validation(
    tmp_path: Path,
) -> None:
    """The legacy cross-city prohibition is part of governed semantic safety,
    including the bootstrap where no prior behavior anchor exists."""
    knowledge = json.loads(DEFAULT_KB.read_bytes())
    knowledge["address_aliases"]["江苏苏州工业园"] = "江苏南京新区"
    poisoned = json.dumps(knowledge, ensure_ascii=False).encode("utf-8")

    bootstrap, _, _ = make_policy_service(
        tmp_path / "bootstrap",
        kb_bytes=poisoned,
        expect_bootstrap=False,
    )
    assert bootstrap.query_active(ADMIN)["status"] == "none"
    assert bootstrap._store.policy_governance_events == []

    service, _, kb_path = make_policy_service(tmp_path / "import")
    kb_path.write_bytes(poisoned)
    candidate_id, worker = freeze_and_validate(service, import_draft(service))
    assert worker["outcome"] == "rejected"
    candidate = next(
        item
        for item in service.query_candidates(ADMIN)["candidates"]
        if item["candidate_id"] == candidate_id
    )
    outcomes = check_outcomes(
        validation_bundle(service, candidate["validation_bundle_id"])
    )
    assert outcomes["semantic_entity_safety"] == "protected_fail"
    assert service.query_active(ADMIN)["active_generation"] == 1


def test_candidate_scope_covers_bound_and_actual_activation_time(
    tmp_path: Path,
) -> None:
    """Validation-time validity is insufficient: the frozen scope must cover
    both the approved activation time and the actual activation linearization."""
    from datetime import datetime, timezone

    now = int(time.time())

    def iso(timestamp: int) -> str:
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()

    def reviewed_candidate(
        service: PolicyGovernanceService, valid_to: int, label: str
    ) -> str:
        draft_id = import_draft(service)
        service.revise_draft(
            principal=ADMIN,
            draft_id=draft_id,
            metadata={
                "scope": S08_SCOPE,
                "validity": {
                    "valid_from": iso(now - 60),
                    "valid_to": iso(valid_to),
                },
                "source": SOURCE_BUNDLE_ID,
                "reason": label,
            },
            idempotency_key=f"revise-{label}",
            expected_governance_revision=governance_revision(service),
        )
        candidate_id, worker = freeze_and_validate(service, draft_id)
        assert worker["outcome"] == "validated"
        service.submit_review(
            principal=ADMIN,
            candidate_id=candidate_id,
            idempotency_key=f"review-{label}",
            expected_governance_revision=governance_revision(service),
        )
        return candidate_id

    service, _, _ = make_policy_service(tmp_path / "bound")
    candidate = reviewed_candidate(service, now + 30, "bound-expiry")
    preview = service.preview_impact(
        principal=ADMIN,
        candidate_id=candidate,
        idempotency_key="approve-after-expiry-preview",
        expected_governance_revision=governance_revision(service),
    )
    revision_before = governance_revision(service)
    with pytest.raises(PolicyInvalidTransition):
        service.approve(
            principal=APPROVER,
            candidate_id=candidate,
            activation_time=now + 60,
            recovery_release_id=service.query_active(ADMIN)["candidate_id"],
            preview_manifest_id=preview["manifest_id"],
            idempotency_key="approve-after-expiry",
            expected_governance_revision=governance_revision(service),
        )
    assert governance_revision(service) == revision_before
    assert service.query_active(ADMIN)["active_generation"] == 1

    delayed, _, _ = make_policy_service(tmp_path / "delayed")
    delayed_candidate = reviewed_candidate(delayed, now + 120, "delayed-expiry")
    approval = preview_and_approve(
        delayed,
        candidate_id=delayed_candidate,
        activation_time=now + 60,
        recovery_release_id=delayed.query_active(ADMIN)["candidate_id"],
        idempotency_key="approve-before-expiry",
    )
    delayed.schedule(
        principal=ADMIN,
        approval_binding_id=approval["approval_binding_id"],
        activation_at=now + 60,
        idempotency_key="schedule-before-expiry",
        expected_governance_revision=governance_revision(delayed),
    )
    protected_before = {
        "generation": delayed.query_active(ADMIN)["active_generation"],
        "events": governance_revision(delayed),
        "audit": len(delayed._store.audit_events),
        "outbox": len(delayed._store.outbox),
        "idempotency": len(delayed._store.idempotency),
    }
    result = delayed.process_next_policy_job(now=now + 180)
    assert result["status"] == "failed", result
    assert delayed.query_active(ADMIN)["active_generation"] == protected_before[
        "generation"
    ]
    assert governance_revision(delayed) == protected_before["events"]
    assert len(delayed._store.audit_events) == protected_before["audit"]
    assert len(delayed._store.outbox) == protected_before["outbox"]
    assert len(delayed._store.idempotency) == protected_before["idempotency"]

    expiring, _, _ = make_policy_service(tmp_path / "resolution")
    expiring_candidate = reviewed_candidate(
        expiring, now + 120, "resolution-expiry"
    )
    approval = preview_and_approve(
        expiring,
        candidate_id=expiring_candidate,
        activation_time=now + 60,
        recovery_release_id=expiring.query_active(ADMIN)["candidate_id"],
        idempotency_key="approve-expiring-resolution",
    )
    expiring.schedule(
        principal=ADMIN,
        approval_binding_id=approval["approval_binding_id"],
        activation_at=now + 60,
        idempotency_key="schedule-expiring-resolution",
        expected_governance_revision=governance_revision(expiring),
    )
    activated = expiring.process_next_policy_job(now=now + 60)
    assert activated["status"] == "complete", activated
    historical_pin = expiring.resolve_run_pin(S08_SCOPE, now + 90)
    assert historical_pin is not None
    revision_before = governance_revision(expiring)
    with pytest.raises(PolicyUnavailable):
        expiring.resolve_run_pin(S08_SCOPE, now + 180)
    assert governance_revision(expiring) == revision_before
    assert (
        expiring.load_pinned_release(historical_pin).release_digest
        == historical_pin["release_digest"]
    )


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
            preview_and_approve(
                service,
                candidate_id=candidate_id,
                activation_time=int(time.time()) + 60,
                recovery_release_id=active["candidate_id"],
                idempotency_key=f"approve-{time.time_ns()}",
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

    # (h) Protected-baseline waivability: a crafted checker that makes
    #     R_VIN_CROSS waivable (with all content IDs recomputed) is a
    #     protected failure; approval is impossible and the active
    #     generation stays untouched.
    service, _, _ = make_policy_service(tmp_path / "protected-waivable")
    crafted = _craft_mutated_candidate(
        service,
        mutate_checker=lambda checker: checker.__setitem__(
            "rules",
            [
                {
                    **rule,
                    "waivable": True,
                    "waiver_policy_id": "c-demo-brand-exception/1",
                    "waiver_reasons": ["DOCUMENTED_BRAND_VARIANCE"],
                    "waiver_scope": "one_application_cycle_run_finding",
                    "waiver_ttl_seconds": 900,
                }
                if rule["rule_id"] == "R_VIN_CROSS"
                else rule
                for rule in checker["rules"]
            ],
        ),
    )
    assert_rejected_without_override(
        service,
        crafted,
        expected_failed="protected_baseline",
        crafted=True,
        case="protected-waivable",
    )

    # (g) Missing/empty/invalid frozen corpus fails closed: bootstrap cannot
    #     produce an active release without a corpus, and validation of an
    #     ordinary candidate is rejected with the active generation intact.
    empty_corpus = tmp_path / "empty-corpus"
    empty_corpus.mkdir()
    blocked_service, _, _ = make_policy_service(
        tmp_path / "corpus-empty",
        corpus_root=empty_corpus,
        expect_bootstrap=False,
    )
    bootstrap = blocked_service.bootstrap_once()
    assert bootstrap["status"] == "blocked"
    with pytest.raises(PolicyUnavailable):
        blocked_service._load_corpus()

    invalid_corpus = tmp_path / "invalid-corpus"
    invalid_corpus.mkdir()
    (invalid_corpus / "broken.json").write_text("{not json", encoding="utf-8")
    service, _, _ = make_policy_service(
        tmp_path / "corpus-invalid",
        corpus_root=invalid_corpus,
        expect_bootstrap=False,
    )
    with pytest.raises(PolicyUnavailable):
        service._load_corpus()

    # A missing corpus directory is fail-closed on a warm service: the
    # ordinary candidate is rejected and the prior active stays intact.
    missing_corpus = tmp_path / "missing-corpus"
    service, _, _ = make_policy_service(tmp_path / "corpus-missing")
    draft_id = import_draft(service)
    service._corpus_root = missing_corpus
    candidate_id, worker = freeze_and_validate(service, draft_id)
    assert worker["outcome"] == "rejected"
    bundle = validation_bundle(service, worker["validation_bundle_id"])
    assert check_outcomes(bundle)["corpus_bound"] == "fail"
    assert service.query_active(ADMIN)["active_generation"] == 1


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
    activation_at = int(time.time()) + max(activation_delay, 60)
    approved = preview_and_approve(
        service,
        candidate_id=candidate_id,
        activation_time=activation_at,
        recovery_release_id=service.query_active(admin)["candidate_id"],
        idempotency_key=f"a-{time.time_ns()}",
        approver=approver,
    )
    scheduled = service.schedule(
        principal=admin,
        approval_binding_id=approved["approval_binding_id"],
        activation_at=activation_at,
        idempotency_key=f"s-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    return draft_id, candidate_id, scheduled["reservation_id"], activation_at


def test_stale_approval_diff_cannot_schedule_after_anchor_activation(
    tmp_path: Path,
) -> None:
    """An approval reviews one exact active anchor.  If another candidate
    becomes active first, the stale approval cannot be scheduled against the
    new generation without a new review and approval."""
    service, rules_path, _ = make_policy_service(tmp_path)
    bootstrap_id = service.query_active(ADMIN)["candidate_id"]
    now = int(time.time())

    def prepare_candidate() -> str:
        draft_id = import_draft(service)
        candidate_id, worker = freeze_and_validate(service, draft_id)
        assert worker["outcome"] == "validated"
        service.submit_review(
            principal=ADMIN,
            candidate_id=candidate_id,
            idempotency_key=f"review-{time.time_ns()}",
            expected_governance_revision=governance_revision(service),
        )
        return candidate_id

    def approve_candidate(
        candidate_id: str, activation_time: int
    ) -> dict[str, Any]:
        return preview_and_approve(
            service,
            candidate_id=candidate_id,
            activation_time=activation_time,
            recovery_release_id=bootstrap_id,
            idempotency_key=f"approve-{time.time_ns()}",
        )
        return candidate_id, approved

    rules_path.write_bytes(
        rules_path.read_bytes().replace(b'version: "1.9.0"', b'version: "2.0.0"')
    )
    candidate_a = prepare_candidate()

    rules_path.write_bytes(
        rules_path.read_bytes().replace(b'version: "2.0.0"', b'version: "3.0.0"')
    )
    candidate_b = prepare_candidate()
    approval_b = approve_candidate(candidate_b, now + 400)
    approval_a = approve_candidate(candidate_a, now + 300)
    service.schedule(
        principal=ADMIN,
        approval_binding_id=approval_a["approval_binding_id"],
        activation_at=now + 300,
        idempotency_key="schedule-a",
        expected_governance_revision=governance_revision(service),
    )

    binding_b = service._artifact(service._store, approval_b["approval_binding_id"])
    assert binding_b["diff"]["anchor_candidate_id"] == bootstrap_id

    activated = service.process_next_policy_job(now=now + 300)
    assert activated["status"] == "complete", activated
    assert activated["candidate_id"] == candidate_a
    assert service.query_active(ADMIN)["active_generation"] == 2

    protected_before = {
        "revision": governance_revision(service),
        "audit": len(service._store.audit_events),
        "outbox": len(service._store.outbox),
        "idempotency": len(service._store.idempotency),
    }
    with pytest.raises(PolicyConflict):
        service.schedule(
            principal=ADMIN,
            approval_binding_id=approval_b["approval_binding_id"],
            activation_at=now + 400,
            idempotency_key="schedule-b-stale-anchor",
            expected_governance_revision=governance_revision(service),
        )
    assert service.query_active(ADMIN)["candidate_id"] == candidate_a
    assert governance_revision(service) == protected_before["revision"]
    assert len(service._store.audit_events) == protected_before["audit"]
    assert len(service._store.outbox) == protected_before["outbox"]
    assert len(service._store.idempotency) == protected_before["idempotency"]
    assert service.query_candidate(ADMIN, candidate_b)["status"] == "approved"


def test_frozen_draft_revisions_have_distinct_content_bound_fork_ids(
    tmp_path: Path,
) -> None:
    """Two revisions of one frozen draft in the same trusted second retain
    distinct immutable forks instead of overwriting by timestamp collision."""
    service, _, _ = make_policy_service(tmp_path)
    draft_id = import_draft(service)
    service.freeze_candidate(
        principal=ADMIN,
        draft_id=draft_id,
        idempotency_key="freeze-for-forks",
        expected_governance_revision=governance_revision(service),
    )
    fixed_time = int(time.time())
    service._clock = lambda: fixed_time

    fork_ids: list[str] = []
    for reason in ("first same-second fork", "second same-second fork"):
        result = service.revise_draft(
            principal=ADMIN,
            draft_id=draft_id,
            metadata={
                "scope": S08_SCOPE,
                "validity": {"valid_from": "2000-01-01T00:00:00Z"},
                "source": SOURCE_BUNDLE_ID,
                "reason": reason,
            },
            idempotency_key=f"fork-{reason}",
            expected_governance_revision=governance_revision(service),
        )
        fork_ids.append(result["draft_id"])

    assert len(set(fork_ids)) == 2
    service._store.reload()
    assert {
        service._store.policy_drafts[fork_id]["metadata"]["reason"]
        for fork_id in fork_ids
    } == {"first same-second fork", "second same-second fork"}
    assert all(
        service._store.policy_drafts[fork_id]["forked_from"] == draft_id
        for fork_id in fork_ids
    )


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
    preview_and_approve(
        service,
        candidate_id=candidate_id,
        activation_time=activation_at,
        recovery_release_id=service.query_active(ADMIN)["candidate_id"],
        idempotency_key=f"a-{time.time_ns()}",
    )
    approval_binding_id = next(
        item
        for item in service.query_candidates(ADMIN)["candidates"]
        if item["candidate_id"] == candidate_id
    )["approval_binding_id"]
    scheduled = service.schedule(
        principal=ADMIN,
        approval_binding_id=approval_binding_id,
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

    # 3b. Empty idempotency keys and missing/None expected revisions are
    #     rejected with zero effect.
    with pytest.raises(PolicyInvalidTransition):
        service.revise_draft(
            principal=ADMIN,
            draft_id=draft_id,
            metadata={
                "scope": S08_SCOPE,
                "validity": {"valid_from": "2000-01-01T00:00:00Z"},
                "source": SOURCE_BUNDLE_ID,
                "reason": "empty key",
            },
            idempotency_key="   ",
            expected_governance_revision=governance_revision(service),
        )
    with pytest.raises((PolicyInvalidTransition, PolicyConflict)):
        service.revise_draft(
            principal=ADMIN,
            draft_id=draft_id,
            metadata={
                "scope": S08_SCOPE,
                "validity": {"valid_from": "2000-01-01T00:00:00Z"},
                "source": SOURCE_BUNDLE_ID,
                "reason": "missing revision",
            },
            idempotency_key=f"no-rev-{time.time_ns()}",
            expected_governance_revision=None,
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
            preview_manifest_id="",
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
                    preview_manifest_id="",
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

    # 6. Candidate mutation always creates a new identity: revising a
    #    frozen draft forks a new draft identity, and freezing the fork
    #    yields a distinct candidate.
    revise_result = service.revise_draft(
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
    forked_draft_id = revise_result["draft_id"]
    assert forked_draft_id != draft_id
    forked = service.freeze_candidate(
        principal=ADMIN,
        draft_id=forked_draft_id,
        idempotency_key=f"fork-freeze-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    assert forked["candidate_id"] != candidate_id
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

    # 8. Role coexistence and cross-scope: the independent approver reads the
    #    exact candidate workspace while the admin operates, and a
    #    foreign-scope identity cannot touch the scope at all.
    workspace = service.query_candidate(APPROVER, candidate_id)
    assert workspace["candidate_id"] == candidate_id
    assert workspace["manifest_digest"] == freeze["manifest_digest"]
    assert workspace["approval_binding_id"] == approval_binding_id
    foreign = PolicyPrincipal(
        subject="c-demo-foreign-admin",
        role="admin",
        scope="OTHER/demo",
        source_id="s08-test",
    )
    with pytest.raises(PolicyInvalidTransition):
        service.query_active(foreign)
    with pytest.raises(PolicyInvalidTransition):
        service.import_legacy(
            principal=foreign,
            source_bundle_id=SOURCE_BUNDLE_ID,
            idempotency_key=f"foreign-{time.time_ns()}",
            expected_governance_revision=governance_revision(service),
        )

    # 9. Approval invalidation: the binding pins candidate digest and
    #    activation time, so a mutated candidate can never be activated
    #    under the original binding and schedule drift is rejected.
    binding = service._artifact(service._store, approval_binding_id)
    assert binding["candidate_id"] == candidate_id
    assert binding["candidate_digest"] == freeze["manifest_digest"]
    # The frozen fork from step 6 is a distinct candidate that must be
    # validated, reviewed and re-approved under its own binding.
    service.request_validation(
        principal=ADMIN,
        candidate_id=forked["candidate_id"],
        idempotency_key=f"fork-v-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    fork_validation = service.process_next_policy_job(now=activation_at - 1)
    assert fork_validation["candidate_id"] == forked["candidate_id"]
    assert fork_validation["outcome"] == "validated"
    service.submit_review(
        principal=ADMIN,
        candidate_id=forked["candidate_id"],
        idempotency_key=f"fork-rv-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    fork_activation_at = int(time.time()) + 120
    fork_approval = preview_and_approve(
        service,
        candidate_id=forked["candidate_id"],
        activation_time=fork_activation_at,
        recovery_release_id=service.query_active(ADMIN)["candidate_id"],
        idempotency_key=f"fork-a-{time.time_ns()}",
    )
    assert fork_approval["candidate_id"] == forked["candidate_id"]
    assert fork_approval["approval_binding_id"] != approval_binding_id
    fork_binding = service._artifact(service._store, fork_approval["approval_binding_id"])
    assert fork_binding["candidate_id"] == forked["candidate_id"]
    # A metadata-only fork stays behavior-equivalent (same runtime digest)
    # but is a distinct identity that needs its own binding: the original
    # approval never covers it.
    assert fork_binding["candidate_digest"] == freeze["manifest_digest"]
    assert fork_binding["candidate_id"] != candidate_id
    # Schedule drift is rejected: the binding pins the trusted activation
    # time, so a deviating schedule can never advance.
    with pytest.raises(PolicyInvalidTransition):
        service.schedule(
            principal=ADMIN,
            approval_binding_id=fork_approval["approval_binding_id"],
            activation_at=fork_activation_at + 5,
            idempotency_key=f"drift-{time.time_ns()}",
            expected_governance_revision=governance_revision(service),
        )

    # Candidate mutation after approval invalidates the approval: corrupting
    # the candidate manifest in the Registry makes the scheduled activation
    # fail closed and the prior active generation is preserved.
    import sqlite3 as sqlite3_module

    with sqlite3_module.connect(service._store.state_path) as connection:
        connection.execute(
            "DELETE FROM policy_manifests WHERE item_id = ?", (freeze["manifest_id"],)
        )
        connection.execute(
            "DELETE FROM s01_immutable_catalog WHERE table_name = 'policy_manifests' "
            "AND item_id = ?",
            (freeze["manifest_id"],),
        )
        connection.commit()
    outcome = service.process_next_policy_job(now=activation_at + 31)
    assert outcome["status"] == "failed", outcome
    assert outcome["reason_code"] == "S08_ACTIVATION_UNAVAILABLE"
    # The ledger is not polluted: the fold still shows generation 1, but the
    # corrupted Registry can no longer produce a verifiable active query.
    folded = service._fold_active_projection(
        service._store.policy_governance_events, S08_SCOPE
    )
    assert folded is not None
    assert folded["active_generation"] == 1
    with pytest.raises(PolicyUnavailable):
        service.query_active(ADMIN)

    # 10. The configured operator subject owns ordinary worker facts: every
    #     validation/activation governance event carries the configured
    #     operator subject with the stable worker source id, and no
    #     hard-coded activator identity appears anywhere.
    worker_events = [
        event
        for event in service._store.policy_governance_events
        if event.get("actor", {}).get("source_id") == "s08-policy-worker"
    ]
    assert worker_events
    assert {event["actor"]["subject"] for event in worker_events} == {
        "c-demo-policy-operator"
    }
    assert all(event["actor"]["role"] == "operator" for event in worker_events)
    assert not any(
        event.get("actor", {}).get("subject") == "s08-activator"
        for event in service._store.policy_governance_events
    )
    assert not any(
        record.get("subject") == "s08-activator"
        for record in service._store.audit_events
    )


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
            lifecycle_snapshot_provider=_empty_impact_snapshot,
        )

    first = make()
    first.bootstrap_once()
    draft = import_draft(first)
    revised = first.revise_draft(
        principal=ADMIN,
        draft_id=draft,
        metadata={
            "scope": S08_SCOPE,
            "validity": {"valid_from": "2000-01-01T00:00:00Z"},
            "source": SOURCE_BUNDLE_ID,
            "reason": "overlap anchor",
        },
        idempotency_key=f"r-{time.time_ns()}",
        expected_governance_revision=governance_revision(first),
    )
    frozen = first.freeze_candidate(
        principal=ADMIN,
        draft_id=revised["draft_id"],
        idempotency_key=f"f-{time.time_ns()}",
        expected_governance_revision=governance_revision(first),
    )
    candidate_id = frozen["candidate_id"]
    first.request_validation(
        principal=ADMIN,
        candidate_id=candidate_id,
        idempotency_key=f"v-{time.time_ns()}",
        expected_governance_revision=governance_revision(first),
    )
    first.process_next_policy_job()
    first.submit_review(
        principal=ADMIN,
        candidate_id=candidate_id,
        idempotency_key=f"rv-{time.time_ns()}",
        expected_governance_revision=governance_revision(first),
    )
    second = make()

    # 1. Overlapping schedule: while the first candidate's reservation is
    #    still pending, scheduling a second approved candidate is rejected
    #    by the unique reservation constraint.
    second_draft = import_draft(first)
    second_revise = first.revise_draft(
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
        draft_id=second_revise["draft_id"],
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
    activation_b = int(time.time()) + 30
    approved_b = preview_and_approve(
        first,
        candidate_id=second_candidate,
        activation_time=activation_b,
        recovery_release_id=first.query_active(ADMIN)["candidate_id"],
        idempotency_key=f"a-{time.time_ns()}",
    )
    activation_at = int(time.time()) + 300
    approved_a = preview_and_approve(
        first,
        candidate_id=candidate_id,
        activation_time=activation_at,
        recovery_release_id=first.query_active(ADMIN)["candidate_id"],
        idempotency_key=f"a-{time.time_ns()}",
    )
    assert all(
        callable(service._lifecycle_snapshot_provider)
        for service in (first, second)
    )
    first.schedule(
        principal=ADMIN,
        approval_binding_id=approved_a["approval_binding_id"],
        activation_at=activation_at,
        idempotency_key=f"s-{time.time_ns()}",
        expected_governance_revision=governance_revision(first),
    )
    with pytest.raises(PolicyConflict):
        first.schedule(
            principal=ADMIN,
            approval_binding_id=approved_b["approval_binding_id"],
            activation_at=activation_b,
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
    activation_now = int(time.time()) + 301

    def worker(service: PolicyGovernanceService) -> None:
        try:
            results.append(
                service.process_next_policy_job(now=activation_now)
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
        thread.join(timeout=30)
        assert not thread.is_alive()

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
    _, candidate_id, _, activation_at = _full_flow(service)
    before = {
        "revision": governance_revision(service),
        "events": len(service._store.policy_governance_events),
        "audit": len(service._store.audit_events),
        "outbox": len(service._store.outbox),
        "generation": service.query_active(ADMIN)["active_generation"],
    }
    result = service.process_next_policy_job(now=activation_at)
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

    # (1b) A successful activation commits exactly one audit, one idempotency
    #      result, the activation/supersession facts and two outbox events
    #      (S08 activation + S09 final-impact consumption obligation).
    service_ok, _, _ = make_policy_service(tmp_path / "success")
    _, success_candidate, _, activation_at = _full_flow(service_ok)
    audit_before = len(service_ok._store.audit_events)
    outbox_before = len(service_ok._store.outbox)
    idem_before = len(service_ok._store.idempotency)
    worker = service_ok.process_next_policy_job(now=activation_at)
    assert worker["status"] == "complete"
    events = service_ok._store.policy_governance_events
    assert sum(e.get("kind") == "activated" for e in events) == 2  # bootstrap + ordinary
    assert sum(e.get("kind") == "superseded" for e in events) == 1
    activation_audits = [
        e for e in service_ok._store.audit_events
        if e.get("action") == "s08_activation"
    ]
    assert len(activation_audits) == 1
    assert len(service_ok._store.audit_events) == audit_before + 1
    # The activation commits two outbox facts: the S08 activation and the
    # S09 final-impact consumption obligation (the Lifecycle consumer).
    assert len(service_ok._store.outbox) == outbox_before + 2
    assert len(service_ok._store.idempotency) == idem_before + 1
    assert any(
        isinstance(binding, tuple)
        and len(binding) == 2
        and isinstance(binding[1], dict)
        and binding[1].get("activation_event_id") == worker["activation_event_id"]
        for binding in service_ok._store.idempotency.values()
    )
    assert service_ok.query_active(ADMIN)["active_generation"] == 2

    # (1c) Retroactive schedule (activation_at before trusted now) is rejected.
    with pytest.raises(PolicyInvalidTransition):
        service_ok.schedule(
            principal=ADMIN,
            approval_binding_id=service_ok.query_candidates(ADMIN)["candidates"][
                -1
            ]["approval_binding_id"],
            activation_at=1,
            idempotency_key=f"retro-{time.time_ns()}",
            expected_governance_revision=governance_revision(service_ok),
        )

    # (2) Audit unavailable during activation: prior active preserved.
    service = make(None, "fault-audit")
    service.bootstrap_once()
    _, candidate_id, _, activation_at = _full_flow(service)
    service.audit_available = False
    result = service.process_next_policy_job(now=activation_at)
    assert result["status"] == "failed"
    service.audit_available = True
    assert service.query_active(ADMIN)["active_generation"] == 1
    assert not any(
        event.get("kind") == "activated" and not event.get("bootstrap")
        for event in service._store.policy_governance_events
    )

    # (2b) Owner seam failure: a failing Audit/Outbox owner aborts the whole
    #      activation with zero protected-fact delta -- audit, outbox,
    #      idempotency, governance facts and projection all stay unchanged.
    from task4_consistency.controlled import s08 as s08_module

    service = make(None, "fault-outbox-owner")
    service.bootstrap_once()
    _, candidate_id, _, activation_at = _full_flow(service)
    before = {
        "revision": governance_revision(service),
        "events": len(service._store.policy_governance_events),
        "audit": len(service._store.audit_events),
        "outbox": len(service._store.outbox),
        "idempotency": len(service._store.idempotency),
        "generation": service.query_active(ADMIN)["active_generation"],
    }
    original_append_outbox = s08_module.AuditOutboxOwner.append_outbox

    def failing_append_outbox(owner: Any, record: dict[str, Any]) -> None:
        raise RuntimeError("outbox owner unavailable")

    s08_module.AuditOutboxOwner.append_outbox = failing_append_outbox
    try:
        result = service.process_next_policy_job(now=activation_at)
    finally:
        s08_module.AuditOutboxOwner.append_outbox = original_append_outbox
    assert result["status"] == "failed"
    assert governance_revision(service) == before["revision"]
    assert len(service._store.policy_governance_events) == before["events"]
    assert len(service._store.audit_events) == before["audit"]
    assert len(service._store.outbox) == before["outbox"]
    assert len(service._store.idempotency) == before["idempotency"]
    assert service.query_active(ADMIN)["active_generation"] == before["generation"]
    assert not any(
        event.get("kind") == "activated" and not event.get("bootstrap")
        for event in service._store.policy_governance_events
    )

    # (2c) A failing audit owner aborts identically: the idempotency result
    #      written to the staged snapshot never commits.
    service = make(None, "fault-audit-owner")
    service.bootstrap_once()
    _, candidate_id, _, activation_at = _full_flow(service)
    before = {
        "revision": governance_revision(service),
        "events": len(service._store.policy_governance_events),
        "audit": len(service._store.audit_events),
        "outbox": len(service._store.outbox),
        "idempotency": len(service._store.idempotency),
        "generation": service.query_active(ADMIN)["active_generation"],
    }
    original_append_audit = s08_module.AuditOutboxOwner.append_audit

    def failing_append_audit(owner: Any, record: dict[str, Any]) -> None:
        raise RuntimeError("audit owner unavailable")

    s08_module.AuditOutboxOwner.append_audit = failing_append_audit
    try:
        result = service.process_next_policy_job(now=activation_at)
    finally:
        s08_module.AuditOutboxOwner.append_audit = original_append_audit
    assert result["status"] == "failed"
    assert governance_revision(service) == before["revision"]
    assert len(service._store.policy_governance_events) == before["events"]
    assert len(service._store.audit_events) == before["audit"]
    assert len(service._store.outbox) == before["outbox"]
    assert len(service._store.idempotency) == before["idempotency"]
    assert service.query_active(ADMIN)["active_generation"] == before["generation"]

    # (3) Recovery release evidence loss after schedule: activation must
    #     fail closed with exact zero protected-effect delta.
    service = make(None, "fault-recovery-evidence")
    service.bootstrap_once()
    _, candidate_id, _, activation_at = _full_flow(service)
    recovery_id = service.query_active(ADMIN)["candidate_id"]
    recovery_state = service._candidate_state(
        service._store.policy_governance_events, recovery_id
    )
    before = {
        "revision": governance_revision(service),
        "events": len(service._store.policy_governance_events),
        "audit": len(service._store.audit_events),
        "outbox": len(service._store.outbox),
        "idempotency": len(service._store.idempotency),
        "generation": service.query_active(ADMIN)["active_generation"],
    }
    import sqlite3 as sqlite3_module

    with sqlite3_module.connect(service._store.state_path) as connection:
        connection.execute(
            "DELETE FROM policy_artifacts WHERE item_id = ?",
            (recovery_state["validation_bundle_id"],),
        )
        connection.execute(
            "DELETE FROM s01_immutable_catalog "
            "WHERE table_name = 'policy_artifacts' AND item_id = ?",
            (recovery_state["validation_bundle_id"],),
        )
        connection.commit()
    result = service.process_next_policy_job(now=activation_at)
    assert result["status"] == "failed", result
    assert result["reason_code"] == "S08_ACTIVATION_UNAVAILABLE"
    assert governance_revision(service) == before["revision"]
    assert len(service._store.policy_governance_events) == before["events"]
    assert len(service._store.audit_events) == before["audit"]
    assert len(service._store.outbox) == before["outbox"]
    assert len(service._store.idempotency) == before["idempotency"]
    assert service.query_active(ADMIN)["active_generation"] == before["generation"]
    assert not any(
        event.get("kind") == "activated" and not event.get("bootstrap")
        for event in service._store.policy_governance_events
    )

    # (3b) Candidate release evidence is revalidated just as completely as
    #       recovery evidence. Losing one candidate-only component after
    #       schedule leaves the prior generation and all protected facts
    #       unchanged.
    service = make(None, "fault-candidate-evidence")
    service.bootstrap_once()
    service._source_rules_path.write_bytes(
        service._source_rules_path.read_bytes().replace(
            b'version: "1.9.0"', b'version: "9.9.9"'
        )
    )
    _, candidate_id, _, activation_at = _full_flow(service)
    candidate_state = service._candidate_state(
        service._store.policy_governance_events, candidate_id
    )
    candidate_manifest = service._manifest(
        service._store, candidate_state["manifest_id"]
    )
    candidate_checker_id = next(
        item["id"]
        for item in candidate_manifest["components"]
        if item["type"] == "checker"
    )
    recovery_state = service._candidate_state(
        service._store.policy_governance_events,
        service.query_active(ADMIN)["candidate_id"],
    )
    recovery_manifest = service._manifest(
        service._store, recovery_state["manifest_id"]
    )
    recovery_checker_id = next(
        item["id"]
        for item in recovery_manifest["components"]
        if item["type"] == "checker"
    )
    assert candidate_checker_id != recovery_checker_id
    before = {
        "revision": governance_revision(service),
        "events": len(service._store.policy_governance_events),
        "audit": len(service._store.audit_events),
        "outbox": len(service._store.outbox),
        "idempotency": len(service._store.idempotency),
        "generation": service.query_active(ADMIN)["active_generation"],
    }
    with sqlite3_module.connect(service._store.state_path) as connection:
        connection.execute(
            "DELETE FROM policy_artifacts WHERE item_id = ?",
            (candidate_checker_id,),
        )
        connection.execute(
            "DELETE FROM s01_immutable_catalog "
            "WHERE table_name = 'policy_artifacts' AND item_id = ?",
            (candidate_checker_id,),
        )
        connection.commit()
    result = service.process_next_policy_job(now=activation_at)
    assert result["status"] == "failed", result
    assert result["reason_code"] == "S08_ACTIVATION_UNAVAILABLE"
    assert governance_revision(service) == before["revision"]
    assert len(service._store.policy_governance_events) == before["events"]
    assert len(service._store.audit_events) == before["audit"]
    assert len(service._store.outbox) == before["outbox"]
    assert len(service._store.idempotency) == before["idempotency"]
    assert service.query_active(ADMIN)["active_generation"] == before["generation"]

    # (4) Injected partial commit during validation: no bundle, no event,
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
    # S09 approval contract: every approval binds the immutable impact
    # preview; S08-only flows over the governed fixture wire the read-only
    # empty Lifecycle snapshot seam.
    policy._lifecycle_snapshot_provider = _empty_impact_snapshot
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
    _, candidate_id, _, activation_at = _full_flow(policy, activation_delay=0)
    policy.process_next_policy_job(now=activation_at)
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

    # The validation evidence is bound to the bundle: complete corpus
    # digest, fresh-process determinism runs and declared checker limits.
    candidates = policy.query_candidates(ADMIN)["candidates"]
    active_candidate = next(
        item for item in candidates if item["candidate_id"] == candidate_id
    )
    bundle = validation_bundle(policy, active_candidate["validation_bundle_id"])
    assert bundle["status"] == "validated"
    corpus_manifest = bundle["inputs"]["corpus"]
    assert (
        corpus_manifest["digest"]
        == bundle["results"]["corpus_diff"]["corpus_digest"]
    )
    assert bundle["results"]["corpus_diff"]["applications_skipped"] == 0
    assert (
        corpus_manifest["count"]
        == bundle["results"]["corpus_diff"]["applications_compared"]
    )
    assert bundle["results"]["determinism"]["runs"] == 2
    assert bundle["results"]["determinism"]["equal"] is True
    checker_component = next(
        item for item in active["components"] if item["type"] == "checker"
    )
    checker = policy._artifact(policy._store, checker_component["id"])
    assert dict(checker["limits"]) == dict(spec["limits"])
    assert (
        bundle["inputs"]["component_digests"]["checker"]
        == checker_component["digest"]
    )

    # Every governed field is a Ledger/Registry pin, not caller metadata.
    # Changing an activation fact, generation, or component list must be
    # rejected before the checker can materialize.
    forged_specs: list[dict[str, Any]] = []
    forged = copy.deepcopy(spec)
    forged["activation_event_id"] = "governance_forged"
    forged_specs.append(forged)
    forged = copy.deepcopy(spec)
    forged["active_generation"] = 999
    forged_specs.append(forged)
    forged = copy.deepcopy(spec)
    forged["components"][0] = {
        **forged["components"][0],
        "id": "artifact_sha256_" + "0" * 64,
        "digest": "0" * 64,
    }
    forged_specs.append(forged)
    for field, value in (
        ("release_id", "forged-release"),
        ("release_digest", "0" * 64),
        ("checker_build", "forged-checker"),
    ):
        forged = copy.deepcopy(spec)
        forged[field] = value
        forged_specs.append(forged)
    forged = copy.deepcopy(spec)
    forged["baseline_release"] = {
        **forged["baseline_release"],
        "knowledge_digest": "0" * 64,
    }
    forged_specs.append(forged)
    forged = copy.deepcopy(spec)
    forged["limits"] = {**forged["limits"], "max_findings": 1}
    forged_specs.append(forged)
    forged = copy.deepcopy(spec)
    forged["applicable_check_ids"] = list(reversed(forged["applicable_check_ids"]))
    forged_specs.append(forged)
    for forged_spec in forged_specs:
        with pytest.raises(PolicyUnavailable):
            policy.load_pinned_release(forged_spec)


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

    # Delete the active manifest row (and its catalog seal) behind the
    # service's back: resolution must fail closed instead of resolving a
    # projection or falling back to files.
    import sqlite3 as sqlite3_module

    policy._store.reload()
    active = policy._store.policy_active_projections[S08_SCOPE]
    manifest_id = active["manifest_id"]
    with sqlite3_module.connect(policy._store.state_path) as connection:
        connection.execute(
            "DELETE FROM policy_manifests WHERE item_id = ?",
            (manifest_id,),
        )
        connection.execute(
            "DELETE FROM s01_immutable_catalog WHERE table_name = 'policy_manifests' "
            "AND item_id = ?",
            (manifest_id,),
        )
        connection.commit()

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
    # The ledger is not polluted (generation 1), but the corrupted Registry
    # can no longer produce a verifiable active projection: the query fails
    # closed instead of returning an unverifiable warm object.
    folded = policy._fold_active_projection(
        policy._store.policy_governance_events, S08_SCOPE
    )
    assert folded is not None
    assert folded["active_generation"] == 1
    with pytest.raises(PolicyUnavailable):
        policy.query_active(ADMIN)


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
    tmp_path: Path,
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

    service, policy, rules_path, _ = _governed_s01(tmp_path)
    pinned = policy.resolve_run_pin(S08_SCOPE, int(time.time()))
    poisoned = tmp_path / "poisoned.json"
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


# --- Fix brief G1: runtime authority, projection rebuild, pinned content ----

def test_active_projection_rebuilds_from_ledger_after_restart(
    tmp_path: Path,
) -> None:
    """The active projection is a rebuildable cache: tampering with the
    mutable projection must never change what the resolver returns, and a
    restart folds the same active generation from the append-only Ledger."""
    service, policy, rules_path, _ = _governed_s01(tmp_path)
    before = policy.query_active(ADMIN)
    assert before["status"] == "active"
    assert before["active_generation"] == 1

    # Tamper with the mutable projection row (a rebuildable cache, not an
    # owner); the ledger must still win.
    policy._store.reload()
    policy._store.policy_active_projections[S08_SCOPE]["manifest_id"] = (
        "manifest_sha256_" + "0" * 64
    )
    policy._store.policy_active_projections[S08_SCOPE]["active_generation"] = 99
    policy._store.persist()

    pin = policy.resolve_run_pin(S08_SCOPE, int(time.time()))
    assert pin is not None
    assert pin["active_generation"] == 1
    assert pin["manifest_digest"] == before["manifest_digest"]
    assert pin["activation_event_id"] == before["activation_event_id"]

    # A fresh service folds the identical active generation from the ledger.
    restarted = PolicyGovernanceService(
        state_path=service._store.state_path,
        source_rules_path=DEFAULT_RULES,
        source_kb_path=DEFAULT_KB,
        corpus_root=CORPUS,
    )
    restarted_pin = restarted.resolve_run_pin(S08_SCOPE, int(time.time()))
    assert restarted_pin is not None
    assert restarted_pin["active_generation"] == pin["active_generation"]
    assert restarted_pin["manifest_digest"] == pin["manifest_digest"]
    assert restarted_pin["activation_event_id"] == pin["activation_event_id"]
    assert restarted_pin["components"] == pin["components"]

    # Deleting the projection row outright must not flip a governed runtime
    # back to the legacy oracle at public S01 admission: the Ledger keeps
    # resolution stable and the legacy-oracle spy stays untouched.
    import sqlite3 as sqlite3_module

    from types import SimpleNamespace

    from task4_consistency.controlled.s01 import (
        ControlledScenarioService as S01Service,
    )

    with sqlite3_module.connect(policy._store.state_path) as connection:
        connection.execute("DELETE FROM policy_active_projections")
        connection.commit()
    ledger_pin = policy.resolve_run_pin(S08_SCOPE, int(time.time()))
    assert ledger_pin is not None
    assert ledger_pin["active_generation"] == pin["active_generation"]
    assert ledger_pin["manifest_digest"] == pin["manifest_digest"]

    oracle_calls: list[Any] = []

    def spy_oracle(application: Any) -> Any:
        oracle_calls.append(application)
        return SimpleNamespace(checks=[])

    admitted_service = S01Service(
        fixture_root=service.fixture_root,
        rules_path=rules_path,
        state_path=policy._store.state_path,
        policy_governance=policy,
        legacy_oracle_runner=spy_oracle,
    )
    admitted_app = _s01_admit(admitted_service, "s08-projection-deleted-admit")
    assert oracle_calls == []
    completed = admitted_service.process_next_job()
    assert completed.status == "complete", completed
    assert oracle_calls == []
    admitted_run = next(
        item
        for item in admitted_service._store.runs
        if item.get("run_id") == completed.run_id
    )
    assert admitted_run["spec"]["active_generation"] == pin["active_generation"]
    assert admitted_run["spec"]["manifest_digest"] == pin["manifest_digest"]


def test_warmed_pinned_checker_revalidates_registry(
    tmp_path: Path,
) -> None:
    """The checker cache is a non-authoritative accelerator: after the
    Registry artifact is deleted, both fresh and warmed services must fail
    closed instead of returning a warm object."""
    import sqlite3 as sqlite3_module

    service, policy, _, _ = _governed_s01(tmp_path)
    app_id = _s01_admit(service, "s08-warm-checker-1")
    result = service.process_next_job()
    assert result.status == "complete", result
    run_record = next(
        item
        for item in service._store.runs
        if item.get("run_id") == result.run_id
    )
    run_spec = run_record["spec"]
    # Warm the checker cache.
    warmed = policy.load_pinned_checker(run_spec)
    assert warmed is not None

    # Delete the checker artifact row (and its catalog seal) behind the
    # service's back, simulating registry corruption.
    policy._store.reload()
    manifest = next(
        item
        for item in policy._store.policy_manifests
        if item["manifest_id"] == run_spec["manifest_id"]
    )
    checker_entry = next(
        item for item in manifest["components"] if item["type"] == "checker"
    )
    with sqlite3_module.connect(policy._store.state_path) as connection:
        connection.execute(
            "DELETE FROM policy_artifacts WHERE item_id = ?",
            (checker_entry["id"],),
        )
        connection.execute(
            "DELETE FROM s01_immutable_catalog WHERE table_name = 'policy_artifacts' "
            "AND item_id = ?",
            (checker_entry["id"],),
        )
        connection.commit()

    # The warmed service must revalidate the registry before using the cache.
    with pytest.raises(PolicyUnavailable):
        policy.load_pinned_checker(run_spec)
    # A fresh service must fail closed identically.
    fresh = PolicyGovernanceService(
        state_path=service._store.state_path,
        source_rules_path=DEFAULT_RULES,
        source_kb_path=DEFAULT_KB,
        corpus_root=CORPUS,
    )
    with pytest.raises(PolicyUnavailable):
        fresh.load_pinned_checker(run_spec)
    # Resolution itself fails closed after corruption: no partial or current
    # release can be produced from the missing artifact.
    with pytest.raises(PolicyUnavailable):
        fresh.resolve_run_pin(S08_SCOPE, int(time.time()))


def test_s08_never_appends_audit_or_outbox_collections_directly() -> None:
    """G5 architecture seam: S08 submits audit/outbox records to the
    AuditOutboxOwner seam and never appends another module's collections
    directly."""
    source = (ROOT / "task4_consistency" / "controlled" / "s08.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "append":
            value = node.value
            if isinstance(value, ast.Attribute) and value.attr in {
                "audit_events",
                "outbox",
            }:
                violations.append((value.attr, node.lineno))
    assert violations == [], violations


_COMPONENT_TYPES = [
    "check_policy",
    "semantic_catalog",
    "entity_knowledge",
    "normalization_policy",
    "comparison_policy",
    "readiness_policy",
    "operators",
    "normalizers",
    "checker",
    "input_contract",
    "limits",
]


@pytest.mark.parametrize("target", [*_COMPONENT_TYPES, "validation_bundle", "approval_binding"])
@pytest.mark.parametrize("mode", ["deleted", "drifted"])
def test_pinned_artifact_loss_or_drift_fails_closed_fresh_and_warm(
    tmp_path: Path, target: str, mode: str
) -> None:
    """Deleting or content-drifting any manifest component or the bound
    validation/approval artifact fails closed for both fresh and warmed
    services: resolver and pinned-release loader reject, and a new worker
    run stops with the machine-verifiable pinned-release contract without
    publishing findings."""
    import sqlite3 as sqlite3_module

    from task4_consistency.controlled.s01_store import _encode, _integrity_digest

    service, policy, _, _ = _governed_s01(tmp_path)
    _, candidate_id, _, activation_at = _full_flow(policy, activation_delay=0)
    policy.process_next_policy_job(now=activation_at)
    active = policy.query_active(ADMIN)
    assert active["active_generation"] == 2
    spec = policy.resolve_run_pin(S08_SCOPE, int(time.time()))
    assert spec is not None
    # Warm the loader before the tamper.
    policy.load_pinned_release(spec)
    # One public admission queues a worker job but nothing runs yet.
    app_id = _s01_admit(service, f"pin-loss-{target}-{mode}")

    policy._store.reload()
    manifest = next(
        item
        for item in policy._store.policy_manifests
        if item["manifest_id"] == spec["manifest_id"]
    )
    if target == "validation_bundle":
        artifact_id = spec["validation_bundle_id"]
    elif target == "approval_binding":
        artifact_id = spec["approval_binding_id"]
    else:
        artifact_id = next(
            item["id"]
            for item in manifest["components"]
            if item["type"] == target
        )
    with sqlite3_module.connect(policy._store.state_path) as connection:
        row = connection.execute(
            "SELECT payload FROM policy_artifacts WHERE item_id = ?",
            (artifact_id,),
        ).fetchone()
        assert row is not None, artifact_id
        if mode == "deleted":
            connection.execute(
                "DELETE FROM policy_artifacts WHERE item_id = ?", (artifact_id,)
            )
            connection.execute(
                "DELETE FROM s01_immutable_catalog "
                "WHERE table_name = 'policy_artifacts' AND item_id = ?",
                (artifact_id,),
            )
        else:
            original = json.loads(row[0])
            # Keep the item shape intact (S01 persists the shared store);
            # mutate only the canonical content so the artifact digest no
            # longer verifies against its pinned component digest.
            if original.get("canonical_json"):
                mutated_content = json.loads(original["canonical_json"])
                mutated_content["tampered"] = True
                original = {
                    **original,
                    "canonical_json": json.dumps(
                        mutated_content,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                }
            else:
                original = {
                    **original,
                    "raw_hex": "00" + original.get("raw_hex", "00")[2:],
                }
            mutated = _encode(original)
            integrity = _integrity_digest("policy_artifacts", artifact_id, mutated)
            connection.execute(
                "UPDATE policy_artifacts SET payload = ?, integrity_sha256 = ? "
                "WHERE item_id = ?",
                (mutated, integrity, artifact_id),
            )
            connection.execute(
                "UPDATE s01_immutable_catalog SET integrity_sha256 = ? "
                "WHERE table_name = 'policy_artifacts' AND item_id = ?",
                (integrity, artifact_id),
            )
        connection.commit()

    # Fresh resolver and loader fail closed.
    with pytest.raises(PolicyUnavailable):
        policy.resolve_run_pin(S08_SCOPE, int(time.time()))
    with pytest.raises(PolicyUnavailable):
        policy.load_pinned_release(spec)
    # The warmed loader revalidates the registry and still fails closed.
    with pytest.raises(PolicyUnavailable):
        policy.load_pinned_release(spec)
    # A fresh service fails identically.
    fresh = PolicyGovernanceService(
        state_path=policy._store.state_path,
        source_rules_path=policy._source_rules_path,
        source_kb_path=policy._source_kb_path,
        corpus_root=CORPUS,
    )
    with pytest.raises(PolicyUnavailable):
        fresh.resolve_run_pin(S08_SCOPE, int(time.time()))
    with pytest.raises(PolicyUnavailable):
        fresh.load_pinned_release(spec)
    # The queued worker run stops with the pinned-release contract; no
    # findings and no current/partial run are published.
    stopped = service.process_next_job()
    assert stopped.status == "stopped", stopped
    assert stopped.reason_code == "PINNED_RELEASE_UNAVAILABLE"
    assert not any(
        finding.get("application_id") == app_id
        for finding in service._store.findings
    )
    assert not any(
        run.get("application_id") == app_id and run.get("status") == "complete"
        for run in service._store.runs
    )


def test_required_dependency_loss_blocks_new_run_resolution(tmp_path: Path) -> None:
    """Restarting an active store with audit or storage unavailable must
    fail closed for new pin resolution and pinned release loads, with no
    fallback to bootstrap/prior and no ledger mutation."""
    service, policy, _, _ = _governed_s01(tmp_path)
    assert policy.resolve_run_pin(S08_SCOPE, int(time.time())) is not None
    ledger_before = len(policy._store.policy_governance_events)

    restarted = PolicyGovernanceService(
        state_path=policy._store.state_path,
        source_rules_path=policy._source_rules_path,
        source_kb_path=policy._source_kb_path,
        corpus_root=CORPUS,
        audit_available=False,
        storage_available=False,
    )
    with pytest.raises(PolicyUnavailable):
        restarted.resolve_run_pin(S08_SCOPE, int(time.time()))
    # No fallback, no ledger mutation.
    assert len(restarted._store.policy_governance_events) == ledger_before

    audit_only = PolicyGovernanceService(
        state_path=policy._store.state_path,
        source_rules_path=policy._source_rules_path,
        source_kb_path=policy._source_kb_path,
        corpus_root=CORPUS,
        audit_available=False,
    )
    with pytest.raises(PolicyUnavailable):
        audit_only.resolve_run_pin(S08_SCOPE, int(time.time()))

    storage_only = PolicyGovernanceService(
        state_path=policy._store.state_path,
        source_rules_path=policy._source_rules_path,
        source_kb_path=policy._source_kb_path,
        corpus_root=CORPUS,
        storage_available=False,
    )
    with pytest.raises(PolicyUnavailable):
        storage_only.resolve_run_pin(S08_SCOPE, int(time.time()))
    assert len(storage_only._store.policy_governance_events) == ledger_before

    # Storage loss fences commands before idempotency replay or mutation.
    revision_before = len(storage_only._store.policy_governance_events)
    with pytest.raises(PolicyUnavailable):
        storage_only.import_legacy(
            principal=ADMIN,
            source_bundle_id=SOURCE_BUNDLE_ID,
            idempotency_key=f"storage-loss-{time.time_ns()}",
            expected_governance_revision=revision_before,
        )
    assert len(storage_only._store.policy_governance_events) == revision_before

    # Compatibility loads independently require each authority boundary.
    active = policy.query_active(ADMIN)
    release = policy.resolve_run_pin(S08_SCOPE, int(time.time()))["release"]
    compat_spec = {
        "release_id": release["release_id"],
        "release_digest": release["digest"],
        "checker_build": release["checker_build"],
    }
    assert active["bootstrap"] is True
    for unavailable in (
        {"audit_available": False},
        {"storage_available": False},
    ):
        compat_service = PolicyGovernanceService(
            state_path=policy._store.state_path,
            source_rules_path=policy._source_rules_path,
            source_kb_path=policy._source_kb_path,
            corpus_root=CORPUS,
            **unavailable,
        )
        with pytest.raises(PolicyUnavailable):
            compat_service.load_compat_release(compat_spec)

    # A queued activation is not claimed when storage trust is lost; all
    # protected effects remain at delta zero.
    activation_service, _, _ = make_policy_service(tmp_path / "storage-job")
    _, _, _, activation_at = _full_flow(activation_service, activation_delay=60)
    protected_before = {
        "revision": governance_revision(activation_service),
        "audit": len(activation_service._store.audit_events),
        "outbox": len(activation_service._store.outbox),
        "idempotency": len(activation_service._store.idempotency),
        "generation": activation_service.query_active(ADMIN)["active_generation"],
    }
    activation_service.storage_available = False
    blocked = activation_service.process_next_policy_job(now=activation_at)
    assert blocked["status"] == "failed", blocked
    activation_service.storage_available = True
    assert governance_revision(activation_service) == protected_before["revision"]
    assert len(activation_service._store.audit_events) == protected_before["audit"]
    assert len(activation_service._store.outbox) == protected_before["outbox"]
    assert len(activation_service._store.idempotency) == protected_before["idempotency"]
    assert (
        activation_service.query_active(ADMIN)["active_generation"]
        == protected_before["generation"]
    )

    # Bootstrap is a protected governance write and cannot run on an
    # untrusted storage boundary.
    blocked_bootstrap = PolicyGovernanceService(
        state_path=tmp_path / "blocked-bootstrap.sqlite3",
        source_rules_path=policy._source_rules_path,
        source_kb_path=policy._source_kb_path,
        corpus_root=CORPUS,
        storage_available=False,
    )
    bootstrap_result = blocked_bootstrap.bootstrap_once()
    assert bootstrap_result == {
        "status": "blocked",
        "reason_code": "STORAGE_UNAVAILABLE",
    }
    assert blocked_bootstrap._store.policy_governance_events == []
    assert blocked_bootstrap._store.audit_events == []


def test_validator_build_change_invalidates_prior_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outdated candidate evidence cannot be reviewed, approved or activated;
    already-activated releases remain resolvable by their historical pins."""
    import task4_consistency.controlled.s08 as s08_module

    current_build = s08_module.VALIDATOR_BUILD
    service, rules_path, _ = make_policy_service(tmp_path / "validator-build")
    module_root = Path(s08_module.__file__).resolve().parent
    validator_sources = sorted(
        module_root / name
        for name in ("s01_checker.py", "s08.py", "s08_validate.py")
    )
    expected_code_digest = content_digest(
        [
            (path.name, hashlib.sha256(path.read_bytes()).hexdigest())
            for path in validator_sources
        ]
    )
    assert s08_module._VALIDATOR_CODE_DIGEST == expected_code_digest

    historical_spec = service.resolve_run_pin(S08_SCOPE, int(time.time()))
    assert historical_spec is not None
    current_code_digest = s08_module._VALIDATOR_CODE_DIGEST
    monkeypatch.setattr(s08_module, "VALIDATOR_BUILD", "s08-validator/next")
    monkeypatch.setattr(s08_module, "_VALIDATOR_CODE_DIGEST", "a" * 64)
    assert service.resolve_run_pin(S08_SCOPE, int(time.time())) is not None
    assert (
        service.load_pinned_release(historical_spec).release_id
        == historical_spec["release"]["release_id"]
    )
    monkeypatch.setattr(s08_module, "VALIDATOR_BUILD", current_build)
    monkeypatch.setattr(
        s08_module, "_VALIDATOR_CODE_DIGEST", current_code_digest
    )

    draft_id = import_draft(service)
    candidate_id, worker = freeze_and_validate(service, draft_id)
    assert worker["outcome"] == "validated"
    candidates = service.query_candidates(ADMIN)["candidates"]
    candidate = next(
        item for item in candidates if item["candidate_id"] == candidate_id
    )
    bundle_before_id = candidate["validation_bundle_id"]
    bundle_before = validation_bundle(service, bundle_before_id)
    assert bundle_before["validator_build"] == s08_module.VALIDATOR_BUILD
    assert bundle_before["validator"]["code_sha256"]
    assert bundle_before["results"]["raw_outcomes"] is not None
    raw_before = bundle_before["results"]["raw_outcomes"]
    anchor_digest = raw_before["runs"]["anchor"]["outcome_set_digest"]
    assert raw_before["outcome_sets"][anchor_digest]

    service.submit_review(
        principal=ADMIN,
        candidate_id=candidate_id,
        idempotency_key=f"vb-review-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    state = service._candidate_state(
        service._store.policy_governance_events, candidate_id
    )

    # The recorded validator code digest is an enforced identity, not
    # metadata: review and approval both reject evidence from different code
    # even when the suite/build labels stay unchanged.
    monkeypatch.setattr(s08_module, "_VALIDATOR_CODE_DIGEST", "f" * 64)
    with pytest.raises(PolicyUnavailable):
        service._review_material(service._store, state, S08_SCOPE)
    with pytest.raises(PolicyUnavailable):
        preview_and_approve(
            service,
            candidate_id=candidate_id,
            activation_time=int(time.time()) + 60,
            recovery_release_id=service.query_active(ADMIN)["candidate_id"],
            idempotency_key=f"code-approve-{time.time_ns()}",
        )
    monkeypatch.setattr(
        s08_module, "_VALIDATOR_CODE_DIGEST", current_code_digest
    )

    # The validator build changes; the old evidence is invalid.
    monkeypatch.setattr(s08_module, "VALIDATOR_BUILD", "s08-validator/99")
    with pytest.raises(PolicyUnavailable):
        service._review_material(service._store, state, S08_SCOPE)
    with pytest.raises(PolicyUnavailable):
        preview_and_approve(
            service,
            candidate_id=candidate_id,
            activation_time=int(time.time()) + 60,
            recovery_release_id=service.query_active(ADMIN)["candidate_id"],
            idempotency_key=f"vb-approve-{time.time_ns()}",
        )
    assert service.query_active(ADMIN)["active_generation"] == 1

    # A fresh candidate (behavior-equivalent version drift) validated
    # under the changed build carries the new build identity in a distinct
    # bundle.
    rules_path.write_bytes(
        rules_path.read_bytes().replace(
            b'version: "1.9.0"', b'version: "9.9.9"'
        )
    )
    second_draft = import_draft(service)
    second_candidate, second_worker = freeze_and_validate(service, second_draft)
    assert second_worker["outcome"] == "validated"
    bundle_second = validation_bundle(
        service, second_worker["validation_bundle_id"]
    )
    assert bundle_second["validator_build"] == "s08-validator/99"
    assert second_worker["validation_bundle_id"] != bundle_before_id

    # After the current build returns, the build-99 evidence remains
    # ineligible for review.
    monkeypatch.setattr(s08_module, "VALIDATOR_BUILD", current_build)
    state_second = service._candidate_state(
        service._store.policy_governance_events, second_candidate
    )
    with pytest.raises(PolicyUnavailable):
        service._review_material(service._store, state_second, S08_SCOPE)
    assert service.query_active(ADMIN)["active_generation"] == 1

    # Code identity is rechecked again at the activation linearization point.
    activation_service, _, _ = make_policy_service(tmp_path / "validator-code")
    _, _, _, activation_at = _full_flow(
        activation_service, activation_delay=60
    )
    monkeypatch.setattr(s08_module, "_VALIDATOR_CODE_DIGEST", "e" * 64)
    activation = activation_service.process_next_policy_job(now=activation_at)
    assert activation["status"] == "failed", activation
    monkeypatch.setattr(
        s08_module, "_VALIDATOR_CODE_DIGEST", current_code_digest
    )
    assert activation_service.query_active(ADMIN)["active_generation"] == 1


def test_fresh_process_bounds_reject_without_terminating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bounded child probes: cardinality breach, runtime breach, output
    flood and a skipped corpus entry all terminate promptly and reject;
    the memory/process boundary is enforced inside the child process."""
    import subprocess as subprocess_module
    import sys as sys_module

    from dataclasses import replace
    from task4_consistency.controlled import s08 as s08_module

    service, rules_path, kb_path = make_policy_service(tmp_path / "bounds")
    release = service.resolve_run_pin(S08_SCOPE, int(time.time()))["release"][
        "target_release"
    ]
    assert release is not None

    # (a) Cardinality breach: a corpus beyond the item cap is rejected by
    #     the child; tiny skipped fixtures keep the probe fast.
    tiny = {"application_id": "APP-TINY", "documents": []}
    started = time.monotonic()
    outcome = service._run_fresh_process(
        release, [{"fixture": tiny}] * 5001
    )
    assert outcome is None
    assert time.monotonic() - started < 30.0

    # (b) Runtime breach: a valid two-document fuzzy comparison has finite
    #     but deliberately expensive material. The 10ms release deadline
    #     interrupts it during execution, rather than timing it after return.
    heavy_fixture = {
        "application_id": "APP-HEAVY",
        "documents": [
            {
                "doc_id": "heavy-registration",
                "doc_type": "机动车登记证书",
                "fields": {"owner_name": "ab" * 2_000_000 + "x"},
            },
            {
                "doc_id": "heavy-policy",
                "doc_type": "交强险保单",
                "fields": {"owner_name": "ba" * 2_000_000 + "y"},
            },
        ],
    }
    slow_release = replace(
        release,
        limits=(("max_documents", 20), ("max_findings", 100), ("max_runtime_ms", 10)),
    )
    started = time.monotonic()
    outcome = service._run_fresh_process(
        slow_release, [{"fixture": heavy_fixture}]
    )
    assert outcome is None
    assert time.monotonic() - started < 1.0

    # (c) Output flood: lower only the parent transport cap, then run a valid
    #     one-document fixture. It reaches stdout streaming without crossing
    #     document, finding or input cardinality first.
    flood_fixture = {
        "application_id": "APP-FLOOD",
        "documents": [
            {
                "doc_id": "flood-registration",
                "doc_type": "机动车登记证书",
                "fields": {"vin": "LSVAA4182N2444555"},
            }
        ],
    }
    started = time.monotonic()
    with monkeypatch.context() as bounded:
        bounded.setattr(s08_module, "_MAX_SUBPROCESS_STDOUT_BYTES", 1)
        outcome = service._run_fresh_process(
            release, [{"fixture": flood_fixture}]
        )
    assert outcome is None
    assert time.monotonic() - started < 30.0

    # (d) The child enforces a finite memory/process boundary: verified in a
    #     throwaway subprocess so the test process is never constrained.
    boundary_probe = subprocess_module.run(
        [
            sys_module.executable,
            "-c",
            "import resource;"
            "from task4_consistency.controlled.s08_validate import "
            "_apply_process_boundaries;"
            "_apply_process_boundaries();"
            "print(resource.getrlimit(resource.RLIMIT_AS)[0])",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
        cwd=str(ROOT),
    )
    assert int(boundary_probe.stdout.strip()) <= 512 * 1024 * 1024

    # (e) A skipped corpus entry produces rejected evidence at validation
    #     time with the active generation unchanged.  The bootstrap anchors
    #    on the healthy corpus; the served corpus then switches to the
    #     skipped entry.
    skipped_root = tmp_path / "skipped-corpus"
    skipped_root.mkdir()
    (skipped_root / "skip.json").write_text(
        json.dumps({"application_id": "APP-SKIP", "documents": []}),
        encoding="utf-8",
    )
    service2, _, _ = make_policy_service(tmp_path / "skipped")
    service2._corpus_root = skipped_root
    draft2 = import_draft(service2)
    candidate2, worker2 = freeze_and_validate(service2, draft2)
    assert worker2["outcome"] == "rejected"
    bundle2 = validation_bundle(service2, worker2["validation_bundle_id"])
    outcomes2 = check_outcomes(bundle2)
    assert outcomes2["corpus_zero_diff"] == "fail"
    assert bundle2["results"]["corpus_diff"]["applications_skipped"] >= 1
    assert service2.query_active(ADMIN)["active_generation"] == 1


def test_workspace_projects_validation_rejected_and_keeps_prior_active(
    tmp_path: Path,
) -> None:
    """A candidate whose validation the owner rejects is presented with the
    authoritative terminal outcome: status rejected, no actions, the
    registered rejection reason and the rejection evidence visible, and the
    prior active anchor untouched."""
    service, _, _ = make_policy_service(tmp_path)
    draft_id = import_draft(service)
    service._corpus_root = tmp_path / "missing-corpus"
    candidate_id, worker = freeze_and_validate(service, draft_id)
    assert worker["outcome"] == "rejected"
    workspace = service.query_candidate_workspace(ADMIN, candidate_id)
    assert workspace["status"] == "rejected"
    assert workspace["actions"] == []
    assert workspace["validation_outcome"] == {
        "status": "rejected",
        "reason_code": "S08_VALIDATION_REJECTED",
    }
    assert workspace["activation_outcome"] is None
    assert workspace["validation_bundle"]["status"] == "rejected"
    assert workspace["active_anchor"] is not None
    assert workspace["active_anchor"]["candidate_id"] != candidate_id
    assert service.query_active(ADMIN)["active_generation"] == 1


def test_workspace_keeps_validation_verdict_after_approver_rejection(
    tmp_path: Path,
) -> None:
    service, _, _ = make_policy_service(tmp_path)
    candidate_id, worker = freeze_and_validate(service, import_draft(service))
    assert worker["outcome"] == "validated"

    service.reject(
        principal=APPROVER,
        candidate_id=candidate_id,
        reason_code="S08_REVIEW_REJECTED",
        idempotency_key="approver-reject-after-validation",
        expected_governance_revision=governance_revision(service),
    )

    workspace = service.query_candidate_workspace(ADMIN, candidate_id)
    assert workspace["status"] == "rejected"
    assert workspace["validation_outcome"] == {
        "status": "validated",
        "reason_code": "S08_VALIDATION_PASSED",
    }
    assert workspace["events"][-1]["reason_code"] == "S08_REVIEW_REJECTED"


def test_workspace_projects_activation_diagnostic_failure_and_prior_active(
    tmp_path: Path,
) -> None:
    """An activation worker failure is presented as an authoritative terminal
    activation outcome: the failed candidate never becomes active, the
    prior-active anchor stays visible, and no new active generation exists."""
    bundle = tmp_path / "server-bundle"
    bundle.mkdir()
    (bundle / "rules.yaml").write_bytes(DEFAULT_RULES.read_bytes())
    (bundle / "entity_kb.json").write_bytes(DEFAULT_KB.read_bytes())

    def inject(write_point: str) -> None:
        if write_point == "s08.activation":
            raise OSError("injected S08 fault")

    service = PolicyGovernanceService(
        state_path=tmp_path / "fault.sqlite3",
        source_rules_path=bundle / "rules.yaml",
        source_kb_path=bundle / "entity_kb.json",
        corpus_root=CORPUS,
        fault_injector=inject,
    )
    service.bootstrap_once()
    _, candidate_id, _, activation_at = _full_flow(service)
    prior = service.query_active(ADMIN)
    result = service.process_next_policy_job(now=activation_at)
    assert result["status"] == "failed"
    workspace = service.query_candidate_workspace(ADMIN, candidate_id)
    assert workspace["status"] == "scheduled"
    assert workspace["actions"] == ["cancel"]
    assert workspace["validation_outcome"] == {
        "status": "validated",
        "reason_code": "S08_VALIDATION_PASSED",
    }
    assert workspace["activation_outcome"]["status"] == "failed"
    assert (
        workspace["activation_outcome"]["reason_code"] == "S08_ACTIVATION_INTERNAL"
    )
    assert workspace["active_anchor"]["candidate_id"] == prior["candidate_id"]
    assert workspace["active_anchor"]["manifest_digest"] == prior["manifest_digest"]
    assert service.query_active(ADMIN)["active_generation"] == 1
    assert workspace["activation_outcome"].get("activation_event_id") is None


def test_workspace_query_is_one_atomic_snapshot_across_interleaved_commit(
    tmp_path: Path,
) -> None:
    """A worker transition that commits to the store while the workspace is
    being read can never split the projection: the response is the exact
    snapshot taken under the single service lock, with status, actions,
    revision, events and active anchor mutually consistent."""
    state_path = tmp_path / "atomic.sqlite3"
    service, rules_path, kb_path = make_policy_service(
        tmp_path / "atomic", state_path=state_path
    )
    _, candidate_id, _, activation_at = _full_flow(service)
    before = service.query_candidate_workspace(ADMIN, candidate_id)
    assert before["status"] == "scheduled"
    assert before["actions"] == ["cancel"]
    assert before["activation_outcome"]["status"] == "pending"
    assert before["active_anchor"] is not None
    assert before["active_anchor"]["candidate_id"] != candidate_id

    original_reload = service._store.reload
    injected = {"done": False}
    other = PolicyGovernanceService(
        state_path=state_path,
        source_rules_path=rules_path,
        source_kb_path=kb_path,
        corpus_root=CORPUS,
    )
    other._lifecycle_snapshot_provider = _empty_impact_snapshot

    def reload_with_interleaved_commit() -> None:
        original_reload()
        if not injected["done"]:
            injected["done"] = True
            # The concurrent worker lands its activation commit between the
            # query's reload and the projection construction.
            other.process_next_policy_job(now=int(time.time()) + 120)

    service._store.reload = reload_with_interleaved_commit  # type: ignore[method-assign]
    snapshot = service.query_candidate_workspace(ADMIN, candidate_id)
    # The response is the pre-commit frame: everything still self-consistent.
    assert snapshot["status"] == "scheduled"
    assert snapshot["actions"] == ["cancel"]
    assert snapshot["governance_revision"] == before["governance_revision"]
    assert len(snapshot["events"]) == len(before["events"])
    assert snapshot["active_anchor"] == before["active_anchor"]
    assert snapshot["activation_outcome"]["status"] == "pending"
    # The next frame (a fresh reload) observes the interleaved commit.
    service._store.reload = original_reload
    after = service.query_candidate_workspace(ADMIN, candidate_id)
    assert after["status"] == "active"
    assert after["actions"] == []
    assert after["activation_outcome"]["status"] == "active"
    assert after["activation_outcome"]["active_generation"] == 2
    assert after["active_anchor"]["candidate_id"] == candidate_id
    assert after["governance_revision"] > snapshot["governance_revision"]


def test_workspace_projects_terminal_outcomes_for_superseded_and_cancelled(
    tmp_path: Path,
) -> None:
    """Append-only ledger verdicts keep the authoritative terminal outcome
    honest after the candidate moves on: a superseded candidate still reports
    the activation that happened, and a cancelled-but-validated candidate
    still reports the validation verdict."""
    service, _, _ = make_policy_service(tmp_path)
    _, first_candidate, _, activation_at = _full_flow(service)
    assert (
        service.process_next_policy_job(now=activation_at)["status"]
        == "complete"
    )
    # A successor activation supersedes the first release.  The frozen draft
    # forks on revise, so the second candidate must be built from the
    # revised draft identity exactly like the overlapping-schedule drill.
    second_draft = import_draft(service)
    second_revise = service.revise_draft(
        principal=ADMIN,
        draft_id=second_draft,
        metadata={
            "scope": S08_SCOPE,
            "validity": {"valid_from": "2000-01-01T00:00:00Z"},
            "source": SOURCE_BUNDLE_ID,
            "reason": "supersede case",
        },
        idempotency_key=f"r-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    second_freeze = service.freeze_candidate(
        principal=ADMIN,
        draft_id=second_revise["draft_id"],
        idempotency_key=f"f-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    second_candidate = second_freeze["candidate_id"]
    service.request_validation(
        principal=ADMIN,
        candidate_id=second_candidate,
        idempotency_key=f"v-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    service.process_next_policy_job()
    service.submit_review(
        principal=ADMIN,
        candidate_id=second_candidate,
        idempotency_key=f"rv-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    second_at = int(time.time()) + 30
    second_approval = preview_and_approve(
        service,
        candidate_id=second_candidate,
        activation_time=second_at,
        recovery_release_id=service.query_active(ADMIN)["candidate_id"],
        idempotency_key=f"a-{time.time_ns()}",
    )
    service.schedule(
        principal=ADMIN,
        approval_binding_id=second_approval["approval_binding_id"],
        activation_at=second_at,
        idempotency_key=f"s-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    assert (
        service.process_next_policy_job(now=second_at)["status"] == "complete"
    )
    first = service.query_candidate_workspace(ADMIN, first_candidate)
    assert first["status"] == "superseded"
    assert first["activation_outcome"]["status"] == "active"
    assert first["activation_outcome"]["activation_event_id"]
    assert first["activation_outcome"]["active_generation"] == 2
    assert first["validation_outcome"]["status"] == "validated"
    second = service.query_candidate_workspace(ADMIN, second_candidate)
    assert second["activation_outcome"]["status"] == "active"
    assert second["activation_outcome"]["active_generation"] == 3

    # A candidate cancelled after validation keeps the validated verdict.
    cancelled_service, _, _ = make_policy_service(tmp_path / "cancel")
    draft_id = import_draft(cancelled_service)
    candidate_id, worker = freeze_and_validate(cancelled_service, draft_id)
    assert worker["outcome"] == "validated"
    cancelled_service.cancel(
        principal=ADMIN,
        candidate_id=candidate_id,
        reason_code="t08-cancel",
        idempotency_key=f"c-{time.time_ns()}",
        expected_governance_revision=governance_revision(cancelled_service),
    )
    workspace = cancelled_service.query_candidate_workspace(
        ADMIN, candidate_id
    )
    assert workspace["status"] == "cancelled"
    assert workspace["validation_outcome"]["status"] == "validated"
    assert workspace["activation_outcome"] is None


def test_query_candidates_not_blocked_by_validation_worker_lock(tmp_path):
    """Regression guard for the shared governance lock: the validation
    worker's long computation must not queue read queries behind it.  The
    worker previously held the lock for the whole validation (claim +
    ~1s of digest/check computation + result write), which pushed poll
    requests past the 2s HTTP contract on a loaded host.  After the fix
    only the claim and the result write hold the lock, so a query issued
    mid-computation returns in well under a second."""
    service, _, _ = make_policy_service(tmp_path)
    draft_id = import_draft(service)
    freeze = service.freeze_candidate(
        principal=ADMIN,
        draft_id=draft_id,
        idempotency_key=f"lock-freeze-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    candidate_id = freeze["candidate_id"]
    service.request_validation(
        principal=ADMIN,
        candidate_id=candidate_id,
        idempotency_key=f"lock-validate-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )

    results: list[dict[str, Any]] = []

    def worker() -> None:
        results.append(service.process_next_policy_job())

    thread = threading.Thread(target=worker)
    thread.start()
    # Let the worker claim the job and enter the validation computation.
    time.sleep(0.5)
    start = time.monotonic()
    service.query_candidates(ADMIN)
    elapsed = time.monotonic() - start
    thread.join(60)
    assert results and results[0]["status"] == "complete"
    assert elapsed < 1.0, (
        f"query queued behind the validation worker lock for {elapsed:.2f}s"
    )


def test_cancel_during_validation_compute_does_not_report_failure(
    tmp_path, monkeypatch
):
    """A candidate cancelled while the validation computation is running
    must not surface a validation-failure terminal: the race previously
    landed the job in diagnostic and projected the cancelled candidate as
    ``validation_outcome.status == failed``."""
    service, _, _ = make_policy_service(tmp_path)
    draft_id = import_draft(service)
    freeze = service.freeze_candidate(
        principal=ADMIN,
        draft_id=draft_id,
        idempotency_key=f"race-freeze-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    candidate_id = freeze["candidate_id"]
    service.request_validation(
        principal=ADMIN,
        candidate_id=candidate_id,
        idempotency_key=f"race-validate-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )

    original = service._validate_candidate

    def slow_validate(owner, state):
        time.sleep(0.8)
        return original(owner, state)

    monkeypatch.setattr(service, "_validate_candidate", slow_validate)
    results: list[dict[str, Any]] = []

    def worker() -> None:
        results.append(service.process_next_policy_job())

    thread = threading.Thread(target=worker)
    thread.start()
    time.sleep(0.2)  # worker claimed the job and is inside the computation
    service.cancel(
        principal=ADMIN,
        candidate_id=candidate_id,
        reason_code="t08-cancel-mid-validation",
        idempotency_key=f"race-cancel-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    thread.join(60)
    workspace = service.query_candidate_workspace(ADMIN, candidate_id)
    assert workspace["status"] == "cancelled"
    outcome = workspace["validation_outcome"]
    assert outcome is None or outcome["status"] != "failed", outcome
    jobs = [
        job
        for job in service._store.policy_jobs
        if job.get("kind") == "validation"
        and job.get("candidate_id") == candidate_id
    ]
    assert jobs and jobs[0]["status"] == "complete"


# --- R2: exact worker ownership (lease/fence/attempt) at every write --------


def _claim(
    service: PolicyGovernanceService, worker_id: str, now: int
) -> tuple[dict[str, Any], int, Any]:
    claim = service._claim_policy_job(worker_id, now)
    assert not isinstance(claim, dict), claim
    return claim


def _attempts_for(
    service: PolicyGovernanceService, policy_job_id: str
) -> list[dict[str, Any]]:
    return [
        record
        for record in service._store.policy_attempts
        if record.get("policy_job_id") == policy_job_id
    ]


class _FakeClock:
    """Deterministic clock for lease-liveness tests: ``advance()`` moves
    time forward, so a settlement that happens after the 30s lease expiry
    is observable through the real worker write path."""

    def __init__(self, start: int) -> None:
        self.value = start

    def __call__(self) -> int:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += seconds


def _scheduled_candidate(
    service: PolicyGovernanceService, label: str
) -> tuple[str, int]:
    _, candidate_id, _, activation_at = _full_flow(service)
    return candidate_id, activation_at


def test_validation_lease_takeover_stale_success_is_discarded(tmp_path: Path) -> None:
    """Lease expires, fence 2 reclaims and publishes; fence 1's late success
    settles only as its own discarded attempt and cannot publish a second
    verdict, artifact, event, audit, idempotency or outbox fact."""
    service, _, _ = make_policy_service(tmp_path)
    draft_id = import_draft(service)
    freeze = service.freeze_candidate(
        principal=ADMIN,
        draft_id=draft_id,
        idempotency_key=f"takeover-freeze-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    candidate_id = freeze["candidate_id"]
    service.request_validation(
        principal=ADMIN,
        candidate_id=candidate_id,
        idempotency_key=f"takeover-validate-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    now = int(time.time())
    job_a, now_a, snapshot_a = _claim(service, "worker-a", now)
    assert job_a["fence"] == 1 and job_a["attempt_no"] == 1
    job_b, now_b, snapshot_b = _claim(service, "worker-b", now + 31)
    assert job_b["fence"] == 2 and job_b["attempt_no"] == 2
    done = service._run_validation_job(job_b, now_b, snapshot_b)
    assert done["status"] == "complete", done
    assert done["outcome"] == "validated"
    audits_before = len(service._store.audit_events)
    outbox_before = len(service._store.outbox)
    artifacts_before = len(service._store.policy_artifacts)

    late = service._run_validation_job(job_a, now_a, snapshot_a)
    assert late["status"] == "discarded", late
    assert late["reason_code"] == "S08_ATTEMPT_STALE"
    verdicts = [
        event
        for event in service._store.policy_governance_events
        if event.get("kind") in {"validated", "rejected"}
        and event.get("candidate_id") == candidate_id
    ]
    assert len(verdicts) == 1  # only fence 2 published
    assert len(service._store.audit_events) == audits_before
    assert len(service._store.outbox) == outbox_before
    assert len(service._store.policy_artifacts) == artifacts_before
    current_job = next(
        item
        for item in service._store.policy_jobs
        if item["policy_job_id"] == job_a["policy_job_id"]
    )
    assert current_job["status"] == "complete"
    assert current_job["worker_id"] == "worker-b"
    assert current_job.get("reason_code") is None
    by_status = {
        (attempt["attempt_no"], attempt["status"])
        for attempt in _attempts_for(service, job_a["policy_job_id"])
    }
    assert (1, "running") in by_status and (1, "discarded") in by_status
    assert (2, "running") in by_status and (2, "complete") in by_status
    workspace = service.query_candidate_workspace(ADMIN, candidate_id)
    assert workspace["validation_outcome"]["status"] == "validated"


def test_validation_stale_worker_exception_cannot_mark_new_lease_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale fence-1 worker that throws after the fence-2 reclaim can only
    settle its own discarded attempt: the newer lease is never marked
    diagnostic and its verdict stays intact."""
    service, _, _ = make_policy_service(tmp_path)
    draft_id = import_draft(service)
    freeze = service.freeze_candidate(
        principal=ADMIN,
        draft_id=draft_id,
        idempotency_key=f"stale-exc-freeze-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    candidate_id = freeze["candidate_id"]
    service.request_validation(
        principal=ADMIN,
        candidate_id=candidate_id,
        idempotency_key=f"stale-exc-validate-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    now = int(time.time())
    job_a, now_a, snapshot_a = _claim(service, "worker-a", now)
    job_b, now_b, snapshot_b = _claim(service, "worker-b", now + 31)
    done = service._run_validation_job(job_b, now_b, snapshot_b)
    assert done["status"] == "complete", done

    def explode(owner: Any, state: dict[str, Any]) -> Any:
        raise PolicyUnavailable("injected stale worker failure")

    monkeypatch.setattr(service, "_validate_candidate", explode)
    late = service._run_validation_job(job_a, now_a, snapshot_a)
    assert late["status"] == "discarded", late
    current_job = next(
        item
        for item in service._store.policy_jobs
        if item["policy_job_id"] == job_a["policy_job_id"]
    )
    assert current_job["status"] == "complete"  # never diagnostic
    assert current_job["worker_id"] == "worker-b"
    assert current_job.get("reason_code") is None
    verdicts = [
        event
        for event in service._store.policy_governance_events
        if event.get("kind") in {"validated", "rejected"}
        and event.get("candidate_id") == candidate_id
    ]
    assert len(verdicts) == 1
    workspace = service.query_candidate_workspace(ADMIN, candidate_id)
    assert workspace["validation_outcome"]["status"] == "validated"


def test_activation_lease_takeover_stale_success_is_discarded(tmp_path: Path) -> None:
    """The activation twin of the validation takeover: fence 1's late success
    after fence 2 activated publishes nothing and the prior/fresh active
    generation is exactly the fence-2 one."""
    service, _, _ = make_policy_service(tmp_path)
    candidate_id, activation_at = _scheduled_candidate(service, "act-takeover")
    job_a, now_a, snapshot_a = _claim(service, "worker-a", activation_at)
    assert job_a["fence"] == 1
    job_b, now_b, snapshot_b = _claim(service, "worker-b", activation_at + 31)
    assert job_b["fence"] == 2
    done = service._run_activation_job(job_b, now_b, snapshot_b)
    assert done["status"] == "complete", done
    activated = [
        event
        for event in service._store.policy_governance_events
        if event.get("kind") == "activated" and not event.get("bootstrap")
    ]
    assert len(activated) == 1
    before = (
        len(service._store.audit_events),
        len(service._store.outbox),
        len(service._store.idempotency),
    )
    late = service._run_activation_job(job_a, now_a, snapshot_a)
    assert late["status"] == "discarded", late
    assert late["reason_code"] == "S08_ATTEMPT_STALE"
    activated_after = [
        event
        for event in service._store.policy_governance_events
        if event.get("kind") == "activated" and not event.get("bootstrap")
    ]
    assert len(activated_after) == 1
    assert (
        len(service._store.audit_events),
        len(service._store.outbox),
        len(service._store.idempotency),
    ) == before
    assert service.query_active(ADMIN)["active_generation"] == 2
    current_job = next(
        item
        for item in service._store.policy_jobs
        if item["policy_job_id"] == job_a["policy_job_id"]
    )
    assert current_job["status"] == "complete"
    assert current_job["worker_id"] == "worker-b"
    workspace = service.query_candidate_workspace(ADMIN, candidate_id)
    assert workspace["activation_outcome"]["status"] == "active"
    assert workspace["activation_outcome"]["active_generation"] == 2


def test_activation_stale_worker_exception_cannot_mark_new_lease_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale fence-1 activation worker that throws after the reclaim cannot
    mark the fence-2 lease diagnostic or add any second activation fact."""
    service, _, _ = make_policy_service(tmp_path)
    candidate_id, activation_at = _scheduled_candidate(service, "act-stale-exc")
    job_a, now_a, snapshot_a = _claim(service, "worker-a", activation_at)
    job_b, now_b, snapshot_b = _claim(service, "worker-b", activation_at + 31)
    done = service._run_activation_job(job_b, now_b, snapshot_b)
    assert done["status"] == "complete", done

    def explode(owner: Any, manifest_id: str, manifest_digest: str) -> Any:
        raise PolicyUnavailable("injected stale activation failure")

    monkeypatch.setattr(service, "_verify_pinned_manifest", explode)
    late = service._run_activation_job(job_a, now_a, snapshot_a)
    assert late["status"] == "discarded", late
    current_job = next(
        item
        for item in service._store.policy_jobs
        if item["policy_job_id"] == job_a["policy_job_id"]
    )
    assert current_job["status"] == "complete"  # never diagnostic
    assert current_job["worker_id"] == "worker-b"
    assert current_job.get("reason_code") is None
    activated = [
        event
        for event in service._store.policy_governance_events
        if event.get("kind") == "activated" and not event.get("bootstrap")
    ]
    assert len(activated) == 1
    assert service.query_active(ADMIN)["active_generation"] == 2


def test_crash_after_claim_preserves_durable_attempt_and_restart_reclaims(
    tmp_path: Path,
) -> None:
    """A crash after claim leaves the first durable attempt; a restart
    reclaims the expired lease and allows exactly one business effect."""
    service, rules_path, kb_path = make_policy_service(tmp_path)
    draft_id = import_draft(service)
    freeze = service.freeze_candidate(
        principal=ADMIN,
        draft_id=draft_id,
        idempotency_key=f"crash-freeze-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    candidate_id = freeze["candidate_id"]
    service.request_validation(
        principal=ADMIN,
        candidate_id=candidate_id,
        idempotency_key=f"crash-validate-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    now = int(time.time())
    job_a, now_a, snapshot_a = _claim(service, "worker-a", now)
    assert job_a["fence"] == 1
    policy_job_id = job_a["policy_job_id"]
    attempts = _attempts_for(service, policy_job_id)
    assert len(attempts) == 1 and attempts[0]["status"] == "running"
    # Crash: the claimed tuple is dropped without any terminal write.
    del job_a, now_a, snapshot_a
    restarted = PolicyGovernanceService(
        state_path=service._store.state_path,
        source_rules_path=rules_path,
        source_kb_path=kb_path,
        corpus_root=CORPUS,
    )
    result = restarted.process_next_policy_job(now=now + 31)
    assert result["status"] == "complete", result
    assert result["outcome"] == "validated"
    verdicts = [
        event
        for event in restarted._store.policy_governance_events
        if event.get("kind") in {"validated", "rejected"}
        and event.get("candidate_id") == candidate_id
    ]
    assert len(verdicts) == 1  # exactly one business effect
    by_status = {
        (attempt["attempt_no"], attempt["status"])
        for attempt in _attempts_for(restarted, policy_job_id)
    }
    assert (1, "running") in by_status  # first durable attempt preserved
    assert (2, "running") in by_status and (2, "complete") in by_status


def test_cancel_between_claim_and_compute_settles_discarded(tmp_path: Path) -> None:
    """A cancel landing between claim and the result write settles the job
    as discarded (complete job + discarded attempt) with no stale verdict."""
    service, _, _ = make_policy_service(tmp_path)
    draft_id = import_draft(service)
    freeze = service.freeze_candidate(
        principal=ADMIN,
        draft_id=draft_id,
        idempotency_key=f"claim-window-freeze-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    candidate_id = freeze["candidate_id"]
    service.request_validation(
        principal=ADMIN,
        candidate_id=candidate_id,
        idempotency_key=f"claim-window-validate-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    now = int(time.time())
    job_a, now_a, snapshot_a = _claim(service, "worker-a", now)
    service.cancel(
        principal=ADMIN,
        candidate_id=candidate_id,
        reason_code="t08-cancel-claim-window",
        idempotency_key=f"claim-window-cancel-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    result = service._run_validation_job(job_a, now_a, snapshot_a)
    assert result["status"] == "discarded", result
    current_job = next(
        item
        for item in service._store.policy_jobs
        if item["policy_job_id"] == job_a["policy_job_id"]
    )
    assert current_job["status"] == "complete"
    assert current_job.get("reason_code") is None
    assert not any(
        event.get("kind") in {"validated", "rejected"}
        and event.get("candidate_id") == candidate_id
        for event in service._store.policy_governance_events
    )
    by_status = {
        (attempt["attempt_no"], attempt["status"])
        for attempt in _attempts_for(service, job_a["policy_job_id"])
    }
    assert (1, "running") in by_status and (1, "discarded") in by_status


def test_hold_between_claim_and_activation_compute_settles_discarded(
    tmp_path: Path,
) -> None:
    """An activation hold landing between claim and the result write settles
    the activation job as discarded; no activation fact is published."""
    service, _, _ = make_policy_service(tmp_path)
    candidate_id, activation_at = _scheduled_candidate(service, "hold-window")
    job_a, now_a, snapshot_a = _claim(service, "worker-a", activation_at)
    operator = PolicyPrincipal(
        subject="c-demo-policy-operator",
        role="operator",
        scope=S08_SCOPE,
        source_id="s08-test",
    )
    service.stop_activations(
        principal=operator,
        reason_code="S08_DRILL_HOLD",
        idempotency_key=f"hold-window-stop-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    result = service._run_activation_job(job_a, now_a, snapshot_a)
    assert result["status"] == "discarded", result
    current_job = next(
        item
        for item in service._store.policy_jobs
        if item["policy_job_id"] == job_a["policy_job_id"]
    )
    assert current_job["status"] == "complete"
    assert current_job.get("reason_code") is None
    assert not any(
        event.get("kind") == "activated" and not event.get("bootstrap")
        for event in service._store.policy_governance_events
    )
    assert service.query_active(ADMIN)["active_generation"] == 1


def test_injected_persistence_failure_leaves_coherent_deltas(tmp_path: Path) -> None:
    """An injected write-point failure keeps every business delta at zero and
    settles the job diagnostic with a registered reason code plus durable
    running/failed attempts: job, attempt, event, artifact, audit,
    idempotency, outbox and reservation stay mutually consistent."""
    bundle = tmp_path / "server-bundle"
    bundle.mkdir()
    (bundle / "rules.yaml").write_bytes(DEFAULT_RULES.read_bytes())
    (bundle / "entity_kb.json").write_bytes(DEFAULT_KB.read_bytes())

    def inject(write_point: str) -> None:
        if write_point == "s08.validation":
            raise OSError("injected S08 fault")

    service = PolicyGovernanceService(
        state_path=tmp_path / "coherent.sqlite3",
        source_rules_path=bundle / "rules.yaml",
        source_kb_path=bundle / "entity_kb.json",
        corpus_root=CORPUS,
        fault_injector=inject,
    )
    assert service.bootstrap_once()["status"] == "activated"
    draft_id = import_draft(service)
    freeze = service.freeze_candidate(
        principal=ADMIN,
        draft_id=draft_id,
        idempotency_key=f"coherent-freeze-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    candidate_id = freeze["candidate_id"]
    service.request_validation(
        principal=ADMIN,
        candidate_id=candidate_id,
        idempotency_key=f"coherent-validate-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    before = {
        "events": len(service._store.policy_governance_events),
        "audit": len(service._store.audit_events),
        "outbox": len(service._store.outbox),
        "idempotency": len(service._store.idempotency),
        "artifacts": len(service._store.policy_artifacts),
        "reservations": len(service._store.policy_schedule_reservations),
    }
    result = service.process_next_policy_job()
    assert result["status"] == "failed", result
    assert result["reason_code"] == "S08_VALIDATION_INTERNAL"
    job = next(
        item
        for item in service._store.policy_jobs
        if item.get("kind") == "validation"
        and item.get("candidate_id") == candidate_id
    )
    assert job["status"] == "diagnostic"
    assert job["reason_code"] == "S08_VALIDATION_INTERNAL"
    assert job.get("terminal_reason") is None  # no raw exception text stored
    assert len(service._store.policy_governance_events) == before["events"]
    assert len(service._store.audit_events) == before["audit"]
    assert len(service._store.outbox) == before["outbox"]
    assert len(service._store.idempotency) == before["idempotency"]
    assert len(service._store.policy_artifacts) == before["artifacts"]
    assert len(service._store.policy_schedule_reservations) == before["reservations"]
    statuses = {
        attempt["status"] for attempt in _attempts_for(service, job["policy_job_id"])
    }
    assert statuses == {"running", "failed"}


def test_validation_diagnostic_retry_same_second_uses_new_job_and_latest_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, _ = make_policy_service(tmp_path)
    clock = _FakeClock(1_700_000_000)
    service._clock = clock
    draft_id = import_draft(service)
    freeze = service.freeze_candidate(
        principal=ADMIN,
        draft_id=draft_id,
        idempotency_key="diagnostic-retry-freeze",
        expected_governance_revision=governance_revision(service),
    )
    candidate_id = freeze["candidate_id"]
    first = service.request_validation(
        principal=ADMIN,
        candidate_id=candidate_id,
        idempotency_key="diagnostic-retry-first",
        expected_governance_revision=governance_revision(service),
    )
    original_validate = service._validate_candidate
    monkeypatch.setattr(
        service,
        "_validate_candidate",
        lambda owner, state: (_ for _ in ()).throw(RuntimeError("validation fault")),
    )
    failed = service.process_next_policy_job(now=clock.value)
    assert failed["status"] == "failed"
    monkeypatch.setattr(service, "_validate_candidate", original_validate)

    second = service.request_validation(
        principal=ADMIN,
        candidate_id=candidate_id,
        idempotency_key="diagnostic-retry-second",
        expected_governance_revision=governance_revision(service),
    )
    assert second["policy_job_id"] != first["policy_job_id"]
    assert service.query_candidate_workspace(ADMIN, candidate_id)[
        "validation_outcome"
    ] == {"status": "pending", "reason_code": None}

    completed = service.process_next_policy_job(now=clock.value)
    assert completed["status"] == "complete"
    assert service.query_candidate_workspace(ADMIN, candidate_id)[
        "validation_outcome"
    ] == {"status": "validated", "reason_code": "S08_VALIDATION_PASSED"}


# --- R3: fresh ownership at settlement, isolated claim snapshot, and a
# --- truthful terminal activation attempt ----------------------------------


def test_validation_expired_lease_without_takeover_discards(tmp_path: Path) -> None:
    """A validation that finishes after its lease expired, with no successor
    claim, is no longer a current owner: it settles only its own discarded
    attempt and must not publish a verdict, artifact, audit, idempotency or
    outbox fact, and must not touch the still-leased job row.  The lease is
    checked against a fresh settlement-time clock read, not the claim time."""
    clock = _FakeClock(1_700_000_000)
    service, _, _ = make_policy_service(tmp_path)
    service._clock = clock
    draft_id = import_draft(service)
    freeze = service.freeze_candidate(
        principal=ADMIN,
        draft_id=draft_id,
        idempotency_key=f"expired-freeze-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    candidate_id = freeze["candidate_id"]
    service.request_validation(
        principal=ADMIN,
        candidate_id=candidate_id,
        idempotency_key=f"expired-validate-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    now = int(clock())
    job, now_a, snapshot = _claim(service, "worker-a", now)
    lease_until = int(job["lease_until"])
    assert lease_until == now + 30
    clock.advance(31)  # a long computation outlives the 30s lease
    before = {
        "events": len(service._store.policy_governance_events),
        "audit": len(service._store.audit_events),
        "outbox": len(service._store.outbox),
        "artifacts": len(service._store.policy_artifacts),
        "idempotency": len(service._store.idempotency),
    }
    result = service._run_validation_job(job, now_a, snapshot)
    assert result["status"] == "discarded", result
    assert result["reason_code"] == "S08_ATTEMPT_STALE"
    assert not any(
        event.get("kind") in {"validated", "rejected"}
        and event.get("candidate_id") == candidate_id
        for event in service._store.policy_governance_events
    )
    assert len(service._store.policy_governance_events) == before["events"]
    assert len(service._store.audit_events) == before["audit"]
    assert len(service._store.outbox) == before["outbox"]
    assert len(service._store.policy_artifacts) == before["artifacts"]
    assert len(service._store.idempotency) == before["idempotency"]
    current_job = next(
        item
        for item in service._store.policy_jobs
        if item["policy_job_id"] == job["policy_job_id"]
    )
    assert current_job["status"] == "leased"  # no successor owns or closes it
    assert current_job["worker_id"] == "worker-a"
    assert int(current_job["lease_until"]) == lease_until
    by_status = {
        (attempt["attempt_no"], attempt["status"])
        for attempt in _attempts_for(service, job["policy_job_id"])
    }
    assert (1, "running") in by_status and (1, "discarded") in by_status


def test_activation_expired_lease_without_takeover_discards(tmp_path: Path) -> None:
    """The activation twin: finishing after lease expiry with no successor
    publishes nothing -- no activated event, projection change, outbox,
    idempotency or audit -- and the prior active stays untouched.  The lease
    is checked against a fresh settlement-time clock read."""
    clock = _FakeClock(1_700_000_000)
    service, _, _ = make_policy_service(tmp_path)
    candidate_id, activation_at = _scheduled_candidate(service, "act-expired")
    # The scheduling flow stamps activation_at with the real clock; align
    # the deterministic clock with it so lease liveness is observable.
    clock.value = activation_at
    service._clock = clock
    active_before = service.query_active(ADMIN)["active_generation"]
    job, now_a, snapshot = _claim(service, "worker-a", activation_at)
    before = {
        "audit": len(service._store.audit_events),
        "outbox": len(service._store.outbox),
        "idempotency": len(service._store.idempotency),
    }
    clock.advance(31)  # the activation compute outlives the 30s lease
    result = service._run_activation_job(job, activation_at, snapshot)
    assert result["status"] == "discarded", result
    assert result["reason_code"] == "S08_ATTEMPT_STALE"
    assert not any(
        event.get("kind") == "activated" and not event.get("bootstrap")
        for event in service._store.policy_governance_events
    )
    assert service.query_active(ADMIN)["active_generation"] == active_before
    assert len(service._store.audit_events) == before["audit"]
    assert len(service._store.outbox) == before["outbox"]
    assert len(service._store.idempotency) == before["idempotency"]
    current_job = next(
        item
        for item in service._store.policy_jobs
        if item["policy_job_id"] == job["policy_job_id"]
    )
    assert current_job["status"] == "leased"
    assert current_job["worker_id"] == "worker-a"
    by_status = {
        (attempt["attempt_no"], attempt["status"])
        for attempt in _attempts_for(service, job["policy_job_id"])
    }
    assert (1, "running") in by_status and (1, "discarded") in by_status


def test_public_pipeline_expired_lease_without_takeover_discards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production entry point itself enforces lease liveness at
    settlement: a validation whose compute outlives the 30s lease is
    discarded by ``process_next_policy_job`` with no successor claim --
    the check is a fresh settlement-time clock read, never the claim time."""
    clock = _FakeClock(1_700_000_000)
    service, _, _ = make_policy_service(tmp_path)
    service._clock = clock
    draft_id = import_draft(service)
    freeze = service.freeze_candidate(
        principal=ADMIN,
        draft_id=draft_id,
        idempotency_key=f"public-expired-freeze-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    candidate_id = freeze["candidate_id"]
    service.request_validation(
        principal=ADMIN,
        candidate_id=candidate_id,
        idempotency_key=f"public-expired-validate-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    now = int(clock())
    original = service._validate_candidate

    def slow_validate(owner: Any, state: dict[str, Any]) -> Any:
        clock.advance(31)  # the compute runs past the 30s lease
        return original(owner, state)

    monkeypatch.setattr(service, "_validate_candidate", slow_validate)
    result = service.process_next_policy_job(now=now)
    assert result["status"] == "discarded", result
    assert result["reason_code"] == "S08_ATTEMPT_STALE"
    assert not any(
        event.get("kind") in {"validated", "rejected"}
        and event.get("candidate_id") == candidate_id
        for event in service._store.policy_governance_events
    )
    current_job = next(
        item
        for item in service._store.policy_jobs
        if item.get("kind") == "validation"
        and item.get("candidate_id") == candidate_id
    )
    assert current_job["status"] == "leased"  # no successor closed it
    assert current_job["worker_id"] == "s08-activator"
    by_status = {
        (attempt["attempt_no"], attempt["status"])
        for attempt in _attempts_for(service, current_job["policy_job_id"])
    }
    assert (1, "running") in by_status and (1, "discarded") in by_status


def test_cancel_during_validation_compute_exception_discards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancel landing during validation followed by a compute exception
    settles the still-owned job as complete+discarded -- never diagnostic --
    with the cancelled candidate state retained and no failed outcome."""
    service, _, _ = make_policy_service(tmp_path)
    draft_id = import_draft(service)
    freeze = service.freeze_candidate(
        principal=ADMIN,
        draft_id=draft_id,
        idempotency_key=f"exc-cancel-freeze-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    candidate_id = freeze["candidate_id"]
    service.request_validation(
        principal=ADMIN,
        candidate_id=candidate_id,
        idempotency_key=f"exc-cancel-validate-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    now = int(time.time())
    job, now_a, snapshot = _claim(service, "worker-a", now)
    service.cancel(
        principal=ADMIN,
        candidate_id=candidate_id,
        reason_code="t08-r3-exc-cancel",
        idempotency_key=f"exc-cancel-cancel-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )

    def explode(owner: Any, state: dict[str, Any]) -> Any:
        raise PolicyUnavailable("injected compute failure after cancel")

    monkeypatch.setattr(service, "_validate_candidate", explode)
    result = service._run_validation_job(job, now_a, snapshot)
    assert result["status"] == "discarded", result
    current_job = next(
        item
        for item in service._store.policy_jobs
        if item["policy_job_id"] == job["policy_job_id"]
    )
    assert current_job["status"] == "complete"
    assert current_job.get("reason_code") is None
    assert not any(
        event.get("kind") in {"validated", "rejected"}
        and event.get("candidate_id") == candidate_id
        for event in service._store.policy_governance_events
    )
    workspace = service.query_candidate_workspace(ADMIN, candidate_id)
    assert workspace["status"] == "cancelled"
    outcome = workspace["validation_outcome"]
    assert outcome is None or outcome["status"] != "failed", outcome
    by_status = {
        (attempt["attempt_no"], attempt["status"])
        for attempt in _attempts_for(service, job["policy_job_id"])
    }
    assert (1, "running") in by_status and (1, "discarded") in by_status


def test_hold_during_activation_compute_exception_discards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hold installed during activation followed by a compute exception
    settles the still-owned job as complete+discarded: prior active
    unchanged, no activated event and no activation/diagnostic lie."""
    service, _, _ = make_policy_service(tmp_path)
    candidate_id, activation_at = _scheduled_candidate(service, "act-exc-hold")
    active_before = service.query_active(ADMIN)["active_generation"]
    job, now_a, snapshot = _claim(service, "worker-a", activation_at)
    operator = PolicyPrincipal(
        subject="c-demo-policy-operator",
        role="operator",
        scope=S08_SCOPE,
        source_id="s08-test",
    )
    service.stop_activations(
        principal=operator,
        reason_code="S08_DRILL_HOLD",
        idempotency_key=f"exc-hold-stop-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )

    def explode(owner: Any, manifest_id: str, manifest_digest: str) -> Any:
        raise PolicyUnavailable("injected compute failure after hold")

    monkeypatch.setattr(service, "_verify_pinned_manifest", explode)
    result = service._run_activation_job(job, now_a, snapshot)
    assert result["status"] == "discarded", result
    current_job = next(
        item
        for item in service._store.policy_jobs
        if item["policy_job_id"] == job["policy_job_id"]
    )
    assert current_job["status"] == "complete"
    assert current_job.get("reason_code") is None
    assert not any(
        event.get("kind") == "activated" and not event.get("bootstrap")
        for event in service._store.policy_governance_events
    )
    assert service.query_active(ADMIN)["active_generation"] == active_before
    by_status = {
        (attempt["attempt_no"], attempt["status"])
        for attempt in _attempts_for(service, job["policy_job_id"])
    }
    assert (1, "running") in by_status and (1, "discarded") in by_status


def test_query_does_not_mutate_claim_snapshot(tmp_path: Path) -> None:
    """A public query/reload between claim and compute cannot alter the
    claim-time snapshot: the compute phase must keep seeing claim-time state,
    and a concurrent cancel becomes visible only at the write-time recheck,
    which settles the job as discarded -- never diagnostic."""
    service, rules_path, kb_path = make_policy_service(tmp_path)
    draft_id = import_draft(service)
    freeze = service.freeze_candidate(
        principal=ADMIN,
        draft_id=draft_id,
        idempotency_key=f"snapshot-freeze-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    candidate_id = freeze["candidate_id"]
    service.request_validation(
        principal=ADMIN,
        candidate_id=candidate_id,
        idempotency_key=f"snapshot-validate-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    now = int(time.time())
    job, now_a, snapshot = _claim(service, "worker-a", now)
    assert snapshot is not service._store  # independent claim-time snapshot
    other = PolicyGovernanceService(
        state_path=service._store.state_path,
        source_rules_path=rules_path,
        source_kb_path=kb_path,
        corpus_root=CORPUS,
    )
    other.cancel(
        principal=ADMIN,
        candidate_id=candidate_id,
        reason_code="t08-r3-snapshot-cancel",
        idempotency_key=f"snapshot-cancel-{time.time_ns()}",
        expected_governance_revision=governance_revision(other),
    )
    service.query_candidates(ADMIN)
    claim_time = service._require_candidate_state(snapshot, candidate_id)
    assert claim_time["status"] == "candidate"  # claim-time view preserved
    result = service._run_validation_job(job, now_a, snapshot)
    assert result["status"] == "discarded", result
    current_job = next(
        item
        for item in service._store.policy_jobs
        if item["policy_job_id"] == job["policy_job_id"]
    )
    assert current_job["status"] == "complete"
    assert current_job.get("reason_code") is None
    assert not any(
        event.get("kind") in {"validated", "rejected"}
        and event.get("candidate_id") == candidate_id
        for event in service._store.policy_governance_events
    )


def test_terminal_attempt_matches_event(tmp_path: Path) -> None:
    """After a successful activation and a fresh-store restart, the terminal
    complete attempt carries the exact activation event identity and
    generation that the activated event, active projection and outbox
    publish -- no placeholder nulls survive restart."""
    service, rules_path, kb_path = make_policy_service(tmp_path)
    candidate_id, activation_at = _scheduled_candidate(service, "act-identity")
    job, now_a, snapshot = _claim(service, "worker-a", activation_at)
    result = service._run_activation_job(job, now_a, snapshot)
    assert result["status"] == "complete", result
    event_id = result["activation_event_id"]
    generation = result["active_generation"]
    assert event_id and generation
    restarted = PolicyGovernanceService(
        state_path=service._store.state_path,
        source_rules_path=rules_path,
        source_kb_path=kb_path,
        corpus_root=CORPUS,
    )
    attempt = next(
        record
        for record in restarted._store.policy_attempts
        if record["policy_job_id"] == job["policy_job_id"]
        and record["status"] == "complete"
    )
    assert attempt["result"]["activation_event_id"] == event_id
    assert attempt["result"]["active_generation"] == generation
    activated = next(
        event
        for event in restarted._store.policy_governance_events
        if event.get("kind") == "activated" and not event.get("bootstrap")
    )
    assert activated["event_id"] == event_id
    assert activated["activation_event_id"] == event_id
    assert activated["active_generation"] == generation
    projection = restarted._store.policy_active_projections[S08_SCOPE]
    assert projection["activation_event_id"] == event_id
    assert projection["active_generation"] == generation
    outbox = next(
        item
        for item in restarted._store.outbox
        if item.get("kind") == "s08_activated"
    )
    assert outbox["activation_event_id"] == event_id
    assert outbox["active_generation"] == generation


def test_cross_service_cancel_reclaim_exception_discards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancel issued by a second service while worker A computes, followed
    by A's stale CAS retry and worker B's post-expiry reclaim, must settle as
    discarded -- never diagnostic: the cancelled candidate stays put, no
    failed outcome or domain event is published, and the prior active
    generation is untouched."""
    service, rules_path, kb_path = make_policy_service(tmp_path)
    draft_id = import_draft(service)
    freeze = service.freeze_candidate(
        principal=ADMIN,
        draft_id=draft_id,
        idempotency_key=f"cross-cancel-freeze-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    candidate_id = freeze["candidate_id"]
    service.request_validation(
        principal=ADMIN,
        candidate_id=candidate_id,
        idempotency_key=f"cross-cancel-validate-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    now = int(time.time())
    job_a, now_a, snapshot_a = _claim(service, "worker-a", now)
    other = PolicyGovernanceService(
        state_path=service._store.state_path,
        source_rules_path=rules_path,
        source_kb_path=kb_path,
        corpus_root=CORPUS,
    )

    def explode(owner: Any, state: dict[str, Any]) -> Any:
        raise PolicyUnavailable("forced compute exception")

    monkeypatch.setattr(service, "_validate_candidate", explode)
    original_persist = service._persist_worker
    injected = False

    def persist_after_cross_service_cancel(staged: Any) -> bool:
        # The second service cancels between A's authoritative reload and
        # A's CAS persist, so A's terminal write loses the revision race.
        nonlocal injected
        if not injected:
            injected = True
            other.cancel(
                principal=ADMIN,
                candidate_id=candidate_id,
                reason_code="t08-r4-cross-cancel",
                idempotency_key=f"cross-cancel-cancel-{time.time_ns()}",
                expected_governance_revision=governance_revision(other),
            )
        return original_persist(staged)

    monkeypatch.setattr(service, "_persist_worker", persist_after_cross_service_cancel)
    first = service._run_validation_job(job_a, now_a, snapshot_a)
    assert first["status"] == "retry", first  # A loses the CAS to the cancel
    second = service.process_next_policy_job(worker_id="worker-b", now=now + 31)
    assert second["status"] == "discarded", second
    service._store.reload()
    current_job = next(
        item
        for item in service._store.policy_jobs
        if item["policy_job_id"] == job_a["policy_job_id"]
    )
    assert current_job["status"] == "complete"
    assert current_job.get("reason_code") is None
    by_status = {
        (attempt["attempt_no"], attempt["status"])
        for attempt in _attempts_for(service, job_a["policy_job_id"])
    }
    assert (1, "running") in by_status
    assert (2, "running") in by_status and (2, "discarded") in by_status
    assert not any(status == "failed" for _, status in by_status)
    assert not any(
        event.get("kind") in {"validated", "rejected"}
        and event.get("candidate_id") == candidate_id
        for event in service._store.policy_governance_events
    )
    assert service.query_active(ADMIN)["active_generation"] == 1
    workspace = service.query_candidate_workspace(ADMIN, candidate_id)
    assert workspace["status"] == "cancelled"
    outcome = workspace["validation_outcome"]
    assert outcome is None or outcome["status"] != "failed", outcome


def test_cross_service_hold_reclaim_exception_discards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hold installed by a second service while worker A activates,
    followed by A's stale CAS retry and B's post-expiry reclaim, settles as
    discarded: no activated event, no diagnostic, prior active unchanged."""
    service, rules_path, kb_path = make_policy_service(tmp_path)
    candidate_id, activation_at = _scheduled_candidate(service, "act-cross-hold")
    active_before = service.query_active(ADMIN)["active_generation"]
    job_a, now_a, snapshot_a = _claim(service, "worker-a", activation_at)
    operator = PolicyPrincipal(
        subject="c-demo-policy-operator",
        role="operator",
        scope=S08_SCOPE,
        source_id="s08-test",
    )
    other = PolicyGovernanceService(
        state_path=service._store.state_path,
        source_rules_path=rules_path,
        source_kb_path=kb_path,
        corpus_root=CORPUS,
    )

    def explode(owner: Any, manifest_id: str, manifest_digest: str) -> Any:
        raise PolicyUnavailable("forced compute exception")

    monkeypatch.setattr(service, "_verify_pinned_manifest", explode)
    original_persist = service._persist_worker
    injected = False

    def persist_after_cross_service_hold(staged: Any) -> bool:
        # The second service installs the hold between A's authoritative
        # reload and A's CAS persist, so A's terminal write loses the race.
        nonlocal injected
        if not injected:
            injected = True
            other.stop_activations(
                principal=operator,
                reason_code="S08_DRILL_HOLD",
                idempotency_key=f"cross-hold-stop-{time.time_ns()}",
                expected_governance_revision=governance_revision(other),
            )
        return original_persist(staged)

    monkeypatch.setattr(service, "_persist_worker", persist_after_cross_service_hold)
    first = service._run_activation_job(job_a, now_a, snapshot_a)
    assert first["status"] == "retry", first  # A loses the CAS to the hold
    second = service.process_next_policy_job(worker_id="worker-b", now=now_a + 31)
    assert second["status"] == "discarded", second
    service._store.reload()
    current_job = next(
        item
        for item in service._store.policy_jobs
        if item["policy_job_id"] == job_a["policy_job_id"]
    )
    assert current_job["status"] == "complete"
    assert current_job.get("reason_code") is None
    by_status = {
        (attempt["attempt_no"], attempt["status"])
        for attempt in _attempts_for(service, job_a["policy_job_id"])
    }
    assert (1, "running") in by_status
    assert (2, "running") in by_status and (2, "discarded") in by_status
    assert not any(status == "failed" for _, status in by_status)
    assert not any(
        event.get("kind") == "activated" and not event.get("bootstrap")
        for event in service._store.policy_governance_events
    )
    assert service.query_active(ADMIN)["active_generation"] == active_before


def test_scope_expiry_during_compute_settles_discarded(tmp_path: Path) -> None:
    """The frozen validity window is rechecked at the trusted settlement
    time, not only at claim time: when valid_to crosses during activation
    compute while the 30s lease stays live, the job settles as discarded,
    the prior active release/generation stays authoritative, and no
    activated event or projection is written."""
    from datetime import datetime, timezone

    service, rules_path, kb_path = make_policy_service(tmp_path)
    base = int(time.time())

    def iso(timestamp: int) -> str:
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()

    draft_id = import_draft(service)
    service.revise_draft(
        principal=ADMIN,
        draft_id=draft_id,
        metadata={
            "scope": S08_SCOPE,
            "validity": {"valid_from": iso(base - 60), "valid_to": iso(base + 20)},
            "source": SOURCE_BUNDLE_ID,
            "reason": "t08-r4-scope-expiry",
        },
        idempotency_key=f"scope-expiry-revise-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    candidate_id, worker = freeze_and_validate(service, draft_id)
    assert worker["outcome"] == "validated"
    service.submit_review(
        principal=ADMIN,
        candidate_id=candidate_id,
        idempotency_key=f"scope-expiry-review-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    activation_at = base + 10
    approval = preview_and_approve(
        service,
        candidate_id=candidate_id,
        activation_time=activation_at,
        recovery_release_id=service.query_active(ADMIN)["candidate_id"],
        idempotency_key=f"scope-expiry-approve-{time.time_ns()}",
    )
    service.schedule(
        principal=ADMIN,
        approval_binding_id=approval["approval_binding_id"],
        activation_at=activation_at,
        idempotency_key=f"scope-expiry-schedule-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    prior_active = service.query_active(ADMIN)
    clock = _FakeClock(activation_at)
    service._clock = clock
    original_verify = service._verify_pinned_manifest
    advanced = False

    def verify_after_scope_check(
        owner: Any, manifest_id: str, manifest_digest: str
    ) -> Any:
        nonlocal advanced
        if not advanced:
            advanced = True
            clock.value = base + 21
        return original_verify(owner, manifest_id, manifest_digest)

    service._verify_pinned_manifest = verify_after_scope_check
    result = service.process_next_policy_job()
    assert result["status"] == "discarded", result
    service._store.reload()
    current_job = next(
        item
        for item in service._store.policy_jobs
        if item.get("candidate_id") == candidate_id and item["kind"] == "activation"
    )
    assert current_job["status"] == "complete"
    assert current_job.get("reason_code") is None
    assert not any(
        event.get("kind") == "activated" and not event.get("bootstrap")
        for event in service._store.policy_governance_events
    )
    active = service.query_active(ADMIN)
    assert active["candidate_id"] == prior_active["candidate_id"]
    assert active["active_generation"] == prior_active["active_generation"]
    projection = service._store.policy_active_projections[S08_SCOPE]
    assert projection["candidate_id"] == prior_active["candidate_id"]
    workspace = service.query_candidate_workspace(ADMIN, candidate_id)
    outcome = workspace["activation_outcome"]
    assert outcome is None or outcome["status"] == "pending", outcome
    restarted = PolicyGovernanceService(
        state_path=service._store.state_path,
        source_rules_path=rules_path,
        source_kb_path=kb_path,
        corpus_root=CORPUS,
    )
    assert (
        restarted.query_active(ADMIN)["active_generation"]
        == prior_active["active_generation"]
    )


def test_activation_settlement_time_is_fresh_and_joined(tmp_path: Path) -> None:
    """The activation projection's activated_at and the activated event's
    trusted_time both come from the fresh settlement clock -- never the
    claim-time now -- and survive a restart."""
    service, rules_path, kb_path = make_policy_service(tmp_path)
    candidate_id, activation_at = _scheduled_candidate(service, "act-settlement-time")
    clock = _FakeClock(activation_at)
    service._clock = clock
    job, now_a, snapshot = _claim(service, "worker-a", activation_at)
    original_verify = service._verify_pinned_manifest
    advanced = False

    def verify_after_scope_check(
        owner: Any, manifest_id: str, manifest_digest: str
    ) -> Any:
        nonlocal advanced
        if not advanced:
            advanced = True
            clock.advance(5)
        return original_verify(owner, manifest_id, manifest_digest)

    service._verify_pinned_manifest = verify_after_scope_check
    result = service._run_activation_job(job, now_a, snapshot)
    assert result["status"] == "complete", result
    projection = service._store.policy_active_projections[S08_SCOPE]
    activated = next(
        event
        for event in service._store.policy_governance_events
        if event.get("kind") == "activated" and not event.get("bootstrap")
    )
    assert projection["activated_at"] == activated["trusted_time"]
    assert projection["activated_at"] == clock.value
    assert projection["activated_at"] != now_a  # never claim-time now
    restarted = PolicyGovernanceService(
        state_path=service._store.state_path,
        source_rules_path=rules_path,
        source_kb_path=kb_path,
        corpus_root=CORPUS,
    )
    assert (
        restarted._store.policy_active_projections[S08_SCOPE]["activated_at"]
        == projection["activated_at"]
    )


def test_scope_expiry_during_compute_exception_discards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A compute exception landing after valid_to crossed -- while the 30s
    lease is still live and the candidate state/hold are unchanged -- must
    settle as discarded, never diagnostic: the activation cannot legally
    land at the trusted settlement time, and prior active stays put."""
    from datetime import datetime, timezone

    service, rules_path, kb_path = make_policy_service(tmp_path)
    base = int(time.time())

    def iso(timestamp: int) -> str:
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()

    draft_id = import_draft(service)
    service.revise_draft(
        principal=ADMIN,
        draft_id=draft_id,
        metadata={
            "scope": S08_SCOPE,
            "validity": {"valid_from": iso(base - 60), "valid_to": iso(base + 20)},
            "source": SOURCE_BUNDLE_ID,
            "reason": "t08-r4-scope-expiry-exception",
        },
        idempotency_key=f"scope-expiry-exc-revise-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    candidate_id, worker = freeze_and_validate(service, draft_id)
    assert worker["outcome"] == "validated"
    service.submit_review(
        principal=ADMIN,
        candidate_id=candidate_id,
        idempotency_key=f"scope-expiry-exc-review-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    activation_at = base + 10
    approval = preview_and_approve(
        service,
        candidate_id=candidate_id,
        activation_time=activation_at,
        recovery_release_id=service.query_active(ADMIN)["candidate_id"],
        idempotency_key=f"scope-expiry-exc-approve-{time.time_ns()}",
    )
    service.schedule(
        principal=ADMIN,
        approval_binding_id=approval["approval_binding_id"],
        activation_at=activation_at,
        idempotency_key=f"scope-expiry-exc-schedule-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    prior_active = service.query_active(ADMIN)
    clock = _FakeClock(activation_at)
    service._clock = clock
    original_verify = service._verify_pinned_manifest
    advanced = False

    def verify_then_explode(
        owner: Any, manifest_id: str, manifest_digest: str
    ) -> Any:
        nonlocal advanced
        if not advanced:
            advanced = True
            clock.value = base + 21
        raise PolicyUnavailable("forced compute exception after scope expiry")

    monkeypatch.setattr(service, "_verify_pinned_manifest", verify_then_explode)
    result = service.process_next_policy_job()
    assert result["status"] == "discarded", result
    monkeypatch.setattr(service, "_verify_pinned_manifest", original_verify)
    service._store.reload()
    current_job = next(
        item
        for item in service._store.policy_jobs
        if item.get("candidate_id") == candidate_id and item["kind"] == "activation"
    )
    assert current_job["status"] == "complete"
    assert current_job.get("reason_code") is None
    assert not any(
        event.get("kind") == "activated" and not event.get("bootstrap")
        for event in service._store.policy_governance_events
    )
    active = service.query_active(ADMIN)
    assert active["candidate_id"] == prior_active["candidate_id"]
    assert active["active_generation"] == prior_active["active_generation"]
    workspace = service.query_candidate_workspace(ADMIN, candidate_id)
    outcome = workspace["activation_outcome"]
    assert outcome is None or outcome["status"] == "pending", outcome
    restarted = PolicyGovernanceService(
        state_path=service._store.state_path,
        source_rules_path=rules_path,
        source_kb_path=kb_path,
        corpus_root=CORPUS,
    )
    assert (
        restarted.query_active(ADMIN)["active_generation"]
        == prior_active["active_generation"]
    )


def test_activation_claim_time_state_loss_settles_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the claim-time hold resolution itself fails before claim_hold is
    bound (corrupt hold event), the exception handler must not crash on an
    unbound claim_hold: it settles fail-closed as discarded with no domain
    fact."""
    service, rules_path, kb_path = make_policy_service(tmp_path)
    candidate_id, activation_at = _scheduled_candidate(service, "act-claim-loss")
    active_before = service.query_active(ADMIN)["active_generation"]
    job, now_a, snapshot = _claim(service, "worker-a", activation_at)

    def raise_corrupt_hold(owner: Any, scope: str) -> Any:
        raise KeyError("corrupt activation hold event")

    monkeypatch.setattr(service, "_activation_hold", raise_corrupt_hold)
    result = service._run_activation_job(job, now_a, snapshot)
    assert result["status"] == "discarded", result
    service._store.reload()
    current_job = next(
        item
        for item in service._store.policy_jobs
        if item["policy_job_id"] == job["policy_job_id"]
    )
    assert current_job["status"] == "complete"
    assert current_job.get("reason_code") is None
    assert not any(
        event.get("kind") == "activated" and not event.get("bootstrap")
        for event in service._store.policy_governance_events
    )
    assert service.query_active(ADMIN)["active_generation"] == active_before


def test_activation_uses_one_trusted_settlement_time_across_restart(
    tmp_path: Path,
) -> None:
    """One activation transaction has one trusted settlement instant: the
    ledger event, supersession event, and derived active projection must not
    sample a moving clock independently; the attempt keeps claim-time start."""
    service, rules_path, kb_path = make_policy_service(tmp_path)
    _, activation_at = _scheduled_candidate(service, "act-one-settlement-time")
    tick = activation_at

    def ticking_clock() -> int:
        nonlocal tick
        tick += 1
        return tick

    service._clock = ticking_clock
    result = service.process_next_policy_job(now=activation_at)
    assert result["status"] == "complete", result
    restarted = PolicyGovernanceService(
        state_path=service._store.state_path,
        source_rules_path=rules_path,
        source_kb_path=kb_path,
        corpus_root=CORPUS,
    )
    activated = next(
        event
        for event in restarted._store.policy_governance_events
        if event.get("event_id") == result["activation_event_id"]
    )
    superseded = next(
        event
        for event in restarted._store.policy_governance_events
        if event.get("kind") == "superseded"
        and event.get("successor_activation_event_id")
        == result["activation_event_id"]
    )
    projection = restarted._store.policy_active_projections[S08_SCOPE]
    attempt = next(
        record
        for record in restarted._store.policy_attempts
        if record.get("status") == "complete"
        and record.get("result", {}).get("activation_event_id")
        == result["activation_event_id"]
    )
    assert activated["trusted_time"] == projection["activated_at"]
    assert superseded["trusted_time"] == activated["trusted_time"]
    assert restarted.query_active(ADMIN)["activated_at"] == activated["trusted_time"]
    assert attempt["started_at"] == activation_at


def test_validation_missing_claim_state_settles_discarded_at_public_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unavailable claim-time candidate cannot escape the public worker:
    the exact live job settles discarded with no validation domain fact."""
    service, _, _ = make_policy_service(tmp_path)
    draft_id = import_draft(service)
    freeze = service.freeze_candidate(
        principal=ADMIN,
        draft_id=draft_id,
        idempotency_key=f"missing-state-freeze-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    candidate_id = freeze["candidate_id"]
    service.request_validation(
        principal=ADMIN,
        candidate_id=candidate_id,
        idempotency_key=f"missing-state-validate-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    original = service._require_candidate_state
    calls = 0

    def missing_once(owner: Any, selected_candidate_id: str) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PolicyNotFound(selected_candidate_id)
        return original(owner, selected_candidate_id)

    monkeypatch.setattr(service, "_require_candidate_state", missing_once)
    result = service.process_next_policy_job()
    assert result["status"] == "discarded", result
    service._store.reload()
    job = next(
        item
        for item in service._store.policy_jobs
        if item.get("candidate_id") == candidate_id and item.get("kind") == "validation"
    )
    assert job["status"] == "complete"
    assert job.get("reason_code") is None
    attempts = _attempts_for(service, job["policy_job_id"])
    assert {attempt["status"] for attempt in attempts} == {"running", "discarded"}
    assert not any(
        event.get("candidate_id") == candidate_id
        and event.get("kind") in {"validated", "rejected"}
        for event in service._store.policy_governance_events
    )


def test_activation_unavailable_scope_settles_stable_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unverifiable frozen scope is a durable fail-closed activation result,
    not an exception escape; it never changes the prior active release."""
    service, rules_path, kb_path = make_policy_service(tmp_path)
    candidate_id, activation_at = _scheduled_candidate(service, "act-scope-unavailable")
    prior_active = service.query_active(ADMIN)

    def unavailable_scope(owner: Any, selected_candidate_id: str, at: int) -> None:
        raise PolicyUnavailable("candidate frozen scope is unavailable")

    monkeypatch.setattr(
        service, "_require_candidate_scope_valid_at", unavailable_scope
    )
    result = service.process_next_policy_job(now=activation_at)
    assert result == {
        "status": "failed",
        "kind": "activation",
        "candidate_id": candidate_id,
        "reason_code": "S08_ACTIVATION_UNAVAILABLE",
    }
    service._store.reload()
    job = next(
        item
        for item in service._store.policy_jobs
        if item.get("candidate_id") == candidate_id and item.get("kind") == "activation"
    )
    assert job["status"] == "diagnostic"
    assert job["reason_code"] == "S08_ACTIVATION_UNAVAILABLE"
    failed = next(
        attempt
        for attempt in _attempts_for(service, job["policy_job_id"])
        if attempt["status"] == "failed"
    )
    assert failed["result"] == {"reason_code": "S08_ACTIVATION_UNAVAILABLE"}
    assert not any(
        event.get("kind") == "activated" and not event.get("bootstrap")
        for event in service._store.policy_governance_events
    )
    restarted = PolicyGovernanceService(
        state_path=service._store.state_path,
        source_rules_path=rules_path,
        source_kb_path=kb_path,
        corpus_root=CORPUS,
    )
    active = restarted.query_active(ADMIN)
    assert active["candidate_id"] == prior_active["candidate_id"]
    assert active["active_generation"] == prior_active["active_generation"]


def test_validation_settlement_clock_loss_retries_then_reclaims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing trusted settlement time never escapes or invents a terminal
    validation fact; the durable lease can be reclaimed after clock recovery."""
    service, _, _ = make_policy_service(tmp_path)
    draft_id = import_draft(service)
    freeze = service.freeze_candidate(
        principal=ADMIN,
        draft_id=draft_id,
        idempotency_key=f"clock-loss-freeze-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    candidate_id = freeze["candidate_id"]
    service.request_validation(
        principal=ADMIN,
        candidate_id=candidate_id,
        idempotency_key=f"clock-loss-validate-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    clock = _FakeClock(1_700_000_000)
    service._clock = clock
    service._clock = lambda: 0
    result = service.process_next_policy_job(now=clock.value)
    assert result == {
        "status": "retry",
        "kind": "validation",
        "candidate_id": candidate_id,
    }
    service._store.reload()
    job = next(
        item
        for item in service._store.policy_jobs
        if item.get("candidate_id") == candidate_id and item.get("kind") == "validation"
    )
    assert job["status"] == "leased"
    assert {item["status"] for item in _attempts_for(service, job["policy_job_id"])} == {
        "running"
    }
    assert not any(
        event.get("candidate_id") == candidate_id
        and event.get("kind") in {"validated", "rejected"}
        for event in service._store.policy_governance_events
    )

    service._clock = clock
    clock.advance(31)
    recovered = service.process_next_policy_job(worker_id="worker-b", now=clock.value)
    assert recovered["status"] == "complete", recovered


def test_activation_settlement_clock_loss_retries_then_reclaims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Activation clock loss leaves the prior active authoritative until the
    durable job is reclaimed with a fresh trusted settlement time."""
    service, _, _ = make_policy_service(tmp_path)
    candidate_id, activation_at = _scheduled_candidate(service, "act-clock-loss")
    prior_active = service.query_active(ADMIN)
    clock = _FakeClock(activation_at)
    service._clock = clock
    service._clock = lambda: 0
    result = service.process_next_policy_job(now=activation_at)
    assert result == {
        "status": "retry",
        "kind": "activation",
        "candidate_id": candidate_id,
    }
    assert service.query_active(ADMIN)["candidate_id"] == prior_active["candidate_id"]
    assert not any(
        event.get("candidate_id") == candidate_id and event.get("kind") == "activated"
        for event in service._store.policy_governance_events
    )

    service._clock = clock
    clock.advance(31)
    recovered = service.process_next_policy_job(worker_id="worker-b", now=clock.value)
    assert recovered["status"] == "complete", recovered
    assert service.query_active(ADMIN)["candidate_id"] == candidate_id


def test_validation_persistent_claim_corruption_settles_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated corrupt candidate lookups stay inside the public worker and
    discard the exact live attempt without publishing a diagnostic lie."""
    service, _, _ = make_policy_service(tmp_path)
    draft_id = import_draft(service)
    freeze = service.freeze_candidate(
        principal=ADMIN,
        draft_id=draft_id,
        idempotency_key=f"corrupt-state-freeze-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )
    candidate_id = freeze["candidate_id"]
    service.request_validation(
        principal=ADMIN,
        candidate_id=candidate_id,
        idempotency_key=f"corrupt-state-validate-{time.time_ns()}",
        expected_governance_revision=governance_revision(service),
    )

    def corrupt_state(owner: Any, selected_candidate_id: str) -> dict[str, Any]:
        raise KeyError(selected_candidate_id)

    monkeypatch.setattr(service, "_require_candidate_state", corrupt_state)
    result = service.process_next_policy_job()
    assert result["status"] == "discarded", result
    service._store.reload()
    job = next(
        item
        for item in service._store.policy_jobs
        if item.get("candidate_id") == candidate_id and item.get("kind") == "validation"
    )
    assert job["status"] == "complete"
    assert job.get("reason_code") is None
    assert {item["status"] for item in _attempts_for(service, job["policy_job_id"])} == {
        "running",
        "discarded",
    }
    assert not any(
        event.get("candidate_id") == candidate_id
        and event.get("kind") in {"validated", "rejected"}
        for event in service._store.policy_governance_events
    )


def test_activation_persistent_claim_corruption_settles_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated corrupt activation-state lookups discard the live attempt and
    preserve the prior active projection without a diagnostic or event lie."""
    service, _, _ = make_policy_service(tmp_path)
    candidate_id, activation_at = _scheduled_candidate(
        service, "act-corrupt-state"
    )
    prior_active = service.query_active(ADMIN)

    def corrupt_state(owner: Any, selected_candidate_id: str) -> dict[str, Any]:
        raise KeyError(selected_candidate_id)

    monkeypatch.setattr(service, "_require_candidate_state", corrupt_state)
    result = service.process_next_policy_job(now=activation_at)
    assert result["status"] == "discarded", result
    service._store.reload()
    job = next(
        item
        for item in service._store.policy_jobs
        if item.get("candidate_id") == candidate_id and item.get("kind") == "activation"
    )
    assert job["status"] == "complete"
    assert job.get("reason_code") is None
    assert {item["status"] for item in _attempts_for(service, job["policy_job_id"])} == {
        "running",
        "discarded",
    }
    assert not any(
        event.get("candidate_id") == candidate_id and event.get("kind") == "activated"
        for event in service._store.policy_governance_events
    )
    assert service.query_active(ADMIN)["candidate_id"] == prior_active["candidate_id"]


# ---------------------------------------------------------------------------
# Ticket #28 R2 Slice 4 — complete S08 governance measurement (SP-12)
# ---------------------------------------------------------------------------


def test_evaluation_governance_measurement_changes_for_any_event_payload_or_revision(
    tmp_path: Path,
) -> None:
    """A governance event payload change that keeps its event id changes the
    measurement: every event row is measured, not only activation ids."""
    import sqlite3 as _sqlite3

    from task4_consistency.controlled.s01_store import _integrity_digest

    service, _rules_path, _kb_path = make_policy_service(tmp_path)
    state_path = tmp_path / "governance.sqlite3"
    before = service.evaluation_governance_measurement()
    store = SQLiteTargetStore(state_path)
    store.reload()
    assert store.policy_governance_events
    event = store.policy_governance_events[-1]
    mutated = copy.deepcopy(event)
    mutated["reason_code"] = "MUTATED-GOVERNANCE-REASON"
    payload_text = json.dumps(
        mutated, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = _integrity_digest(
        "policy_governance_events", event["event_id"], payload_text
    )
    connection = _sqlite3.connect(state_path)
    try:
        connection.execute(
            "UPDATE policy_governance_events SET payload = ?, integrity_sha256 = ? "
            "WHERE item_id = ?",
            (payload_text, digest, event["event_id"]),
        )
        connection.execute(
            "UPDATE s01_immutable_catalog SET integrity_sha256 = ? "
            "WHERE table_name = 'policy_governance_events' AND item_id = ?",
            (digest, event["event_id"]),
        )
        connection.commit()
    finally:
        connection.close()
    after = service.evaluation_governance_measurement()
    assert after != before
