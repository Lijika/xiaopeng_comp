"""S09 conservative impact oracle and canonical manifest (pure module).

The module is intentionally side-effect free: it reads only the immutable
data the caller passes in, uses only the standard library, and never
touches a store, clock, file, network or the current release.  Its single
product callable ``build_impact_manifest`` turns a fixed request mapping
into the canonical, schema-versioned, deterministically sorted impact
manifest whose SHA-256 digest identifies the body.  NaN/Infinity and
non-finite values are rejected; members, dependency sets and reason sets
are deterministically ordered; the module never reads raw field values,
OCR text, attachment locators or free text out of the request.

The oracle fails closed in three levels:

1. when evidence is complete, compute the conservative transitive
   dependency closure across every candidate fact and applicable
   condition (every open-cycle member whose current run cannot already
   prove the target generation is hit);
2. when one member is uncertain but the applicable-scope universe is
   complete and trustworthy, expand impact to the entire release-
   applicable scope;
3. when the scope universe, authority watermark, artifact digest,
   dependency index or required audit completeness cannot be proved,
   raise :class:`ImpactUnprovable` with a stable reason code and the
   smallest trustworthy hold scope.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping

IMPACT_MANIFEST_SCHEMA = "s09-impact-manifest/1"
IMPACT_ENVELOPE_SCHEMA = "s09-impact-envelope/1"
IMPACT_ORACLE_VERSION = "s09-impact-oracle/1"
DEPENDENCY_INDEX_VERSION = "s09-lifecycle-dependency-index/1"

# Dependency categories the oracle reasons over.  A change in any category
# between the predecessor and the candidate release makes every current
# open-cycle run dependent on the release change a member.
DEPENDENCY_CATEGORIES = (
    "release_change",
    "evidence_dependency",
    "semantic_dependency",
    "checker_contract",
)

# Candidate component types whose change is an evidence/semantic dependency
# (readiness, normalization, comparison, semantic catalog, entity
# knowledge), as opposed to a pure check-policy change.
_EVIDENCE_COMPONENT_TYPES = frozenset(
    {
        "semantic_catalog",
        "entity_knowledge",
        "normalization_policy",
        "comparison_policy",
        "readiness_policy",
        "normalizers",
        "input_contract",
    }
)

# The fixed recovery criterion identity every Policy Safety Hold carries.
# Release requires an explicit governed recovery command; this identity is
# part of the hold fact, never a timer or operator checkbox.
HOLD_RECOVERY_CRITERION_ID = "s09-hold-recovery/1"

_REQUIRED_DISPOSITIONS = (
    "applied",
    "already_revalidated",
    "historical_terminated_exposure",
)

_PARTITION_ORDER = (
    "open_cycle",
    "verification_completed",
    "terminating",
    "terminated",
    "compliance_deleted",
)

# Forbidden top-level keys in any manifest/envelope: raw field values, OCR
# text, attachment locators, credentials and free text live outside the
# allowed field set.
_FORBIDDEN_KEYS = frozenset(
    {
        "raw",
        "raw_value",
        "raw_values",
        "ocr_text",
        "ocr",
        "attachment",
        "attachments",
        "locator",
        "locators",
        "path",
        "paths",
        "credential",
        "credentials",
        "password",
        "secret",
        "token",
        "free_text",
        "note",
    }
)


class ImpactUnprovable(Exception):
    """Completeness of the applicable-scope universe (or one of its named
    premises) cannot be proved: activation must be rejected and the
    corresponding scoped Policy Safety Hold established."""

    def __init__(self, reason_code: str, hold_scope: str) -> None:
        super().__init__(f"{reason_code}:{hold_scope}")
        self.reason_code = str(reason_code)
        self.hold_scope = str(hold_scope)


def _reject_non_finite(value: Any, path: str) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"non-finite value at {path}")


def _reject_forbidden_keys(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and key.lower() in _FORBIDDEN_KEYS:
                raise ValueError(f"forbidden key {key!r} at {path}")
            _reject_forbidden_keys(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_forbidden_keys(child, f"{path}[{index}]")
    elif isinstance(value, float):
        _reject_non_finite(value, path)


def canonical_bytes(value: Any) -> bytes:
    """Deterministic, schema-versioned, UTF-8, sorted-key compact JSON.

    ``NaN``/``Infinity`` are rejected before encoding so every byte string
    is reproducible from semantically equal input."""
    _reject_forbidden_keys(value)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except ValueError as error:
        raise ValueError(f"non-canonical manifest content: {error}") from error
    return encoded


def content_digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _component_map(components: Any) -> dict[str, str]:
    if not isinstance(components, dict):
        raise ValueError("components must be a mapping of type -> digest")
    return {
        str(key): str(value)
        for key, value in components.items()
        if str(key) and isinstance(value, str) and value
    }


def _changed_categories(
    predecessor: Mapping[str, Any], candidate: Mapping[str, Any]
) -> tuple[str, ...]:
    before = _component_map(predecessor.get("components"))
    after = _component_map(candidate.get("components"))
    if before == after:
        return ()
    categories: list[str] = []
    if any(after.get(name) != before.get(name) for name in _EVIDENCE_COMPONENT_TYPES):
        categories.append("evidence_dependency")
    if any(
        after.get(name) != before.get(name)
        for name in set(after) | set(before)
        if name not in _EVIDENCE_COMPONENT_TYPES
    ):
        categories.append("release_change")
    return tuple(categories)


def _member_snapshot_items(snapshot: Any) -> list[dict[str, Any]]:
    applications = snapshot.get("applications")
    scope = str(snapshot.get("scope") or "")
    if not isinstance(applications, list):
        raise ImpactUnprovable("UNIVERSE_SNAPSHOT_UNAVAILABLE", scope)
    items = []
    for app in applications:
        if not isinstance(app, dict):
            raise ImpactUnprovable("MEMBER_IDENTITY_INVALID", scope)
        application_id = app.get("application_id")
        cycle = app.get("cycle")
        current_run_id = app.get("current_run_id")
        current_generation = app.get("current_generation")
        if (
            not isinstance(application_id, str)
            or not application_id
            or isinstance(cycle, bool)
            or not isinstance(cycle, int)
            or cycle < 1
            or current_run_id is not None
            and not isinstance(current_run_id, str)
            or current_generation is not None
            and (
                isinstance(current_generation, bool)
                or not isinstance(current_generation, int)
                or current_generation < 1
            )
        ):
            raise ImpactUnprovable("MEMBER_IDENTITY_INVALID", scope)
        partition = str(app.get("partition") or "")
        if partition not in _PARTITION_ORDER:
            raise ImpactUnprovable("PARTITION_UNKNOWN", application_id)
        items.append(
            {
                "application_id": application_id,
                "cycle": cycle,
                "partition": partition,
                "current_run_id": current_run_id,
                "current_generation": current_generation,
                "expected_revisions": {
                    "lifecycle_revision": app.get("lifecycle_revision"),
                    "evidence_revision": app.get("evidence_revision"),
                },
            }
        )
    return items


def _zero_hit_proof(snapshot: Mapping[str, Any]) -> dict[str, Any] | None:
    universe = snapshot.get("universe")
    if not isinstance(universe, dict) or universe.get("complete") is not True:
        return None
    count = int(universe.get("count") or 0)
    universe_digest = str(universe.get("digest") or "")
    if not universe_digest:
        return None
    return {
        "complete_universe_count": count,
        "universe_digest": universe_digest,
        "oracle_version": IMPACT_ORACLE_VERSION,
    }


def _level_one_members(
    items: list[dict[str, Any]],
    target_generation: int,
    changed: tuple[str, ...],
    *,
    evidence_changed: bool,
) -> list[dict[str, Any]]:
    members = []
    for item in items:
        if item["partition"] == "open_cycle":
            current_generation = item["current_generation"]
            if current_generation is not None and current_generation >= target_generation:
                # A current successor already proved the exact new
                # generation: machine-proven non-impact, no member.
                continue
            if current_generation is None:
                # Cannot prove the member already covers the target
                # generation: conservative closure treats it as hit; the
                # caller decides whether uncertainty forces full-scope
                # expansion (level 2).
                continue
            reasons = ["release_change"]
            if evidence_changed:
                reasons.append("evidence_dependency")
            members.append(
                {
                    "application_id": item["application_id"],
                    "cycle": item["cycle"],
                    "partition": item["partition"],
                    "current_run_id": item["current_run_id"],
                    "current_generation": current_generation,
                    "source_generation": current_generation,
                    "target_generation": target_generation,
                    "expected_revisions": item["expected_revisions"],
                    "hit_reasons": tuple(reasons),
                    "required_disposition": "applied",
                }
            )
            continue
        # Terminal partitions (verification_completed, terminated,
        # compliance_deleted) never rewrite history: when their latest run
        # cannot prove the target generation they are members that receive
        # a historical exposure disposition only.
        current_generation = item["current_generation"]
        if current_generation is not None and current_generation >= target_generation:
            continue
        members.append(
            {
                "application_id": item["application_id"],
                "cycle": item["cycle"],
                "partition": item["partition"],
                "current_run_id": item["current_run_id"],
                "current_generation": current_generation,
                "source_generation": current_generation,
                "target_generation": target_generation,
                "expected_revisions": item["expected_revisions"],
                "hit_reasons": ("release_change",),
                "required_disposition": "historical_terminated_exposure",
            }
        )
    return members


def _partition_members(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    partitions: dict[str, list[dict[str, Any]]] = {name: [] for name in _PARTITION_ORDER}
    for item in items:
        partitions[item["partition"]].append(item)
    return partitions


def _member_sort_key(member: Mapping[str, Any]) -> tuple[str, int]:
    return (str(member["application_id"]), int(member["cycle"]))


def _partition_counts_and_digests(
    members: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    counts: dict[str, dict[str, Any]] = {}
    for name in _PARTITION_ORDER:
        partition_members = [
            member for member in members if member["partition"] == name
        ]
        counts[name] = {
            "count": len(partition_members),
            "digest": content_digest(
                [(_member_sort_key(m), m["required_disposition"]) for m in partition_members]
            ),
        }
    return counts


def build_impact_envelope(
    *,
    preview: Mapping[str, Any],
    predecessor: Mapping[str, Any],
    candidate: Mapping[str, Any],
    scope: str,
    risk_class: str,
    dependency_categories: tuple[str, ...],
    required_approvals: tuple[str, ...],
    protected_conditions: tuple[str, ...],
    max_added_members: int,
    max_total_members: int,
) -> dict[str, Any]:
    """The machine-decidable approval envelope binding the exact preview
    digest, predecessor/candidate digests, scope, oracle version,
    dependency categories, risk class, permitted member delta and count
    ceilings.  Approval binds this digest; any drift outside it requires a
    new preview and a new approval."""
    if not isinstance(max_added_members, int) or max_added_members < 0:
        raise ValueError("max_added_members must be a non-negative integer")
    if not isinstance(max_total_members, int) or max_total_members < 1:
        raise ValueError("max_total_members must be a positive integer")
    preview_watermarks = preview.get("authority_watermarks") or {}
    if not all(
        isinstance(preview_watermarks.get(key), int)
        and not isinstance(preview_watermarks.get(key), bool)
        for key in ("governance_revision", "lifecycle_watermark")
    ):
        raise ValueError("preview authority watermarks are not verifiable")
    preview_dependency = preview.get("dependency_index") or {}
    dependency_digest = str(preview_dependency.get("index_digest") or "")
    if (
        preview_dependency.get("complete") is not True
        or not dependency_digest
        or preview_dependency.get("oracle_version") != DEPENDENCY_INDEX_VERSION
    ):
        # Fail closed: the envelope never binds an unproven dependency-index
        # proof fact.
        raise ValueError("preview dependency index is not verifiable")
    envelope = {
        "schema_version": IMPACT_ENVELOPE_SCHEMA,
        "preview_digest": str(preview["digest"]),
        "predecessor": {
            "candidate_id": str(predecessor["candidate_id"]),
            "manifest_digest": str(predecessor["manifest_digest"]),
        },
        "candidate": {
            "candidate_id": str(candidate["candidate_id"]),
            "manifest_digest": str(candidate["manifest_digest"]),
        },
        "scope": scope,
        "oracle_version": str(preview.get("oracle_version") or IMPACT_ORACLE_VERSION),
        "authority_watermarks": {
            "governance_revision": int(preview_watermarks["governance_revision"]),
            "lifecycle_watermark": int(preview_watermarks["lifecycle_watermark"]),
        },
        "permitted_authority_movement": {
            "governance_revision": {
                "minimum": int(preview_watermarks["governance_revision"]),
                "maximum": int(preview_watermarks["governance_revision"]) + 3,
            },
            "lifecycle_watermark": {
                "minimum": int(preview_watermarks["lifecycle_watermark"]),
                "maximum": int(preview_watermarks["lifecycle_watermark"]),
            },
        },
        "dependency_index": {
            "complete": True,
            "index_digest": dependency_digest,
            "oracle_version": str(
                preview_dependency.get("oracle_version") or ""
            ),
        },
        "dependency_categories": sorted(dependency_categories),
        "risk_class": risk_class,
        "member_delta_rules": {
            "max_added": max_added_members,
            "removal": "machine_proof_only",
        },
        "count_ceilings": {
            "max_total": max_total_members,
            "per_partition": dict(
                sorted(
                    {
                        name: info["count"]
                        for name, info in preview["partitions"].items()
                    }.items()
                )
            ),
        },
        "required_approvals": sorted(required_approvals),
        "protected_conditions": sorted(protected_conditions),
    }
    envelope["digest"] = content_digest(envelope)
    return envelope


def build_impact_manifest(request: Mapping[str, Any]) -> dict[str, Any]:
    """The only product callable: build the canonical impact manifest.

    ``request`` carries only phase, predecessor/candidate refs, scope,
    authority watermarks, dependency-index completeness, the Lifecycle-owned
    member snapshot and an optional approval envelope.  The returned mapping
    contains the canonical header, deterministically ordered members,
    partition counts/digests, zero-hit proof and the body SHA-256."""
    phase = str(request.get("phase") or "")
    if phase not in {"preview", "final"}:
        raise ValueError("phase must be 'preview' or 'final'")
    scope = str(request.get("scope") or "")
    if not scope:
        raise ValueError("scope is required")
    predecessor = request.get("predecessor")
    candidate = request.get("candidate")
    if not isinstance(predecessor, Mapping) or not isinstance(candidate, Mapping):
        raise ValueError("predecessor and candidate refs are required")
    snapshot = request.get("snapshot")
    if not isinstance(snapshot, Mapping):
        raise ImpactUnprovable("UNIVERSE_SNAPSHOT_UNAVAILABLE", scope)
    watermarks = request.get("authority_watermarks")
    if not isinstance(watermarks, Mapping) or not all(
        isinstance(watermarks.get(key), int)
        and not isinstance(watermarks.get(key), bool)
        and int(watermarks[key]) >= 0
        for key in ("governance_revision", "lifecycle_watermark")
    ):
        raise ImpactUnprovable("AUTHORITY_WATERMARK_UNKNOWN", scope)
    dependency_index = request.get("dependency_index")
    if (
        not isinstance(dependency_index, Mapping)
        or dependency_index.get("complete") is not True
        or not isinstance(dependency_index.get("index_digest"), str)
        or not str(dependency_index.get("index_digest"))
        or dependency_index.get("oracle_version") != DEPENDENCY_INDEX_VERSION
    ):
        raise ImpactUnprovable("DEPENDENCY_INDEX_INCOMPLETE", scope)
    universe = snapshot.get("universe")
    if not isinstance(universe, Mapping) or universe.get("complete") is not True:
        raise ImpactUnprovable("SCOPE_UNIVERSE_INCOMPLETE", scope)
    if not str(universe.get("digest") or ""):
        raise ImpactUnprovable("SCOPE_UNIVERSE_UNVERIFIABLE", scope)

    items = _member_snapshot_items(snapshot)
    identities = sorted((item["application_id"], item["cycle"]) for item in items)
    universe_count = universe.get("count")
    universe_digest = universe.get("digest")
    if (
        isinstance(universe_count, bool)
        or not isinstance(universe_count, int)
        or universe_count != len(identities)
        or len(set(identities)) != len(identities)
        or not isinstance(universe_digest, str)
        or universe_digest != content_digest(identities)
    ):
        raise ImpactUnprovable("SCOPE_UNIVERSE_UNVERIFIABLE", scope)
    target_generation = int(request.get("target_generation") or 0)
    if target_generation < 1:
        raise ValueError("target_generation is required")
    if any(
        item["partition"] == "open_cycle"
        and item["current_generation"] is None
        for item in items
    ):
        # A member whose current generation cannot be proved: expand to the
        # smallest trustworthy complete scope (the entire applicable
        # universe) instead of guessing per-member.
        expanded = True
        members = [
            {
                "application_id": item["application_id"],
                "cycle": item["cycle"],
                "partition": item["partition"],
                "current_run_id": item["current_run_id"],
                "current_generation": item["current_generation"],
                "source_generation": item["current_generation"],
                "target_generation": target_generation,
                "expected_revisions": item["expected_revisions"],
                "hit_reasons": ("full_scope_expansion",),
                "required_disposition": (
                    "applied"
                    if item["partition"] == "open_cycle"
                    else "historical_terminated_exposure"
                ),
            }
            for item in items
        ]
        level = 2
    else:
        expanded = False
        changed = _changed_categories(predecessor, candidate)
        if not changed:
            changed = ("release_change",)
        evidence_changed = "evidence_dependency" in changed
        members = _level_one_members(
            items,
            target_generation,
            changed,
            evidence_changed=evidence_changed,
        )
        level = 1

    members.sort(key=_member_sort_key)
    zero_hit_proof = _zero_hit_proof(snapshot) if not members else None

    manifest = {
        "schema_version": IMPACT_MANIFEST_SCHEMA,
        "phase": phase,
        "oracle_version": IMPACT_ORACLE_VERSION,
        "scope": scope,
        "level": level,
        "expanded_to_full_scope": expanded,
        "blocked": False,
        "predecessor": {
            "candidate_id": str(predecessor["candidate_id"]),
            "manifest_id": str(predecessor.get("manifest_id") or ""),
            "manifest_digest": str(predecessor["manifest_digest"]),
            "activation_event_id": str(predecessor.get("activation_event_id") or ""),
            "active_generation": int(predecessor.get("active_generation") or 0),
        },
        "candidate": {
            "candidate_id": str(candidate["candidate_id"]),
            "manifest_id": str(candidate.get("manifest_id") or ""),
            "manifest_digest": str(candidate["manifest_digest"]),
            "components": _component_map(candidate.get("components")),
        },
        "authority_watermarks": dict(watermarks),
        "dependency_index": {
            "complete": True,
            "index_digest": str(dependency_index.get("index_digest") or ""),
            "oracle_version": DEPENDENCY_INDEX_VERSION,
        },
        "approval_envelope": dict(request.get("approval_envelope") or {}),
        "delta_rules": {
            "max_added_members": int(request.get("max_added_members") or 0),
            "member_removal": "machine_proof_only",
            "full_scope_expansion_allowed": bool(expanded),
        },
        "universe": {
            "complete": True,
            "count": int(universe.get("count") or 0),
            "digest": str(universe.get("digest") or ""),
        },
        "partitions": _partition_counts_and_digests(members),
        "required_dispositions": list(_REQUIRED_DISPOSITIONS),
        "zero_hit_proof": zero_hit_proof,
        "members": [dict(member) for member in members],
    }
    manifest["members"] = [
        {
            **member,
            "hit_reasons": list(member["hit_reasons"]),
        }
        for member in manifest["members"]
    ]
    body = {
        key: value
        for key, value in manifest.items()
        if key not in {"digest", "manifest_id", "body_sha256"}
    }
    manifest["digest"] = content_digest(body)
    manifest["body_sha256"] = manifest["digest"]
    manifest["manifest_id"] = f"impact_sha256_{manifest['digest']}"
    return manifest


def verify_impact_manifest_digest(manifest: Mapping[str, Any]) -> bool:
    """Recompute the canonical body digest of a (reloaded) manifest and
    compare it with the identity fields it carries."""
    if not isinstance(manifest, Mapping):
        return False
    body = {
        key: value
        for key, value in manifest.items()
        if key not in {"digest", "manifest_id", "body_sha256"}
    }
    try:
        return content_digest(body) == manifest.get("digest")
    except ValueError:
        return False
