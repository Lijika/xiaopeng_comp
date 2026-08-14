"""S09 pure impact oracle and canonical manifest tests.

Independent expected-member literals pin the conservative closure,
full-scope expansion, completeness blocking and zero-hit proof behavior.
The module is pure: no store, clock, file or network is ever involved.
"""

from __future__ import annotations

import json
import math
import re

import pytest

from task4_consistency.controlled.s09_impact import (
    DEPENDENCY_INDEX_VERSION,
    IMPACT_ENVELOPE_SCHEMA,
    IMPACT_MANIFEST_SCHEMA,
    IMPACT_ORACLE_VERSION,
    ImpactUnprovable,
    build_impact_envelope,
    build_impact_manifest,
    canonical_bytes,
    content_digest,
)

SCOPE = "C-DEMO/demo"

PREDECESSOR = {
    "candidate_id": "candidate_sha256_1111111111111111111111111111111111111111111111111111111111111111",
    "manifest_id": "manifest_sha256_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "manifest_digest": "a" * 64,
    "activation_event_id": "governance_event_0000000000000000000001",
    "active_generation": 1,
    "components": {
        "check_policy": "b" * 64,
        "semantic_catalog": "c" * 64,
        "entity_knowledge": "d" * 64,
        "normalization_policy": "e" * 64,
        "comparison_policy": "f" * 64,
        "readiness_policy": "g" * 64,
        "operators": "h" * 64,
        "normalizers": "i" * 64,
        "checker": "j" * 64,
        "input_contract": "k" * 64,
    },
}

CANDIDATE = {
    "candidate_id": "candidate_sha256_2222222222222222222222222222222222222222222222222222222222222222",
    "manifest_id": "manifest_sha256_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "manifest_digest": "2" * 64,
    "components": {
        "check_policy": "b" * 64,
        "semantic_catalog": "c" * 64,
        "entity_knowledge": "d" * 64,
        "normalization_policy": "e" * 64,
        "comparison_policy": "f" * 64,
        "readiness_policy": "g" * 64,
        "operators": "h" * 64,
        "normalizers": "i" * 64,
        "checker": "j" * 64,
        "input_contract": "k" * 64,
    },
}


def _snapshot(
    applications: list[dict],
    *,
    universe_count: int | None = None,
    complete: bool = True,
) -> dict:
    identities = sorted(
        (str(app["application_id"]), int(app.get("cycle") or 1)) for app in applications
    )
    universe_digest = content_digest(identities)
    return {
        "scope": SCOPE,
        "complete": complete,
        "applications": applications,
        "universe": {
            "complete": complete,
            "count": universe_count if universe_count is not None else len(identities),
            "digest": universe_digest if complete else "",
        },
    }


def _open_cycle(app_id: str, generation: int, **extra: object) -> dict:
    return {
        "application_id": app_id,
        "cycle": 1,
        "partition": "open_cycle",
        "current_run_id": f"run_{app_id}",
        "current_generation": generation,
        "lifecycle_revision": 3,
        "evidence_revision": 2,
        **extra,
    }


def _request(*, phase: str = "preview", candidate: dict | None = None, **extra: object) -> dict:
    return {
        "phase": phase,
        "scope": SCOPE,
        "predecessor": PREDECESSOR,
        "candidate": candidate or CANDIDATE,
        "target_generation": 2,
        "authority_watermarks": {"governance_revision": 7, "lifecycle_watermark": 4},
        "dependency_index": {
            "complete": True,
            "index_digest": "9" * 64,
            "oracle_version": DEPENDENCY_INDEX_VERSION,
        },
        **extra,
    }


def test_canonical_bytes_are_sorted_compact_and_stable() -> None:
    first = {"b": 1, "a": [3, 1, 2], "c": {"y": 2, "x": 1}}
    second = {"c": {"x": 1, "y": 2}, "a": [3, 1, 2], "b": 1}
    encoded = canonical_bytes(first)
    assert canonical_bytes(second) == encoded
    assert b" " not in encoded
    assert encoded.decode("utf-8").index("a") < encoded.decode("utf-8").index("b")
    assert canonical_bytes(first) == json.dumps(
        {"a": [3, 1, 2], "b": 1, "c": {"x": 1, "y": 2}},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def test_canonical_bytes_reject_non_finite() -> None:
    for value in ({"x": float("nan")}, {"x": float("inf")}, [float("-inf")]):
        with pytest.raises(ValueError):
            canonical_bytes(value)


def test_canonical_bytes_reject_forbidden_keys() -> None:
    with pytest.raises(ValueError):
        canonical_bytes({"members": [{"raw": "x"}]})
    with pytest.raises(ValueError):
        canonical_bytes({"ocr_text": "x"})
    with pytest.raises(ValueError):
        canonical_bytes({"attachments": [{"locator": "x"}]})


def test_same_content_fixed_bytes_and_digest() -> None:
    manifest = build_impact_manifest(_request(snapshot=_snapshot([])))
    rebuilt = build_impact_manifest(_request(snapshot=_snapshot([])))
    assert manifest["digest"] == rebuilt["digest"] == manifest["body_sha256"]
    assert manifest["manifest_id"] == f"impact_sha256_{manifest['digest']}"
    assert manifest["schema_version"] == IMPACT_MANIFEST_SCHEMA
    assert manifest["oracle_version"] == IMPACT_ORACLE_VERSION
    assert manifest["zero_hit_proof"] == {
        "complete_universe_count": 0,
        "universe_digest": manifest["universe"]["digest"],
        "oracle_version": IMPACT_ORACLE_VERSION,
    }
    assert manifest["members"] == []
    assert manifest["partitions"]["open_cycle"]["count"] == 0


def test_level_one_transitive_closure_over_affected_current_runs() -> None:
    snapshot = _snapshot(
        [
            _open_cycle("APP-1", generation=1),
            _open_cycle("APP-2", generation=1),
            _open_cycle("APP-3", generation=2),  # already proves target
        ]
    )
    manifest = build_impact_manifest(_request(snapshot=snapshot))
    assert manifest["level"] == 1
    assert manifest["expanded_to_full_scope"] is False
    assert manifest["zero_hit_proof"] is None
    members = manifest["members"]
    assert [m["application_id"] for m in members] == ["APP-1", "APP-2"]
    for member in members:
        assert member["partition"] == "open_cycle"
        assert member["source_generation"] == 1
        assert member["target_generation"] == 2
        assert member["required_disposition"] == "applied"
        assert member["hit_reasons"] == ["release_change"]
        assert member["expected_revisions"] == {
            "lifecycle_revision": 3,
            "evidence_revision": 2,
        }
    # Partition counts/digests bind the deterministic member order.
    assert manifest["partitions"]["open_cycle"]["count"] == 2
    assert manifest["partitions"]["verification_completed"]["count"] == 0


def test_evidence_dependency_change_adds_reason() -> None:
    candidate = dict(CANDIDATE)
    candidate["components"] = dict(CANDIDATE["components"])
    candidate["components"]["readiness_policy"] = "3" * 64
    snapshot = _snapshot([_open_cycle("APP-1", generation=1)])
    manifest = build_impact_manifest(_request(snapshot=snapshot, candidate=candidate))
    assert manifest["level"] == 1
    assert manifest["members"][0]["hit_reasons"] == [
        "release_change",
        "evidence_dependency",
    ]


def test_terminal_partitions_are_exposure_members() -> None:
    snapshot = _snapshot(
        [
            _open_cycle("OPEN-1", generation=1),
            {
                "application_id": "VERIFIED-1",
                "cycle": 1,
                "partition": "verification_completed",
                "current_run_id": "run_VERIFIED-1",
                "current_generation": 1,
                "lifecycle_revision": 5,
                "evidence_revision": 2,
            },
            {
                "application_id": "TERMINATED-1",
                "cycle": 1,
                "partition": "terminated",
                "current_run_id": "run_TERMINATED-1",
                "current_generation": 1,
                "lifecycle_revision": 4,
                "evidence_revision": 2,
            },
            {
                "application_id": "DELETED-1",
                "cycle": 1,
                "partition": "compliance_deleted",
                "current_run_id": "run_DELETED-1",
                "current_generation": 1,
                "lifecycle_revision": 2,
                "evidence_revision": 1,
            },
            {
                "application_id": "VERIFIED-2",
                "cycle": 1,
                "partition": "verification_completed",
                "current_run_id": "run_VERIFIED-2",
                "current_generation": 2,
                "lifecycle_revision": 6,
                "evidence_revision": 2,
            },
        ]
    )
    manifest = build_impact_manifest(_request(snapshot=snapshot))
    by_id = {m["application_id"]: m for m in manifest["members"]}
    assert set(by_id) == {"OPEN-1", "VERIFIED-1", "TERMINATED-1", "DELETED-1"}
    assert by_id["OPEN-1"]["required_disposition"] == "applied"
    for app_id in ("VERIFIED-1", "TERMINATED-1", "DELETED-1"):
        assert by_id[app_id]["required_disposition"] == "historical_terminated_exposure"
        assert by_id[app_id]["hit_reasons"] == ["release_change"]
    assert "VERIFIED-2" not in by_id  # already proves the target generation
    assert manifest["members"] == sorted(
        manifest["members"], key=lambda m: (m["application_id"], m["cycle"])
    )


def test_uncertain_member_expands_to_full_trusted_scope() -> None:
    snapshot = _snapshot(
        [
            _open_cycle("KNOWN-1", generation=1),
            _open_cycle("UNKNOWN-1", generation=None),  # cannot be proved
        ]
    )
    manifest = build_impact_manifest(_request(snapshot=snapshot))
    assert manifest["level"] == 2
    assert manifest["expanded_to_full_scope"] is True
    assert [m["application_id"] for m in manifest["members"]] == ["KNOWN-1", "UNKNOWN-1"]
    assert manifest["members"][0]["hit_reasons"] == ["full_scope_expansion"]
    assert manifest["members"][1]["required_disposition"] == "applied"


def test_incomplete_universe_blocks_with_stable_reason() -> None:
    snapshot = _snapshot([_open_cycle("APP-1", generation=1)], complete=False)
    with pytest.raises(ImpactUnprovable) as error:
        build_impact_manifest(_request(snapshot=snapshot))
    assert error.value.reason_code == "SCOPE_UNIVERSE_INCOMPLETE"
    assert error.value.hold_scope == SCOPE


def test_missing_dependency_index_blocks() -> None:
    request = _request(snapshot=_snapshot([]))
    request["dependency_index"] = {"complete": False}
    with pytest.raises(ImpactUnprovable) as error:
        build_impact_manifest(request)
    assert error.value.reason_code == "DEPENDENCY_INDEX_INCOMPLETE"


def test_unknown_authority_watermark_blocks() -> None:
    request = _request(snapshot=_snapshot([]))
    request["authority_watermarks"] = {"governance_revision": "unknown"}
    with pytest.raises(ImpactUnprovable) as error:
        build_impact_manifest(request)
    assert error.value.reason_code == "AUTHORITY_WATERMARK_UNKNOWN"


def test_missing_lifecycle_watermark_blocks() -> None:
    request = _request(snapshot=_snapshot([]))
    request["authority_watermarks"] = {"governance_revision": 7}
    with pytest.raises(ImpactUnprovable) as error:
        build_impact_manifest(request)
    assert error.value.reason_code == "AUTHORITY_WATERMARK_UNKNOWN"


@pytest.mark.parametrize(
    "dependency_index",
    [
        {"complete": True, "index_digest": ""},
        {
            "complete": True,
            "index_digest": "9" * 64,
            "oracle_version": "unknown-index-version",
        },
    ],
)
def test_unverifiable_dependency_index_blocks(
    dependency_index: dict[str, object],
) -> None:
    request = _request(snapshot=_snapshot([]))
    request["dependency_index"] = dependency_index
    with pytest.raises(ImpactUnprovable) as error:
        build_impact_manifest(request)
    assert error.value.reason_code == "DEPENDENCY_INDEX_INCOMPLETE"


def test_empty_members_without_proof_blocks() -> None:
    snapshot = _snapshot([], universe_count=None, complete=True)
    snapshot["universe"] = {"complete": True, "count": 0, "digest": ""}
    with pytest.raises(ImpactUnprovable) as error:
        build_impact_manifest(_request(snapshot=snapshot))
    assert error.value.reason_code == "SCOPE_UNIVERSE_UNVERIFIABLE"


def test_invalid_member_identity_blocks_with_stable_reason() -> None:
    snapshot = _snapshot([_open_cycle("APP-1", generation=1)])
    snapshot["applications"][0]["cycle"] = "unknown"
    with pytest.raises(ImpactUnprovable) as error:
        build_impact_manifest(_request(snapshot=snapshot))
    assert error.value.reason_code == "MEMBER_IDENTITY_INVALID"


def test_universe_identity_count_and_digest_are_content_bound() -> None:
    snapshot = _snapshot([])
    snapshot["universe"]["count"] = 1
    with pytest.raises(ImpactUnprovable) as error:
        build_impact_manifest(_request(snapshot=snapshot))
    assert error.value.reason_code == "SCOPE_UNIVERSE_UNVERIFIABLE"


def test_manifest_carries_no_raw_or_free_text_fields() -> None:
    manifest = build_impact_manifest(
        _request(snapshot=_snapshot([_open_cycle("APP-1", generation=1)]))
    )
    blob = json.dumps(manifest)
    for forbidden in ("raw", "ocr", "attachment", "locator", "path", "note"):
        assert re.search(rf'"{forbidden}', blob) is None


def test_envelope_binds_preview_digest_and_ceilings() -> None:
    snapshot = _snapshot([_open_cycle("APP-1", generation=1)])
    preview = build_impact_manifest(_request(snapshot=snapshot))
    envelope = build_impact_envelope(
        preview=preview,
        predecessor=PREDECESSOR,
        candidate=CANDIDATE,
        scope=SCOPE,
        risk_class="governed_change",
        dependency_categories=("release_change", "evidence_dependency"),
        required_approvals=("policy_approver",),
        protected_conditions=("no_manual_exclusion", "no_old_success_reuse"),
        max_added_members=1,
        max_total_members=3,
    )
    assert envelope["schema_version"] == IMPACT_ENVELOPE_SCHEMA
    assert envelope["preview_digest"] == preview["digest"]
    assert envelope["predecessor"]["candidate_id"] == PREDECESSOR["candidate_id"]
    assert envelope["candidate"]["candidate_id"] == CANDIDATE["candidate_id"]
    assert envelope["member_delta_rules"]["max_added"] == 1
    assert envelope["count_ceilings"]["max_total"] == 3
    assert envelope["count_ceilings"]["per_partition"]["open_cycle"] == 1
    assert envelope["protected_conditions"] == [
        "no_manual_exclusion",
        "no_old_success_reuse",
    ]
    assert envelope["permitted_authority_movement"]["governance_revision"] == {
        "minimum": 7,
        "maximum": 10,
    }
    assert re.fullmatch(r"[0-9a-f]{64}", envelope["digest"])


def test_envelope_changes_with_preview_digest() -> None:
    preview_a = build_impact_manifest(_request(snapshot=_snapshot([])))
    snapshot_b = _snapshot([_open_cycle("APP-1", generation=1)])
    preview_b = build_impact_manifest(_request(snapshot=snapshot_b))
    kwargs = {
        "predecessor": PREDECESSOR,
        "candidate": CANDIDATE,
        "scope": SCOPE,
        "risk_class": "governed_change",
        "dependency_categories": ("release_change",),
        "required_approvals": ("policy_approver",),
        "protected_conditions": ("no_manual_exclusion",),
        "max_added_members": 0,
        "max_total_members": 1,
    }
    envelope_a = build_impact_envelope(preview=preview_a, **kwargs)
    envelope_b = build_impact_envelope(preview=preview_b, **kwargs)
    assert envelope_a["digest"] != envelope_b["digest"]


def test_manifest_digest_changes_when_member_order_is_reordered() -> None:
    snapshot = _snapshot(
        [_open_cycle("APP-2", generation=1), _open_cycle("APP-1", generation=1)]
    )
    manifest = build_impact_manifest(_request(snapshot=snapshot))
    assert [m["application_id"] for m in manifest["members"]] == ["APP-1", "APP-2"]
