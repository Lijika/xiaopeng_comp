"""S08 governed policy release authority.

One concrete ``PolicyGovernanceService`` owns the Policy Artifact Registry and
the Governance Ledger for the fixed C-DEMO scope:

- the Registry stores immutable, content-addressed artifacts (raw legacy
  sources, canonical runtime components, candidate manifests, validation
  bundles, approval bindings, mapping ledgers);
- the Ledger folds candidate state from append-only governance events:
  ``draft -> candidate -> validated -> in_review -> approved -> scheduled ->
  active -> superseded`` with ``rejected`` and pre-activation ``cancelled``.

Drafts are a mutable, non-authoritative workspace.  The active projection is
a rebuildable derivative and is the only release the S01 runtime resolver
reads.  No command accepts paths, URLs, code, I/O or credentials in the body;
the offline importer only references a server allowlisted source bundle id.
"""

from __future__ import annotations

import concurrent.futures
import copy
import hashlib
import json
import platform
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from task4_consistency.kb.store import (
    _validate_address_alias,
    project_graph_to_aliases,
)
from task4_consistency.rules.loader import RuleDef

from task4_consistency.controlled.s01_checker import (
    ProtectedInvariantError,
    _TARGET_CHECKER_BUILD,
    TargetChecker,
    TargetRelease,
)
from task4_consistency.controlled.s01_store import (
    AuditOutboxOwner,
    ScheduleReservationConflict,
    SQLiteTargetStore,
    StaleStoreRevision,
)
from task4_consistency.controlled.s09_diagnostics import (
    S09DiagnosticBundleWriter,
    S09DiagnosticRunner,
    S09DiagnosticView,
)
from task4_consistency.controlled.s09_impact import (
    DEPENDENCY_INDEX_VERSION,
    HOLD_RECOVERY_CRITERION_ID,
    IMPACT_ENVELOPE_SCHEMA,
    IMPACT_MANIFEST_SCHEMA,
    ImpactUnprovable,
    build_impact_envelope,
    build_impact_manifest,
    content_digest as s09_content_digest,
    verify_impact_manifest_digest,
)

S08_SCOPE = "C-DEMO/demo"
SOURCE_BUNDLE_ID = "c-demo-legacy-baseline/1"
GOVERNANCE_SCHEMA = "s08-governance-event/1"
ARTIFACT_SCHEMA = "s08-artifact/1"
MANIFEST_SCHEMA = "s08-candidate-manifest/1"
MAPPING_LEDGER_SCHEMA = "s08-mapping-ledger/1"
VALIDATION_BUNDLE_SCHEMA = "s08-validation-bundle/1"
APPROVAL_BINDING_SCHEMA = "s08-approval-binding/1"
RESERVATION_SCHEMA = "s08-schedule-reservation/1"
IMPORTER_VERSION = "s08-importer/1"
VALIDATOR_SUITE = "s08-validation-suite/1"
VALIDATOR_BUILD = "s08-validator/3"

# Registered stable worker-failure reason codes: the only values a diagnostic
# job row or a public outcome may carry.  Raw exception text and internal
# write points never leave the service boundary.
_WORKER_REASON_CODES: dict[str, dict[str, str]] = {
    "validation": {
        "PolicyUnavailable": "S08_VALIDATION_UNAVAILABLE",
        "PolicyInvalidTransition": "S08_VALIDATION_INVALID_STATE",
        "PolicyConflict": "S08_VALIDATION_CONFLICT",
        "default": "S08_VALIDATION_INTERNAL",
    },
    "activation": {
        "PolicyUnavailable": "S08_ACTIVATION_UNAVAILABLE",
        "PolicyInvalidTransition": "S08_ACTIVATION_INVALID_STATE",
        "PolicyConflict": "S08_ACTIVATION_CONFLICT",
        "default": "S08_ACTIVATION_INTERNAL",
    },
}
_READINESS_POLICY_ID = "c-demo-readiness/1"
_PROTECTED_CHECK_IDS = frozenset({"R_VIN_CROSS", "R_ENGINE_CROSS", "R_ID_EXACT"})
_BOOTSTRAP_REASON = "S08 one-time bootstrap migration release"

# G4: the mapping ledger resolves every server-owned source item.  The
# known option set mirrors exactly what the rules loader compiles; any
# top-level key outside it is an explicit unsupported entry that blocks
# validation instead of being silently dropped.
_RULES_OPTION_POINTERS = (
    "/package",
    "/version",
    "/low_confidence_threshold",
    "/default_require_all_docs",
    "/date_order",
    "/vin_fix_ioq",
    "/vin_strict_check_digit",
    "/expand_id15_to_18",
    "/critical_low_conf_compare",
)
_RULES_KNOWN_TOP_LEVEL = frozenset(
    {
        "package",
        "version",
        "low_confidence_threshold",
        "default_require_all_docs",
        "date_order",
        "vin_fix_ioq",
        "vin_strict_check_digit",
        "expand_id15_to_18",
        "critical_low_conf_compare",
        "field_aliases",
        "rules",
        "changelog",
    }
)
# The closed consumed-field set of the rules loader (RuleDef.from_dict).
_RULE_KNOWN_FIELDS = frozenset(
    {
        "id",
        "name",
        "type",
        "field",
        "docs",
        "on_missing",
        "severity",
        "threshold",
        "uncertain_band",
        "abs_tol",
        "rel_tol",
        "list_field",
        "item_field",
        "if_field_present",
        "required_field",
        "min_confidence",
        "require_all_docs",
        "field_type",
        "transfer_name_policy",
        "transfer_old_docs",
        "transfer_new_docs",
    }
)
_KB_KNOWN_SECTIONS = frozenset(
    {
        "address_aliases",
        "org_aliases",
        "plate_prefixes",
        "graph",
        "version",
        "description",
    }
)
# G4 resource boundary: bounded fresh-process evidence input/output.  The
# declared checker limits stay authoritative for per-run cardinality; these
# bounds only cap the transport and the evidence surface.
_MAX_EVIDENCE_INPUT_BYTES = 64 * 1024 * 1024
_MAX_CORPUS_ITEMS = 5000
_MAX_SUBPROCESS_STDOUT_BYTES = 16 * 1024 * 1024
_MAX_SUBPROCESS_STDERR_BYTES = 4 * 1024 * 1024

# Normal-state transitions; a candidate may also be rejected from validated
# or in_review and cancelled from any pre-activation state.
_STATE_SUCCESSORS = {
    "candidate": frozenset({"validated", "rejected", "cancelled"}),
    "validated": frozenset({"in_review", "rejected", "cancelled"}),
    "in_review": frozenset({"approved", "rejected", "cancelled"}),
    "approved": frozenset({"scheduled", "cancelled"}),
    "scheduled": frozenset({"active", "cancelled"}),
    "active": frozenset({"superseded"}),
    "superseded": frozenset(),
    "rejected": frozenset(),
    "cancelled": frozenset(),
}

_ACTIVATION_KINDS = frozenset({"activated", "superseded"})


def canonical_bytes(value: Any) -> bytes:
    """One canonical UTF-8 encoding: sorted keys, compact separators and no
    NaN/Infinity (the registry rejects non-canonical payloads)."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def content_digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def raw_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class _UniqueKeySafeLoader(yaml.SafeLoader):
    def construct_mapping(
        self, node: yaml.MappingNode, deep: bool = False
    ) -> dict[Any, Any]:
        self.flatten_mapping(node)
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in seen
            except TypeError as error:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found unhashable mapping key",
                    key_node.start_mark,
                ) from error
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key ({key!r})",
                    key_node.start_mark,
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _load_yaml_source(raw: bytes) -> Any:
    return yaml.load(raw.decode("utf-8"), Loader=_UniqueKeySafeLoader)


def _load_json_source(raw: bytes) -> Any:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)


def _graph_mapping_items(
    graph: Any, release: TargetRelease
) -> list[dict[str, Any]]:
    def item(
        pointer: str,
        value: Any,
        classification: str,
        target_ref: str | None,
        reason: str,
        result: Any,
    ) -> dict[str, Any]:
        return {
            "source_ref": "knowledge",
            "source_pointer": pointer,
            "source_digest": content_digest(("kb_graph_source", pointer, value)),
            "classification": classification,
            "target_ref": target_ref,
            "importer_version": IMPORTER_VERSION,
            "reason": reason,
            "result_digest": content_digest(result),
        }

    if not isinstance(graph, dict):
        return [
            item(
                "/graph",
                graph,
                "unsupported",
                None,
                "entity graph is not an object",
                ("kb_graph_invalid", graph),
            )
        ]

    entries: list[dict[str, Any]] = []
    for key in ("version", "description"):
        if key in graph:
            entries.append(
                item(
                    f"/graph/{key}",
                    graph[key],
                    "non_runtime_excluded",
                    None,
                    "graph metadata cannot affect runtime",
                    ("kb_graph_metadata", key),
                )
            )

    release_knowledge = {
        section: dict(values) for section, values in release.knowledge
    }
    nodes = graph.get("nodes")
    if not isinstance(nodes, list):
        entries.append(
            item(
                "/graph/nodes",
                nodes,
                "unsupported",
                None,
                "graph nodes must be a list",
                ("kb_graph_nodes_invalid", nodes),
            )
        )
        nodes_for_projection: list[Any] = []
    else:
        nodes_for_projection = nodes

    edges = graph.get("edges")
    if not isinstance(edges, list):
        entries.append(
            item(
                "/graph/edges",
                edges,
                "unsupported",
                None,
                "graph edges must be a list",
                ("kb_graph_edges_invalid", edges),
            )
        )
    else:
        same_as_targets: dict[str, set[str]] = {}
        for edge in edges:
            if isinstance(edge, dict) and edge.get("rel") == "same_as":
                source_id = str(edge.get("src") or "")
                target_id = str(edge.get("dst") or "")
                if source_id and target_id:
                    same_as_targets.setdefault(source_id, set()).add(target_id)
        conflicting_sources = {
            source_id
            for source_id, targets in same_as_targets.items()
            if len(targets) > 1
        }
        for index, edge in enumerate(edges):
            pointer = f"/graph/edges/{index}"
            relation = edge.get("rel") if isinstance(edge, dict) else None
            if not isinstance(edge, dict) or set(edge) - {"src", "rel", "dst"}:
                entries.append(
                    item(
                        pointer,
                        edge,
                        "unsupported",
                        None,
                        "graph edge contains unknown or malformed fields",
                        ("kb_graph_edge_unsupported", edge),
                    )
                )
                continue
            if relation in {"part_of", "type", "not_same_as"}:
                entries.append(
                    item(
                        pointer,
                        edge,
                        "non_runtime_excluded",
                        None,
                        f"{relation} is recorded but is not a runtime alias relation",
                        ("kb_graph_edge_excluded", relation),
                    )
                )
                continue
            if relation == "same_as":
                source_id = str(edge.get("src") or "")
                if source_id in conflicting_sources:
                    entries.append(
                        item(
                            pointer,
                            edge,
                            "unsupported",
                            None,
                            "same_as source maps to conflicting targets",
                            ("kb_graph_same_as_conflict", source_id),
                        )
                    )
                    continue
                edge_projection = project_graph_to_aliases(
                    {"nodes": nodes_for_projection, "edges": [edge]}
                )
                mapped = [
                    (section, key)
                    for section, values in edge_projection.items()
                    for key in values
                ]
                if len(mapped) == 1:
                    section, key = mapped[0]
                    resolved = release_knowledge.get(section, {}).get(key)
                    entries.append(
                        item(
                            pointer,
                            edge,
                            "explicit_transform",
                            f"checker.knowledge/{section}/{key}",
                            "same_as edge projected with explicit alias priority",
                            ("kb_graph_alias", section, key, resolved),
                        )
                    )
                    continue
                if source_id.startswith("brand:"):
                    entries.append(
                        item(
                            pointer,
                            edge,
                            "non_runtime_excluded",
                            None,
                            "brand same_as is intentionally not collapsed into aliases",
                            ("kb_graph_brand_same_as_excluded", edge),
                        )
                    )
                    continue
            entries.append(
                item(
                    pointer,
                    edge,
                    "unsupported",
                    None,
                    "graph relation has no registered runtime mapping",
                    ("kb_graph_edge_unsupported", edge),
                )
            )

    if isinstance(nodes, list):
        node_ids = [
            str(node.get("id") or "") if isinstance(node, dict) else ""
            for node in nodes
        ]
        node_id_counts = {
            node_id: node_ids.count(node_id) for node_id in set(node_ids) if node_id
        }
        mapped_node_ids = {
            str(edge.get(endpoint) or "")
            for edge in (edges if isinstance(edges, list) else [])
            if isinstance(edge, dict)
            and edge.get("rel") == "same_as"
            and str(edge.get("src") or "").startswith(("addr:", "org:"))
            and str(edge.get("src") or "") not in (
                conflicting_sources if isinstance(edges, list) else set()
            )
            and not (set(edge) - {"src", "rel", "dst"})
            for endpoint in ("src", "dst")
        }
        for index, node in enumerate(nodes):
            pointer = f"/graph/nodes/{index}"
            node_id = node_ids[index]
            valid = (
                isinstance(node, dict)
                and not (set(node) - {"id", "type", "label"})
                and bool(node_id)
                and node_id_counts.get(node_id) == 1
                and isinstance(node.get("label"), str)
                and bool(node["label"].strip())
                and (
                    "type" not in node
                    or isinstance(node.get("type"), str)
                    and bool(node["type"].strip())
                )
            )
            mapped = valid and node_id in mapped_node_ids
            entries.append(
                item(
                    pointer,
                    node,
                    (
                        "explicit_transform"
                        if mapped
                        else "non_runtime_excluded" if valid else "unsupported"
                    ),
                    "checker.knowledge.aliases" if mapped else None,
                    (
                        "node identity and label resolve a supported same_as mapping"
                        if mapped
                        else "node does not feed a supported runtime alias"
                        if valid
                        else "graph node fields, identity, type and label are invalid"
                    ),
                    ("kb_graph_node", node_id, node),
                )
            )

    for key in sorted(set(graph) - {"version", "description", "nodes", "edges"}):
        entries.append(
            item(
                f"/graph/{key}",
                graph[key],
                "unsupported",
                None,
                "unrecognized graph property may not be silently dropped",
                ("kb_graph_property_unsupported", key, graph[key]),
            )
        )
    return entries


# Reproducible identity for every module that decides validation status or
# executes the fresh-process checker.  Computed once at import time.
_VALIDATOR_CODE_DIGEST = content_digest(
    [
        (path.name, raw_digest(path.read_bytes()))
        for path in sorted(
            Path(__file__).resolve().parent / name
            for name in ("s01_checker.py", "s08.py", "s08_validate.py")
        )
    ]
)


class PolicyNotFound(KeyError):
    """A governed object does not exist (existence is hidden from callers)."""


class PolicyConflict(RuntimeError):
    """Stale revision, same-key/different-fingerprint, or transition conflict."""


class PolicyInvalidTransition(RuntimeError):
    """An illegal candidate transition or actor/role/scope violation."""


class PolicyUnavailable(RuntimeError):
    """Registry/Ledger integrity cannot be proven; resolution fails closed."""


class GovernedReleaseNotFound(PolicyUnavailable):
    """The governed release identity resolves to no single verified release:
    a healthy unknown reference (a caller command error), distinct from an
    authority outage or corruption."""


@dataclass(frozen=True)
class PolicyPrincipal:
    subject: str
    role: str  # "admin" | "approver" | "operator" | "auditor" | ...
    scope: str
    source_id: str


def _validate_principal(principal: PolicyPrincipal | None) -> None:
    if not isinstance(principal, PolicyPrincipal):
        raise PolicyInvalidTransition("policy principal is missing")
    if (
        not isinstance(principal.subject, str)
        or not principal.subject
        or principal.subject.strip() != principal.subject
        or len(principal.subject) > 200
    ):
        raise PolicyInvalidTransition("policy principal subject is invalid")
    if principal.role not in {
        "admin",
        "approver",
        "operator",
        "auditor",
        "replay_operator",
        "simulation_operator",
    }:
        raise PolicyInvalidTransition("policy principal role is invalid")
    if principal.scope != S08_SCOPE:
        raise PolicyInvalidTransition("policy principal scope is not served")
    if (
        not isinstance(principal.source_id, str)
        or not principal.source_id
        or principal.source_id.strip() != principal.source_id
    ):
        raise PolicyInvalidTransition("policy principal source is invalid")


class PolicyGovernanceService:
    """Artifact Registry + Governance Ledger for one C-DEMO policy scope."""

    def __init__(
        self,
        *,
        state_path: str | Path,
        clock: Callable[[], int] | None = None,
        audit_available: bool = True,
        storage_available: bool = True,
        fault_injector: Callable[[str], None] | None = None,
        migration_admin_subject: str = "c-demo-migration-admin",
        admin_subject: str = "c-demo-policy-admin",
        approver_subject: str = "c-demo-policy-approver",
        operator_subject: str = "c-demo-policy-operator",
        source_rules_path: str | Path | None = None,
        source_kb_path: str | Path | None = None,
        corpus_root: str | Path | None = None,
        lifecycle_snapshot_provider: (
            Callable[
                [SQLiteTargetStore, str | None], dict[str, Any]
            ]
            | None
        ) = None,
        diagnostic_snapshot_provider: (
            Callable[[SQLiteTargetStore, str], dict[str, Any]] | None
        ) = None,
    ) -> None:
        for name, value in (
            ("migration admin", migration_admin_subject),
            ("policy admin", admin_subject),
            ("policy approver", approver_subject),
            ("policy operator", operator_subject),
        ):
            if (
                not isinstance(value, str)
                or not value
                or value.strip() != value
                or len(value) > 200
            ):
                raise ValueError(f"{name} subject must be canonical")
        if len({migration_admin_subject, admin_subject, approver_subject, operator_subject}) != 4:
            raise ValueError(
                "migration/admin/approver/operator subjects must be distinct"
            )
        self._migration_admin_subject = migration_admin_subject
        self._admin_subject = admin_subject
        self._approver_subject = approver_subject
        self._operator_subject = operator_subject
        self.audit_available = audit_available
        self.storage_available = storage_available
        self._fault_injector = fault_injector
        self._clock = clock or (lambda: int(time.time()))
        self._source_rules_path = (
            Path(source_rules_path).resolve()
            if source_rules_path is not None
            else None
        )
        self._source_kb_path = (
            Path(source_kb_path).resolve() if source_kb_path is not None else None
        )
        self._corpus_root = (
            Path(corpus_root).resolve() if corpus_root is not None else None
        )
        self._store = SQLiteTargetStore(state_path)
        self._lock = threading.RLock()
        self._checker_cache: dict[str, TargetChecker] = {}
        self._lifecycle_snapshot_provider = lifecycle_snapshot_provider
        self._diagnostic_snapshot_provider = diagnostic_snapshot_provider

    # ------------------------------------------------------------------ ids

    @staticmethod
    def _stable_id(prefix: str, fingerprint: str) -> str:
        return f"{prefix}_{hashlib.sha256(fingerprint.encode()).hexdigest()[:24]}"

    def _idempotency_key(
        self, principal: PolicyPrincipal, action: str, idempotency_key: str
    ) -> str:
        return self._stable_id(
            "s08_idem",
            f"{principal.scope}/{principal.subject}/{principal.role}/{action}/{idempotency_key}",
        )

    @staticmethod
    def _fingerprint(*values: Any) -> str:
        return hashlib.sha256(canonical_bytes(values)).hexdigest()

    # ------------------------------------------------------------ store/io

    def _before_write(self, write_point: str) -> None:
        if self._fault_injector is None:
            return
        try:
            self._fault_injector(write_point)
        except Exception as error:
            raise RuntimeError(write_point) from error

    def _trusted_time(self) -> int:
        value = int(self._clock())
        if value < 1:
            raise PolicyUnavailable("trusted clock is unavailable")
        return value

    def _audit_time_fields(self, staged: SQLiteTargetStore) -> dict[str, Any]:
        event_time = max(
            self._trusted_time(),
            max(
                (
                    int(record.get("event_time", 0))
                    for record in staged.audit_events
                    if isinstance(record.get("event_time"), int)
                ),
                default=self._trusted_time(),
            ),
        )
        sequence = 1 + sum(
            int(record.get("event_sequence", 0)) >= 1 for record in staged.audit_events
        )
        return {
            "event_time": event_time,
            "event_sequence": sequence,
            "event_time_key": f"{event_time:020d}:{sequence:010d}",
        }

    def _append_audit(
        self,
        staged: SQLiteTargetStore,
        *,
        action: str,
        principal: PolicyPrincipal,
        result: str,
        reason_code: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "event_id": self._stable_id(
                "audit", f"s08_{action}:{len(staged.audit_events) + 1}"
            ),
            "action": f"s08_{action}",
            "subject": principal.subject,
            "role": principal.role,
            "scope": principal.scope,
            "source_id": principal.source_id,
            "result": result,
            "reason_code": reason_code,
            **self._audit_time_fields(staged),
        }
        if details:
            payload.update(details)
        # The audit collection is owned by the owner seam: S08 submits the
        # immutable record and never appends the collection itself.
        AuditOutboxOwner(staged).append_audit(payload)

    def _append_governance_event(
        self,
        staged: SQLiteTargetStore,
        *,
        kind: str,
        principal: PolicyPrincipal,
        reason_code: str,
        details: dict[str, Any] | None = None,
        trusted_time: int | None = None,
    ) -> dict[str, Any]:
        revision = len(staged.policy_governance_events) + 1
        event: dict[str, Any] = {
            "event_id": self._stable_id(
                "governance", f"{principal.scope}:{revision}:{kind}"
            ),
            "schema_version": GOVERNANCE_SCHEMA,
            "scope": principal.scope,
            "revision": revision,
            "kind": kind,
            "actor": {
                "subject": principal.subject,
                "role": principal.role,
                "source_id": principal.source_id,
            },
            "trusted_time": (
                self._trusted_time() if trusted_time is None else trusted_time
            ),
            "reason_code": reason_code,
        }
        if details:
            event.update(details)
        staged.policy_governance_events.append(event)
        return event

    @staticmethod
    def _verify_governance_revision(
        staged: SQLiteTargetStore, expected: int
    ) -> None:
        if (
            isinstance(expected, bool)
            or not isinstance(expected, int)
            or expected < 0
        ):
            raise PolicyConflict("expected governance revision is invalid")
        current = len(staged.policy_governance_events)
        if expected != current:
            raise PolicyConflict(
                f"stale governance revision: expected {expected}, found {current}"
            )

    def _artifact(
        self, owner: SQLiteTargetStore, artifact_id: str
    ) -> dict[str, Any]:
        matches = [
            item
            for item in owner.policy_artifacts
            if item.get("artifact_id") == artifact_id
        ]
        if len(matches) != 1:
            raise PolicyUnavailable(f"registry artifact is unavailable: {artifact_id}")
        artifact = matches[0]
        digest = artifact.get("content_sha256")
        prefix = (
            artifact_id.rsplit("_sha256_", 1)[0]
            if "_sha256_" in artifact_id
            else ""
        )
        if (
            prefix not in {"artifact", "approval", "validation", "ledger"}
            or artifact_id != f"{prefix}_sha256_{digest}"
            or not artifact.get("canonical_json")
        ):
            raise PolicyUnavailable("registry artifact digest does not verify")
        try:
            content = json.loads(artifact["canonical_json"])
        except (TypeError, json.JSONDecodeError) as error:
            raise PolicyUnavailable("registry artifact content is invalid") from error
        if content_digest(content) != digest:
            raise PolicyUnavailable("registry artifact content digest does not match")
        return content

    def _manifest(
        self, owner: SQLiteTargetStore, manifest_id: str
    ) -> dict[str, Any]:
        matches = [
            item
            for item in owner.policy_manifests
            if item.get("manifest_id") == manifest_id
        ]
        if len(matches) != 1:
            raise PolicyUnavailable("candidate manifest is unavailable")
        manifest = matches[0]
        manifest_digest = manifest.get("digest")
        expected = f"manifest_sha256_{manifest_digest}"
        if manifest_id != expected or not isinstance(manifest_digest, str):
            raise PolicyUnavailable("candidate manifest digest does not verify")
        material = {
            key: value
            for key, value in manifest.items()
            if key not in {"manifest_id", "digest"}
        }
        if content_digest(material) != manifest_digest:
            raise PolicyUnavailable("candidate manifest digest does not match")
        return manifest

    @staticmethod
    def _verify_validator_identity(validation: dict[str, Any]) -> None:
        validator = validation.get("validator")
        if (
            validation.get("validation_suite") != VALIDATOR_SUITE
            or validation.get("validator_build") != VALIDATOR_BUILD
            or not isinstance(validator, dict)
            or validator.get("suite") != VALIDATOR_SUITE
            or validator.get("build") != VALIDATOR_BUILD
            or validator.get("code_sha256") != _VALIDATOR_CODE_DIGEST
        ):
            raise PolicyUnavailable(
                "validation bundle was produced by a different validator identity"
            )

    # ------------------------------------------------------------- folding

    @classmethod
    def _candidate_state(
        cls, events: list[dict[str, Any]], candidate_id: str
    ) -> dict[str, Any]:
        state: dict[str, Any] = {"candidate_id": candidate_id, "status": None}
        for event in events:
            if event.get("candidate_id") != candidate_id:
                continue
            kind = event.get("kind")
            if kind == "candidate_frozen":
                state.update(
                    {
                        "status": "candidate",
                        "manifest_id": event.get("manifest_id"),
                        "manifest_digest": event.get("manifest_digest"),
                        "components": event.get("components"),
                        "author_subject": event.get("actor", {}).get("subject"),
                        "created_at": event.get("trusted_time"),
                    }
                )
                if event.get("rollback"):
                    state["rollback"] = True
                    state["rollback_target_id"] = event.get("rollback_target_id")
            elif kind in _ACTIVATION_KINDS:
                state["status"] = (
                    "superseded" if kind == "superseded" else "active"
                )
                state["activation_event_id"] = event.get("activation_event_id")
                state["active_generation"] = event.get("active_generation")
                state["activated_at"] = event.get("trusted_time")
                if kind == "superseded":
                    state["successor_candidate_id"] = event.get(
                        "successor_candidate_id"
                    )
            elif kind == "validated":
                state.update(
                    {
                        "status": "validated",
                        "validation_bundle_id": event.get("validation_bundle_id"),
                        "validation_bundle_digest": event.get(
                            "validation_bundle_digest"
                        ),
                    }
                )
            elif kind == "in_review":
                state["status"] = "in_review"
            elif kind == "approved":
                state.update(
                    {
                        "status": "approved",
                        "approval_binding_id": event.get("approval_binding_id"),
                        "approval_binding_digest": event.get(
                            "approval_binding_digest"
                        ),
                        "activation_time": event.get("activation_time"),
                        "recovery_release_id": event.get("recovery_release_id"),
                    }
                )
            elif kind == "scheduled":
                state.update(
                    {
                        "status": "scheduled",
                        "reservation_id": event.get("reservation_id"),
                        "scheduled_at": event.get("scheduled_at"),
                    }
                )
            elif kind in {"rejected", "cancelled"}:
                state["status"] = kind
                state["reason_code"] = event.get("reason_code")
                if event.get("validation_bundle_id"):
                    state["validation_bundle_id"] = event.get("validation_bundle_id")
                    state["validation_bundle_digest"] = event.get(
                        "validation_bundle_digest"
                    )
        return state

    def _fold_candidates(self, owner: SQLiteTargetStore) -> dict[str, dict[str, Any]]:
        candidates: dict[str, dict[str, Any]] = {}
        for event in owner.policy_governance_events:
            candidate_id = event.get("candidate_id")
            if not isinstance(candidate_id, str) or not candidate_id:
                continue
            if candidate_id not in candidates:
                candidates[candidate_id] = self._candidate_state(
                    owner.policy_governance_events, candidate_id
                )
        return candidates

    # ------------------------------------------------------------- commands

    def _replay_or_conflict(
        self, staged: SQLiteTargetStore, key: str, fingerprint: str
    ) -> tuple[str, Any] | None:
        previous = staged.idempotency.get(key)
        if previous is None:
            return None
        previous_fingerprint, previous_result = previous
        if previous_fingerprint == fingerprint:
            return "replayed", previous_result
        raise PolicyConflict("idempotency key conflicts with a different fingerprint")

    def _run_command(
        self,
        principal: PolicyPrincipal,
        action: str,
        idempotency_key: str,
        fingerprint: str,
        mutate: Callable[[SQLiteTargetStore, str], dict[str, Any]],
    ) -> dict[str, Any]:
        """Reload, stage, mutate and persist one governance command with
        stale-store-revision retry.  The S01 background runtime commits on
        the same store, so an unrelated commit between staging and persist
        must not fail the command."""
        if (
            not isinstance(idempotency_key, str)
            or not idempotency_key
            or idempotency_key.strip() != idempotency_key
            or len(idempotency_key) > 200
        ):
            raise PolicyInvalidTransition("idempotency key is invalid")
        if not self.audit_available or not self.storage_available:
            raise PolicyUnavailable(
                "required audit or storage authority is unavailable"
            )
        with self._lock:
            for attempt in range(3):
                self._store.reload()
                key = self._idempotency_key(principal, action, idempotency_key)
                replay = self._replay_or_conflict(self._store, key, fingerprint)
                if replay is not None:
                    return self._result(replay[1], replayed=True)
                staged = copy.deepcopy(self._store)
                result = mutate(staged, key)
                try:
                    staged.persist()
                except StaleStoreRevision:
                    if attempt < 2:
                        time.sleep(0.01)
                    continue
                except ScheduleReservationConflict as error:
                    raise PolicyConflict(
                        "an overlapping pending schedule reservation exists for this scope"
                    ) from error
                self._store = staged
                return self._result(result)
        raise PolicyConflict("store revision advanced concurrently")

    def import_legacy(
        self,
        *,
        principal: PolicyPrincipal,
        source_bundle_id: str,
        idempotency_key: str,
        expected_governance_revision: int | None,
    ) -> dict[str, Any]:
        _validate_principal(principal)
        if principal.role != "admin":
            raise PolicyInvalidTransition("only the Rule Administrator may import")
        if source_bundle_id != SOURCE_BUNDLE_ID:
            raise PolicyInvalidTransition("source bundle is not on the server allowlist")
        if (
            not isinstance(idempotency_key, str)
            or not idempotency_key
            or idempotency_key.strip() != idempotency_key
        ):
            raise PolicyInvalidTransition("idempotency key is invalid")
        rules_path = self._source_rules_path
        kb_path = self._source_kb_path
        if rules_path is None or not rules_path.is_file():
            raise PolicyInvalidTransition("server source bundle is not configured")
        if kb_path is None or not kb_path.is_file():
            raise PolicyInvalidTransition("server knowledge bundle is not configured")
        rules_bytes = rules_path.read_bytes()
        kb_bytes = kb_path.read_bytes()
        rules_digest = raw_digest(rules_bytes)
        kb_digest = raw_digest(kb_bytes)
        # The import identity binds the raw source bytes/digests, the bundle
        # identity, the importer version and the semantic payload -- never
        # filesystem path strings.
        fingerprint = self._fingerprint(
            "import_legacy",
            source_bundle_id,
            rules_digest,
            kb_digest,
            IMPORTER_VERSION,
        )
        try:
            cfg = _load_rules_bytes(rules_bytes)
            knowledge = _load_knowledge_bytes(kb_bytes)
        except (ValueError, yaml.YAMLError, json.JSONDecodeError) as error:
            raise PolicyInvalidTransition(
                "server source bundle cannot be parsed"
            ) from error
        release = TargetRelease.compile(cfg, rules_digest, knowledge=knowledge)
        mapping_ledger = self._build_mapping_ledger(rules_bytes, kb_bytes, release)

        def mutate(staged: SQLiteTargetStore, key: str) -> dict[str, Any]:
            self._verify_governance_revision(staged, expected_governance_revision)
            ledger_artifact = self._stage_json_artifact(
                staged,
                type="mapping_ledger",
                content=mapping_ledger["material"],
            )
            mapping_ledger_id = ledger_artifact["id"]
            source_artifacts = [
                self._stage_raw_artifact(
                    staged,
                    kind="check_policy",
                    content=rules_bytes,
                    digest=rules_digest,
                ),
                self._stage_raw_artifact(
                    staged,
                    kind="entity_knowledge",
                    content=kb_bytes,
                    digest=kb_digest,
                ),
            ]
            component_ids: list[dict[str, str]] = []
            for entry in release.component_artifacts():
                component_ids.append(self._stage_json_artifact(staged, **entry))
            checker_component = self._stage_json_artifact(
                staged, type="checker", content=release.to_artifact()
            )
            component_ids.append(checker_component)
            component_ids.extend(
                {
                    "type": artifact["kind"],
                    "id": artifact["artifact_id"],
                    "digest": artifact["content_sha256"],
                }
                for artifact in source_artifacts
            )
            draft_id = self._stable_id(
                "draft", f"{principal.scope}:{fingerprint}"
            )
            if draft_id in staged.policy_drafts:
                draft = staged.policy_drafts[draft_id]
            else:
                draft = {
                    "draft_id": draft_id,
                    "schema_version": "s08-draft/1",
                    "scope": principal.scope,
                    "status": "draft",
                    "bootstrap": False,
                    "created_by": principal.subject,
                    "created_at": self._trusted_time(),
                    "revision": 0,
                    "source_bundle_id": source_bundle_id,
                    "source_sha256": rules_digest,
                    "knowledge_sha256": kb_digest,
                    "mapping_ledger_id": mapping_ledger_id,
                    "mapping_ledger_digest": mapping_ledger["digest"],
                    "artifact_ids": [
                        item["artifact_id"] for item in source_artifacts
                    ],
                    "components": component_ids,
                    "metadata": {
                        "scope": principal.scope,
                        "validity": {"valid_from": "2000-01-01T00:00:00Z"},
                        "source": source_bundle_id,
                        "reason": "S08 offline import of the legacy C-DEMO baseline",
                    },
                    "candidate_id": None,
                }
                staged.policy_drafts[draft_id] = draft
            event = self._append_governance_event(
                staged,
                kind="imported",
                principal=principal,
                reason_code="S08_LEGACY_IMPORTED",
                details={
                    "draft_id": draft_id,
                    "source_bundle_id": source_bundle_id,
                    "source_sha256": rules_digest,
                    "knowledge_sha256": kb_digest,
                    "mapping_ledger_id": mapping_ledger_id,
                    "mapping_ledger_digest": mapping_ledger["digest"],
                    "importer_version": IMPORTER_VERSION,
                },
            )
            self._append_audit(
                staged,
                action="import_legacy",
                principal=principal,
                result="accepted",
                reason_code="S08_LEGACY_IMPORTED",
                details={
                    "draft_id": draft_id,
                    "governance_event_id": event["event_id"],
                },
            )
            AuditOutboxOwner(staged).append_outbox(
                {
                    "event_id": self._stable_id("outbox", f"s08:imported:{draft_id}"),
                    "kind": "s08_source_imported",
                    "scope": principal.scope,
                    "draft_id": draft_id,
                    "mapping_ledger_id": mapping_ledger_id,
                    "status": "pending",
                }
            )
            result = {
                "status": "accepted",
                "draft_id": draft_id,
                "mapping_ledger_id": mapping_ledger_id,
                "mapping_ledger_digest": mapping_ledger["digest"],
                "source_sha256": rules_digest,
                "knowledge_sha256": kb_digest,
                "governance_revision": len(staged.policy_governance_events),
            }
            staged.idempotency[key] = (fingerprint, result)
            return result

        return self._run_command(
            principal, "import_legacy", idempotency_key, fingerprint, mutate
        )

    def revise_draft(
        self,
        *,
        principal: PolicyPrincipal,
        draft_id: str,
        metadata: dict[str, Any],
        idempotency_key: str,
        expected_governance_revision: int | None,
    ) -> dict[str, Any]:
        _validate_principal(principal)
        if principal.role != "admin":
            raise PolicyInvalidTransition("only the Rule Administrator may revise")
        if (
            not isinstance(metadata, dict)
            or not metadata
            or not all(
                isinstance(key, str) and key
                for key in metadata
            )
        ):
            raise PolicyInvalidTransition("draft metadata is invalid")
        for required in ("scope", "validity", "source", "reason"):
            if required not in metadata:
                raise PolicyInvalidTransition(f"draft metadata requires {required}")
        fingerprint = self._fingerprint(
            "revise_draft", draft_id, metadata
        )

        def mutate(staged: SQLiteTargetStore, key: str) -> dict[str, Any]:
            self._verify_governance_revision(staged, expected_governance_revision)
            draft = self._require_draft(staged, draft_id)
            if draft.get("bootstrap"):
                raise PolicyInvalidTransition("bootstrap draft is not editable")
            if draft.get("candidate_id"):
                # A frozen draft is immutable; revisions fork a new draft
                # identity so the frozen candidate snapshot never changes.
                new_draft_id = self._stable_id(
                    "draft",
                    f"{draft_id}:fork:{len(staged.policy_governance_events) + 1}:"
                    f"{fingerprint}",
                )
                draft = {
                    **copy.deepcopy(draft),
                    "draft_id": new_draft_id,
                    "revision": 1,
                    "candidate_id": None,
                    "forked_from": draft_id,
                    "revised_by": principal.subject,
                    "revised_at": self._trusted_time(),
                }
                staged.policy_drafts[new_draft_id] = draft
                revised_draft_id = new_draft_id
            else:
                draft["revision"] = int(draft.get("revision", 0)) + 1
                draft["revised_by"] = principal.subject
                draft["revised_at"] = self._trusted_time()
                staged.policy_drafts[draft_id] = draft
                revised_draft_id = draft_id
            draft["metadata"] = copy.deepcopy(metadata)
            staged.policy_drafts[revised_draft_id] = draft
            revision_event = self._append_governance_event(
                staged,
                kind="draft_revised",
                principal=principal,
                reason_code="S08_DRAFT_REVISED",
                details={
                    "draft_id": revised_draft_id,
                    "draft_revision": draft["revision"],
                    "metadata_digest": content_digest(metadata),
                },
            )
            self._append_audit(
                staged,
                action="revise_draft",
                principal=principal,
                result="accepted",
                reason_code="S08_DRAFT_REVISED",
                details={
                    "draft_id": revised_draft_id,
                    "draft_revision": draft["revision"],
                    "governance_event_id": revision_event["event_id"],
                },
            )
            result = {
                "status": "accepted",
                "draft_id": revised_draft_id,
                "draft_revision": draft["revision"],
                "governance_revision": len(staged.policy_governance_events),
            }
            staged.idempotency[key] = (fingerprint, result)
            return result

        return self._run_command(
            principal, "revise_draft", idempotency_key, fingerprint, mutate
        )

    def freeze_candidate(
        self,
        *,
        principal: PolicyPrincipal,
        draft_id: str,
        idempotency_key: str,
        expected_governance_revision: int | None,
    ) -> dict[str, Any]:
        _validate_principal(principal)
        if principal.role != "admin":
            raise PolicyInvalidTransition("only the Rule Administrator may freeze")
        fingerprint = self._fingerprint("freeze_candidate", draft_id)

        def mutate(staged: SQLiteTargetStore, key: str) -> dict[str, Any]:
            self._verify_governance_revision(staged, expected_governance_revision)
            draft = self._require_draft(staged, draft_id)
            metadata = draft.get("metadata")
            if (
                not isinstance(metadata, dict)
                or metadata.get("scope") != principal.scope
                or not isinstance(metadata.get("validity"), dict)
                or not metadata["validity"].get("valid_from")
                or not isinstance(metadata.get("source"), str)
                or not metadata["source"]
                or not isinstance(metadata.get("reason"), str)
                or not metadata["reason"]
            ):
                raise PolicyInvalidTransition(
                    "draft governance metadata is incomplete"
                )
            components = copy.deepcopy(draft["components"])
            compatibility = {
                "checker_build": _TARGET_CHECKER_BUILD,
                "input_contract_schema": "s01-evidence-snapshot/1",
                "evidence_readiness_policy": _READINESS_POLICY_ID,
            }
            manifest_material = {
                "schema_version": MANIFEST_SCHEMA,
                "scope": principal.scope,
                "components": components,
                "compatibility": compatibility,
            }
            manifest_digest = content_digest(manifest_material)
            manifest_id = f"manifest_sha256_{manifest_digest}"
            manifest = {
                "manifest_id": manifest_id,
                "digest": manifest_digest,
                **manifest_material,
            }
            if not any(
                item.get("manifest_id") == manifest_id
                for item in staged.policy_manifests
            ):
                staged.policy_manifests.append(manifest)
            candidate_id = self._stable_id(
                "candidate",
                f"{draft_id}:{draft['revision']}:{manifest_digest}",
            )
            if candidate_id in self._fold_candidates(staged):
                raise PolicyConflict("candidate identity already exists")
            event = self._append_governance_event(
                staged,
                kind="candidate_frozen",
                principal=principal,
                reason_code="S08_CANDIDATE_FROZEN",
                details={
                    "candidate_id": candidate_id,
                    "draft_id": draft_id,
                    "manifest_id": manifest_id,
                    "manifest_digest": manifest_digest,
                    "components": components,
                    "metadata": copy.deepcopy(metadata),
                    "mapping_ledger_id": draft.get("mapping_ledger_id"),
                    "mapping_ledger_digest": draft.get("mapping_ledger_digest"),
                    "source_sha256": draft.get("source_sha256"),
                    "knowledge_sha256": draft.get("knowledge_sha256"),
                },
            )
            draft["candidate_id"] = candidate_id
            staged.policy_drafts[draft_id] = draft
            self._append_audit(
                staged,
                action="freeze_candidate",
                principal=principal,
                result="accepted",
                reason_code="S08_CANDIDATE_FROZEN",
                details={
                    "candidate_id": candidate_id,
                    "manifest_id": manifest_id,
                    "governance_event_id": event["event_id"],
                },
            )
            result = {
                "status": "accepted",
                "candidate_id": candidate_id,
                "manifest_id": manifest_id,
                "manifest_digest": manifest_digest,
                "components": components,
                "governance_revision": len(staged.policy_governance_events),
            }
            staged.idempotency[key] = (fingerprint, result)
            return result

        return self._run_command(
            principal, "freeze_candidate", idempotency_key, fingerprint, mutate
        )

    def request_validation(
        self,
        *,
        principal: PolicyPrincipal,
        candidate_id: str,
        idempotency_key: str,
        expected_governance_revision: int | None,
    ) -> dict[str, Any]:
        _validate_principal(principal)
        if principal.role != "admin":
            raise PolicyInvalidTransition("only the Rule Administrator may request validation")
        fingerprint = self._fingerprint("request_validation", candidate_id)

        def mutate(staged: SQLiteTargetStore, key: str) -> dict[str, Any]:
            self._verify_governance_revision(staged, expected_governance_revision)
            state = self._require_candidate_state(staged, candidate_id)
            if state["status"] != "candidate":
                raise PolicyInvalidTransition(
                    f"candidate {candidate_id} cannot request validation from {state['status']}"
                )
            existing = [
                job
                for job in staged.policy_jobs
                if job.get("kind") == "validation"
                and job.get("candidate_id") == candidate_id
                and job.get("status") not in {"complete", "diagnostic"}
            ]
            if existing:
                raise PolicyConflict("validation job already exists for this candidate")
            policy_job_id = self._stable_id("policy_job", key)
            staged.policy_jobs.append(
                {
                    "policy_job_id": policy_job_id,
                    "schema_version": "s08-policy-job/1",
                    "kind": "validation",
                    "scope": principal.scope,
                    "candidate_id": candidate_id,
                    "status": "queued",
                    "fence": 0,
                    "attempt_no": 0,
                    "created_by": principal.subject,
                    "created_at": self._trusted_time(),
                    "expected_governance_revision": len(
                        staged.policy_governance_events
                    ),
                }
            )
            AuditOutboxOwner(staged).append_outbox(
                {
                    "event_id": self._stable_id("outbox", policy_job_id),
                    "kind": "s08_validation_requested",
                    "scope": principal.scope,
                    "candidate_id": candidate_id,
                    "policy_job_id": policy_job_id,
                    "status": "pending",
                }
            )
            self._append_audit(
                staged,
                action="request_validation",
                principal=principal,
                result="accepted",
                reason_code="S08_VALIDATION_REQUESTED",
                details={
                    "candidate_id": candidate_id,
                    "policy_job_id": policy_job_id,
                },
            )
            result = {
                "status": "accepted",
                "policy_job_id": policy_job_id,
                "candidate_id": candidate_id,
                "governance_revision": len(staged.policy_governance_events),
            }
            staged.idempotency[key] = (fingerprint, result)
            return result

        return self._run_command(
            principal, "request_validation", idempotency_key, fingerprint, mutate
        )

    def submit_review(
        self,
        *,
        principal: PolicyPrincipal,
        candidate_id: str,
        idempotency_key: str,
        expected_governance_revision: int | None,
    ) -> dict[str, Any]:
        _validate_principal(principal)
        if principal.role != "admin":
            raise PolicyInvalidTransition("only the Rule Administrator may submit review")
        fingerprint = self._fingerprint("submit_review", candidate_id)

        def mutate(staged: SQLiteTargetStore, key: str) -> dict[str, Any]:
            self._verify_governance_revision(staged, expected_governance_revision)
            state = self._require_candidate_state(staged, candidate_id)
            if state["status"] != "validated":
                raise PolicyInvalidTransition(
                    f"candidate {candidate_id} cannot enter review from {state['status']}"
                )
            event = self._append_governance_event(
                staged,
                kind="in_review",
                principal=principal,
                reason_code="S08_CANDIDATE_IN_REVIEW",
                details={
                    "candidate_id": candidate_id,
                    "validation_bundle_id": state["validation_bundle_id"],
                    "validation_bundle_digest": state["validation_bundle_digest"],
                },
            )
            self._append_audit(
                staged,
                action="submit_review",
                principal=principal,
                result="accepted",
                reason_code="S08_CANDIDATE_IN_REVIEW",
                details={
                    "candidate_id": candidate_id,
                    "governance_event_id": event["event_id"],
                },
            )
            result = {
                "status": "accepted",
                "candidate_id": candidate_id,
                "validation_bundle_id": state["validation_bundle_id"],
                "governance_revision": len(staged.policy_governance_events),
            }
            staged.idempotency[key] = (fingerprint, result)
            return result

        return self._run_command(
            principal, "submit_review", idempotency_key, fingerprint, mutate
        )

    def _review_material(
        self, owner: SQLiteTargetStore, state: dict[str, Any], scope: str
    ) -> dict[str, Any]:
        """Deterministic review material shared by the candidate workspace
        and the approval binding: anchor/candidate component changes,
        applicable-check delta, behavior result, validation bundle, full
        mapping ledger and the unsupported report.  Any drift in this
        material changes the binding digest and invalidates the approval."""
        manifest = self._verify_pinned_manifest(
            owner, state["manifest_id"], state["manifest_digest"]
        )
        active = self._fold_active_projection(
            owner.policy_governance_events, scope
        )
        anchor_manifest = None
        if active is not None:
            anchor_manifest = self._verify_pinned_manifest(
                owner, active["manifest_id"], active["manifest_digest"]
            )
        anchor_components = (
            {item["type"]: item for item in anchor_manifest["components"]}
            if anchor_manifest is not None
            else {}
        )
        candidate_components = {
            item["type"]: item for item in manifest["components"]
        }
        changes: list[dict[str, Any]] = []
        for component_type in sorted(
            set(anchor_components) | set(candidate_components)
        ):
            anchor_item = anchor_components.get(component_type)
            candidate_item = candidate_components.get(component_type)
            if anchor_item is None:
                changes.append(
                    {
                        "component": component_type,
                        "change": "added",
                        "candidate_id": candidate_item["id"],
                        "candidate_digest": candidate_item["digest"],
                    }
                )
            elif candidate_item is None:
                changes.append(
                    {
                        "component": component_type,
                        "change": "removed",
                        "anchor_id": anchor_item["id"],
                        "anchor_digest": anchor_item["digest"],
                    }
                )
            elif anchor_item["digest"] != candidate_item["digest"]:
                changes.append(
                    {
                        "component": component_type,
                        "change": "modified",
                        "anchor_id": anchor_item["id"],
                        "anchor_digest": anchor_item["digest"],
                        "candidate_id": candidate_item["id"],
                        "candidate_digest": candidate_item["digest"],
                    }
                )
        candidate_release = TargetRelease.from_artifact(
            self._artifact(owner, self._component_id(manifest, "checker"))
        )
        anchor_release = None
        if anchor_manifest is not None:
            anchor_release = TargetRelease.from_artifact(
                self._artifact(
                    owner, self._component_id(anchor_manifest, "checker")
                )
            )
        anchor_checks = (
            tuple(anchor_release.public_manifest()["applicable_check_ids"])
            if anchor_release is not None
            else ()
        )
        candidate_checks = tuple(
            candidate_release.public_manifest()["applicable_check_ids"]
        )
        validation = (
            self._artifact(owner, state["validation_bundle_id"])
            if state.get("validation_bundle_id")
            else None
        )
        if validation is not None:
            self._verify_validator_identity(validation)
        corpus_diff = (
            validation.get("results", {}).get("corpus_diff")
            if validation is not None
            else None
        )
        ledger = None
        frozen_events = [
            event
            for event in owner.policy_governance_events
            if event.get("kind") == "candidate_frozen"
            and event.get("candidate_id") == state.get("candidate_id")
        ]
        frozen = frozen_events[-1] if len(frozen_events) == 1 else None
        ledger_id = frozen.get("mapping_ledger_id") if frozen else None
        ledger_digest = frozen.get("mapping_ledger_digest") if frozen else None
        if ledger_id and ledger_digest:
            ledger = self._find_mapping_ledger(owner, ledger_id, ledger_digest)
        unsupported = [
            item
            for item in (ledger or {}).get("items", [])
            if item.get("classification") == "unsupported"
        ]
        return {
            "schema_version": "s08-review-material/1",
            "candidate_id": state["candidate_id"],
            "candidate_digest": manifest["digest"],
            "anchor_candidate_id": (
                active["candidate_id"] if active is not None else None
            ),
            "anchor_components": {
                key: {"id": item["id"], "digest": item["digest"]}
                for key, item in anchor_components.items()
            },
            "candidate_components": {
                key: {"id": item["id"], "digest": item["digest"]}
                for key, item in candidate_components.items()
            },
            "changes": changes,
            "applicable_check_delta": {
                "anchor": anchor_checks,
                "candidate": candidate_checks,
                "added": sorted(set(candidate_checks) - set(anchor_checks)),
                "removed": sorted(set(anchor_checks) - set(candidate_checks)),
            },
            "behavior_delta": {
                "equal": bool(corpus_diff and corpus_diff.get("equal")),
                "reason": (
                    corpus_diff.get("reason")
                    if corpus_diff is not None
                    else "validation evidence is unavailable"
                ),
            },
            "validation_bundle_id": state.get("validation_bundle_id"),
            "validation_bundle_digest": state.get("validation_bundle_digest"),
            "mapping_ledger_id": ledger_id,
            "mapping_ledger": ledger,
            "unsupported_report": {
                "count": len(unsupported),
                "items": unsupported,
            },
        }

    def _require_current_approval_review(
        self,
        owner: SQLiteTargetStore,
        state: dict[str, Any],
        scope: str,
        binding: dict[str, Any],
    ) -> None:
        bound = binding.get("diff")
        if not isinstance(bound, dict) or canonical_bytes(
            bound
        ) != canonical_bytes(self._review_material(owner, state, scope)):
            raise PolicyConflict(
                "approval review no longer matches the active release anchor"
            )

    def approve(
        self,
        *,
        principal: PolicyPrincipal,
        candidate_id: str,
        activation_time: int,
        recovery_release_id: str,
        idempotency_key: str,
        expected_governance_revision: int | None,
        preview_manifest_id: str,
    ) -> dict[str, Any]:
        _validate_principal(principal)
        if principal.role != "approver":
            raise PolicyInvalidTransition("only the Policy Approver may approve")
        if (
            isinstance(activation_time, bool)
            or not isinstance(activation_time, int)
            or activation_time < self._trusted_time()
        ):
            raise PolicyInvalidTransition("activation time is non-retroactive")
        if (
            not isinstance(recovery_release_id, str)
            or not recovery_release_id
            or recovery_release_id.strip() != recovery_release_id
        ):
            raise PolicyInvalidTransition("recovery release identity is invalid")
        if (
            not isinstance(preview_manifest_id, str)
            or not preview_manifest_id
            or preview_manifest_id.strip() != preview_manifest_id
        ):
            raise PolicyInvalidTransition("impact preview identity is invalid")
        # S-1/S-5: every newly approved candidate binds the immutable impact
        # preview, so the approval fingerprint covers the preview identity
        # and the same idempotency key with a different preview must conflict
        # instead of replaying the first approval binding.
        fingerprint = self._fingerprint(
            "approve",
            candidate_id,
            activation_time,
            recovery_release_id,
            preview_manifest_id,
        )

        def mutate(staged: SQLiteTargetStore, key: str) -> dict[str, Any]:
            self._verify_governance_revision(staged, expected_governance_revision)
            state = self._require_candidate_state(staged, candidate_id)
            if state["status"] != "in_review":
                raise PolicyInvalidTransition(
                    f"candidate {candidate_id} cannot be approved from {state['status']}"
                )
            if state.get("author_subject") == principal.subject:
                raise PolicyInvalidTransition("the author cannot approve the candidate")
            recovery_states = self._fold_candidates(staged)
            recovery = recovery_states.get(recovery_release_id)
            if recovery is None or recovery.get("status") not in {
                "active",
                "superseded",
            }:
                raise PolicyInvalidTransition(
                    "recovery release is not a known governed release"
                )
            self._require_candidate_scope_valid_at(
                staged, candidate_id, activation_time
            )
            # Re-verify the manifest and validation bundle remain
            # registry-bound, then bind exactly the recomputed review
            # material: component changes, applicable-check delta, behavior
            # result, mapping ledger and unsupported report.
            review = self._review_material(staged, state, principal.scope)
            binding_material = {
                "schema_version": APPROVAL_BINDING_SCHEMA,
                "candidate_id": candidate_id,
                "candidate_digest": state["manifest_digest"],
                "validation_bundle_id": state["validation_bundle_id"],
                "validation_bundle_digest": state["validation_bundle_digest"],
                "diff": review,
                "scope": principal.scope,
                "activation_time": activation_time,
                "recovery_release_id": recovery_release_id,
                "approved_by": principal.subject,
            }
            # S09: the approval always binds the immutable impact preview and
            # the machine-decidable envelope derived from it.  Any change to
            # predecessor/candidate/scope/oracle/dependency category/risk,
            # any full-scope expansion, any member/count ceiling breach
            # requires a new preview and a new approval.
            preview = self._preview_manifest(staged, preview_manifest_id)
            if str(preview.get("candidate", {}).get("candidate_id")) != candidate_id:
                raise PolicyInvalidTransition(
                    "impact preview does not belong to the candidate"
                )
            envelope = self._impact_envelope(
                staged, preview=preview, candidate=state
            )
            binding_material.update(
                {
                    "preview_manifest_id": preview_manifest_id,
                    "preview_manifest_digest": str(preview["digest"]),
                    "impact_envelope": envelope,
                    "impact_envelope_digest": str(envelope["digest"]),
                }
            )
            binding_digest = content_digest(binding_material)
            approval_binding_id = f"approval_sha256_{binding_digest}"
            if not any(
                item.get("artifact_id") == approval_binding_id
                for item in staged.policy_artifacts
            ):
                staged.policy_artifacts.append(
                    {
                        "artifact_id": approval_binding_id,
                        "schema_version": ARTIFACT_SCHEMA,
                        "kind": "approval_binding",
                        "content_sha256": binding_digest,
                        "content_bytes": len(canonical_bytes(binding_material)),
                        "canonical_json": canonical_bytes(binding_material).decode(
                            "utf-8"
                        ),
                        "raw_hex": None,
                        "importer_version": None,
                    }
                )
            event = self._append_governance_event(
                staged,
                kind="approved",
                principal=principal,
                reason_code="S08_CANDIDATE_APPROVED",
                details={
                    "candidate_id": candidate_id,
                    "approval_binding_id": approval_binding_id,
                    "approval_binding_digest": binding_digest,
                    "validation_bundle_id": state["validation_bundle_id"],
                    "validation_bundle_digest": state["validation_bundle_digest"],
                    "activation_time": activation_time,
                    "recovery_release_id": recovery_release_id,
                },
            )
            self._append_audit(
                staged,
                action="approve",
                principal=principal,
                result="accepted",
                reason_code="S08_CANDIDATE_APPROVED",
                details={
                    "candidate_id": candidate_id,
                    "approval_binding_id": approval_binding_id,
                    "governance_event_id": event["event_id"],
                },
            )
            result = {
                "status": "accepted",
                "candidate_id": candidate_id,
                "approval_binding_id": approval_binding_id,
                "approval_binding_digest": binding_digest,
                "validation_bundle_id": state["validation_bundle_id"],
                "validation_bundle_digest": state["validation_bundle_digest"],
                "author_subject": state.get("author_subject"),
                "approver_subject": principal.subject,
                "activation_time": activation_time,
                "recovery_release_id": recovery_release_id,
                "preview_manifest_id": binding_material.get("preview_manifest_id"),
                "preview_manifest_digest": binding_material.get(
                    "preview_manifest_digest"
                ),
                "impact_envelope": binding_material.get("impact_envelope"),
                "impact_envelope_digest": binding_material.get(
                    "impact_envelope_digest"
                ),
                "governance_revision": len(staged.policy_governance_events),
            }
            staged.idempotency[key] = (fingerprint, result)
            return result

        return self._run_command(
            principal, "approve", idempotency_key, fingerprint, mutate
        )

    def reject(
        self,
        *,
        principal: PolicyPrincipal,
        candidate_id: str,
        reason_code: str,
        idempotency_key: str,
        expected_governance_revision: int | None,
    ) -> dict[str, Any]:
        _validate_principal(principal)
        if principal.role != "approver":
            raise PolicyInvalidTransition("only the Policy Approver may reject")
        if (
            not isinstance(reason_code, str)
            or not reason_code
            or reason_code.strip() != reason_code
        ):
            raise PolicyInvalidTransition("rejection reason is invalid")
        fingerprint = self._fingerprint("reject", candidate_id, reason_code)

        def mutate(staged: SQLiteTargetStore, key: str) -> dict[str, Any]:
            self._verify_governance_revision(staged, expected_governance_revision)
            state = self._require_candidate_state(staged, candidate_id)
            if state["status"] not in {"validated", "in_review"}:
                raise PolicyInvalidTransition(
                    f"candidate {candidate_id} cannot be rejected from {state['status']}"
                )
            event = self._append_governance_event(
                staged,
                kind="rejected",
                principal=principal,
                reason_code=reason_code,
                details={"candidate_id": candidate_id},
            )
            self._append_audit(
                staged,
                action="reject",
                principal=principal,
                result="accepted",
                reason_code="S08_CANDIDATE_REJECTED",
                details={
                    "candidate_id": candidate_id,
                    "governance_event_id": event["event_id"],
                },
            )
            result = {
                "status": "accepted",
                "candidate_id": candidate_id,
                "reason_code": reason_code,
                "governance_revision": len(staged.policy_governance_events),
            }
            staged.idempotency[key] = (fingerprint, result)
            return result

        return self._run_command(
            principal, "reject", idempotency_key, fingerprint, mutate
        )

    def schedule(
        self,
        *,
        principal: PolicyPrincipal,
        approval_binding_id: str,
        activation_at: int,
        idempotency_key: str,
        expected_governance_revision: int | None,
    ) -> dict[str, Any]:
        _validate_principal(principal)
        if principal.role != "admin":
            raise PolicyInvalidTransition("only the Rule Administrator may schedule")
        if (
            isinstance(activation_at, bool)
            or not isinstance(activation_at, int)
            or activation_at < 1
        ):
            raise PolicyInvalidTransition("activation time is invalid")
        if (
            not isinstance(approval_binding_id, str)
            or not approval_binding_id
            or approval_binding_id.strip() != approval_binding_id
        ):
            raise PolicyInvalidTransition("approval binding identity is invalid")
        fingerprint = self._fingerprint(
            "schedule", approval_binding_id, activation_at
        )

        def mutate(staged: SQLiteTargetStore, key: str) -> dict[str, Any]:
            self._verify_governance_revision(staged, expected_governance_revision)
            binding = self._artifact(staged, approval_binding_id)
            if binding.get("schema_version") != APPROVAL_BINDING_SCHEMA:
                raise PolicyInvalidTransition("approval binding is not verifiable")
            candidate_id = str(binding["candidate_id"])
            state = self._require_candidate_state(staged, candidate_id)
            if (
                self._activation_hold(staged, principal.scope) is not None
                and not state.get("rollback")
            ):
                # The hold blocks ordinary activations; the dedicated
                # governed rollback command is explicitly permitted.
                raise PolicyInvalidTransition("activation hold is in effect")
            if activation_at < self._trusted_time():
                raise PolicyInvalidTransition("activation time is retroactive")
            if state["status"] != "approved":
                raise PolicyInvalidTransition(
                    f"candidate {candidate_id} cannot be scheduled from {state['status']}"
                )
            if binding.get("activation_time") != activation_at:
                raise PolicyInvalidTransition(
                    "scheduled time differs from the bound trusted activation time"
                )
            self._require_candidate_scope_valid_at(
                staged, candidate_id, activation_at
            )
            self._require_current_approval_review(
                staged, state, principal.scope, binding
            )
            reservation_id = self._stable_id(
                "reservation", f"{approval_binding_id}:{activation_at}"
            )
            staged.policy_schedule_reservations[reservation_id] = {
                "reservation_id": reservation_id,
                "schema_version": RESERVATION_SCHEMA,
                "scope": principal.scope,
                "approval_binding_id": approval_binding_id,
                "candidate_id": candidate_id,
                "activation_at": activation_at,
                "status": "pending",
                "created_by": principal.subject,
                "created_at": self._trusted_time(),
            }
            event = self._append_governance_event(
                staged,
                kind="scheduled",
                principal=principal,
                reason_code="S08_CANDIDATE_SCHEDULED",
                details={
                    "candidate_id": candidate_id,
                    "approval_binding_id": approval_binding_id,
                    "reservation_id": reservation_id,
                    "activation_at": activation_at,
                    "scheduled_at": self._trusted_time(),
                },
            )
            policy_job_id = self._stable_id(
                "policy_job", f"{candidate_id}:activation:{activation_at}"
            )
            staged.policy_jobs.append(
                {
                    "policy_job_id": policy_job_id,
                    "schema_version": "s08-policy-job/1",
                    "kind": "activation",
                    "scope": principal.scope,
                    "candidate_id": candidate_id,
                    "approval_binding_id": approval_binding_id,
                    "reservation_id": reservation_id,
                    "activation_at": activation_at,
                    "status": "queued",
                    "fence": 0,
                    "attempt_no": 0,
                    "created_by": principal.subject,
                    "created_at": self._trusted_time(),
                    "expected_governance_revision": len(
                        staged.policy_governance_events
                    ),
                }
            )
            AuditOutboxOwner(staged).append_outbox(
                {
                    "event_id": self._stable_id("outbox", policy_job_id),
                    "kind": "s08_activation_scheduled",
                    "scope": principal.scope,
                    "candidate_id": candidate_id,
                    "policy_job_id": policy_job_id,
                    "activation_at": activation_at,
                    "status": "pending",
                }
            )
            self._append_audit(
                staged,
                action="schedule",
                principal=principal,
                result="accepted",
                reason_code="S08_CANDIDATE_SCHEDULED",
                details={
                    "candidate_id": candidate_id,
                    "reservation_id": reservation_id,
                    "policy_job_id": policy_job_id,
                    "governance_event_id": event["event_id"],
                },
            )
            result = {
                "status": "accepted",
                "candidate_id": candidate_id,
                "reservation_id": reservation_id,
                "policy_job_id": policy_job_id,
                "activation_at": activation_at,
                "governance_revision": len(staged.policy_governance_events),
            }
            staged.idempotency[key] = (fingerprint, result)
            return result

        return self._run_command(
            principal, "schedule", idempotency_key, fingerprint, mutate
        )

    def stop_activations(
        self,
        *,
        principal: PolicyPrincipal,
        reason_code: str,
        idempotency_key: str,
        expected_governance_revision: int | None,
    ) -> dict[str, Any]:
        """Apply an activation hold for the served scope: no schedule may
        be created and no pending activation may advance.  The current
        active release keeps resolving; this is not an executable
        rollback (that waits for S09)."""
        _validate_principal(principal)
        if principal.role != "operator":
            raise PolicyInvalidTransition(
                "only the activation operator may stop activations"
            )
        if (
            not isinstance(reason_code, str)
            or not reason_code
            or reason_code.strip() != reason_code
        ):
            raise PolicyInvalidTransition("stop reason is invalid")
        fingerprint = self._fingerprint("stop_activations", reason_code)

        def mutate(staged: SQLiteTargetStore, key: str) -> dict[str, Any]:
            self._verify_governance_revision(staged, expected_governance_revision)
            event = self._append_governance_event(
                staged,
                kind="activation_stopped",
                principal=principal,
                reason_code=reason_code,
                details={"scope": principal.scope},
            )
            projection = staged.policy_active_projections.get(principal.scope)
            if projection is not None:
                projection["activation_hold"] = {
                    "event_id": event["event_id"],
                    "reason_code": reason_code,
                    "stopped_at": self._trusted_time(),
                    "stopped_by": principal.subject,
                }
                staged.policy_active_projections[principal.scope] = projection
            self._append_audit(
                staged,
                action="stop_activations",
                principal=principal,
                result="accepted",
                reason_code="S08_ACTIVATION_STOPPED",
                details={
                    "governance_event_id": event["event_id"],
                    "hold_reason": reason_code,
                },
            )
            result = {
                "status": "accepted",
                "scope": principal.scope,
                "reason_code": reason_code,
                "governance_event_id": event["event_id"],
                "governance_revision": len(staged.policy_governance_events),
            }
            staged.idempotency[key] = (fingerprint, result)
            return result

        return self._run_command(
            principal, "stop_activations", idempotency_key, fingerprint, mutate
        )

    # ------------------------------------------------------------ S09 impact

    def _lifecycle_impact_snapshot(
        self,
        owner: SQLiteTargetStore,
        final_impact_digest: str | None = None,
    ) -> dict[str, Any]:
        """The read-only, side-effect-free Lifecycle-owned impact snapshot.

        Governance never reads application state directly: the Lifecycle
        builds the snapshot from the same physical store snapshot Governance
        already reloaded, so one consistent view is used inside the
        activation transaction without any cross-owner write."""
        if self._lifecycle_snapshot_provider is None:
            raise PolicyUnavailable(
                "Lifecycle impact snapshot provider is not wired"
            )
        try:
            snapshot = self._lifecycle_snapshot_provider(owner, final_impact_digest)
        except Exception as error:
            raise PolicyUnavailable(
                "Lifecycle impact snapshot is unavailable"
            ) from error
        if not isinstance(snapshot, dict) or snapshot.get("complete") is not True:
            raise PolicyUnavailable("Lifecycle impact snapshot is incomplete")
        return snapshot

    def _impact_manifest_request(
        self,
        owner: SQLiteTargetStore,
        *,
        phase: str,
        candidate: dict[str, Any],
        generation: int,
        envelope: dict[str, Any] | None,
        final_impact_digest: str | None = None,
    ) -> dict[str, Any]:
        active = self._fold_active_projection(
            owner.policy_governance_events, S08_SCOPE
        )
        if active is None:
            raise PolicyInvalidTransition("no active predecessor release exists")
        predecessor_manifest = self._verify_pinned_manifest(
            owner,
            active["manifest_id"],
            active["manifest_digest"],
        )
        predecessor_components = {
            str(item["type"]): str(item["digest"])
            for item in predecessor_manifest["components"]
        }
        candidate_manifest = self._verify_pinned_manifest(
            owner,
            candidate["manifest_id"],
            candidate["manifest_digest"],
        )
        candidate_components = {
            str(item["type"]): str(item["digest"])
            for item in candidate_manifest["components"]
        }
        snapshot = self._lifecycle_impact_snapshot(owner, final_impact_digest)
        return {
            "phase": phase,
            "scope": S08_SCOPE,
            "predecessor": {
                "candidate_id": active["candidate_id"],
                "manifest_id": active["manifest_id"],
                "manifest_digest": active["manifest_digest"],
                "activation_event_id": active["activation_event_id"],
                "active_generation": active["active_generation"],
                "components": predecessor_components,
            },
            "candidate": {
                "candidate_id": candidate["candidate_id"],
                "manifest_id": candidate["manifest_id"],
                "manifest_digest": candidate["manifest_digest"],
                "components": candidate_components,
            },
            "target_generation": generation,
            "authority_watermarks": {
                "governance_revision": len(owner.policy_governance_events),
                "lifecycle_watermark": snapshot.get("lifecycle_watermark"),
            },
            "dependency_index": {
                "complete": True,
                "index_digest": str(
                    snapshot.get("dependency_index_digest") or ""
                ),
                "oracle_version": snapshot.get("dependency_index_version"),
            },
            "snapshot": snapshot,
            "approval_envelope": envelope or {},
            "max_added_members": int(
                (envelope or {}).get("member_delta_rules", {}).get("max_added", 0)
            ),
        }

    def preview_impact(
        self,
        *,
        principal: PolicyPrincipal,
        candidate_id: str,
        idempotency_key: str,
        expected_governance_revision: int | None,
    ) -> dict[str, Any]:
        """The immutable, content-addressed conservative impact preview for
        a changed governed release.  The preview fixes predecessor/candidate
        digests, scope, oracle version, dependency categories, partition
        counts/digests and the deterministic member set; approval later binds
        this exact digest."""
        _validate_principal(principal)
        if principal.role not in {"admin", "approver"}:
            raise PolicyInvalidTransition(
                "only the Rule Administrator or Policy Approver may preview impact"
            )
        if (
            not isinstance(candidate_id, str)
            or not candidate_id
            or candidate_id.strip() != candidate_id
        ):
            raise PolicyInvalidTransition("candidate identity is invalid")
        fingerprint = self._fingerprint("preview_impact", candidate_id)

        def mutate(staged: SQLiteTargetStore, key: str) -> dict[str, Any]:
            self._verify_governance_revision(staged, expected_governance_revision)
            state = self._require_candidate_state(staged, candidate_id)
            if state["status"] not in {"validated", "in_review", "approved"}:
                raise PolicyInvalidTransition(
                    f"candidate {candidate_id} cannot be previewed from {state['status']}"
                )
            active = self._fold_active_projection(
                staged.policy_governance_events, S08_SCOPE
            )
            if active is None:
                raise PolicyInvalidTransition("no active predecessor release exists")
            generation = int(active["active_generation"]) + 1
            try:
                manifest = build_impact_manifest(
                    self._impact_manifest_request(
                        staged,
                        phase="preview",
                        candidate=state,
                        generation=generation,
                        envelope=None,
                    )
                )
            except ImpactUnprovable as error:
                raise PolicyInvalidTransition(
                    f"IMPACT_UNPROVABLE_{error.reason_code}"
                ) from error
            event = self._append_governance_event(
                staged,
                kind="impact_previewed",
                principal=principal,
                reason_code="S09_IMPACT_PREVIEWED",
                details={
                    "candidate_id": candidate_id,
                    "manifest_id": manifest["manifest_id"],
                    "digest": manifest["digest"],
                    "phase": manifest["phase"],
                    "member_count": len(manifest["members"]),
                    "partition_counts": {
                        name: info["count"]
                        for name, info in manifest["partitions"].items()
                    },
                    "zero_hit_proof": manifest["zero_hit_proof"] is not None,
                    "target_generation": generation,
                    "predecessor": manifest["predecessor"],
                    "manifest": manifest,
                },
            )
            self._append_audit(
                staged,
                action="preview_impact",
                principal=principal,
                result="accepted",
                reason_code="S09_IMPACT_PREVIEWED",
                details={
                    "candidate_id": candidate_id,
                    "manifest_id": manifest["manifest_id"],
                    "manifest_digest": manifest["digest"],
                    "governance_event_id": event["event_id"],
                },
            )
            result = {
                "status": "accepted",
                "phase": manifest["phase"],
                "manifest_id": manifest["manifest_id"],
                "digest": manifest["digest"],
                "scope": manifest["scope"],
                "oracle_version": manifest["oracle_version"],
                "level": manifest["level"],
                "expanded_to_full_scope": manifest["expanded_to_full_scope"],
                "member_count": len(manifest["members"]),
                "partition_counts": {
                    name: info["count"]
                    for name, info in manifest["partitions"].items()
                },
                "zero_hit_proof": manifest["zero_hit_proof"] is not None,
                "target_generation": generation,
                "governance_revision": len(staged.policy_governance_events),
            }
            staged.idempotency[key] = (fingerprint, result)
            return result

        return self._run_command(
            principal,
            "preview_impact",
            idempotency_key,
            fingerprint,
            mutate,
        )

    def _preview_manifest(
        self, owner: SQLiteTargetStore, preview_manifest_id: str
    ) -> dict[str, Any]:
        """Read one immutable impact preview from the Ledger and re-verify
        its canonical digest before approval binds it."""
        if (
            not isinstance(preview_manifest_id, str)
            or not preview_manifest_id
            or preview_manifest_id.strip() != preview_manifest_id
        ):
            raise PolicyInvalidTransition("preview manifest identity is invalid")
        events = [
            event
            for event in owner.policy_governance_events
            if event.get("kind") == "impact_previewed"
            and event.get("manifest_id") == preview_manifest_id
        ]
        if not events:
            raise PolicyNotFound("impact preview is unavailable")
        event = events[-1]
        manifest = event.get("manifest")
        if not isinstance(manifest, dict):
            raise PolicyUnavailable("impact preview content is unavailable")
        if manifest.get("schema_version") != IMPACT_MANIFEST_SCHEMA:
            raise PolicyUnavailable("impact preview schema is not verifiable")
        if not verify_impact_manifest_digest(manifest):
            raise PolicyUnavailable("impact preview digest does not verify")
        return manifest

    def _impact_envelope(
        self,
        owner: SQLiteTargetStore,
        *,
        preview: dict[str, Any],
        candidate: dict[str, Any],
    ) -> dict[str, Any]:
        active = self._fold_active_projection(
            owner.policy_governance_events, S08_SCOPE
        )
        if active is None:
            raise PolicyInvalidTransition("no active predecessor release exists")
        return build_impact_envelope(
            preview=preview,
            predecessor={
                "candidate_id": active["candidate_id"],
                "manifest_digest": active["manifest_digest"],
            },
            candidate={
                "candidate_id": candidate["candidate_id"],
                "manifest_digest": candidate["manifest_digest"],
            },
            scope=S08_SCOPE,
            risk_class="governed_change",
            dependency_categories=("release_change", "evidence_dependency"),
            required_approvals=("policy_approver",),
            protected_conditions=(
                "no_manual_exclusion",
                "no_old_success_reuse",
                "no_hold_auto_expiry",
            ),
            max_added_members=0,
            max_total_members=max(
                1, len(preview.get("members", [])) or 1
            ),
        )

    def _verify_final_impact_within_envelope(
        self,
        *,
        envelope: dict[str, Any],
        preview: dict[str, Any],
        final: dict[str, Any],
    ) -> None:
        """The activation-time final impact must stay inside the approved
        machine-decidable envelope: same predecessor/candidate digests,
        scope, oracle version, authority watermarks and dependency index, no
        member removal, additions only within the count ceilings.  Any drift
        stops activation with zero protected delta; a new preview and a new
        approval are the next actions."""
        if envelope.get("schema_version") != IMPACT_ENVELOPE_SCHEMA:
            raise PolicyInvalidTransition(
                "approval envelope schema is not verifiable"
            )
        if envelope.get("preview_digest") != preview.get("digest"):
            raise PolicyInvalidTransition(
                "approval envelope does not bind the preview digest"
            )
        # Approved authority facts: scope, oracle version, the preview-time
        # authority watermarks (lifecycle watermark exact; governance
        # revision may only move forward through the approval chain) and the
        # dependency index digest must all match the final manifest.
        if str(envelope.get("scope") or "") != str(final.get("scope") or ""):
            raise PolicyInvalidTransition(
                "final impact scope drifted outside the envelope"
            )
        # The final manifest must bind the exact approved envelope digest:
        # every approved risk/dependency/approval/protection fact lives in
        # that canonical envelope, so a final manifest carrying any other
        # envelope identity cannot claim the approval.
        embedded_envelope = final.get("approval_envelope") or {}
        if str(embedded_envelope.get("digest") or "") != str(
            envelope.get("digest") or ""
        ):
            raise PolicyInvalidTransition(
                "final impact does not bind the approved envelope digest"
            )
        if str(envelope.get("oracle_version") or "") != str(
            final.get("oracle_version") or ""
        ):
            raise PolicyInvalidTransition(
                "final impact oracle version drifted outside the envelope"
            )
        envelope_watermarks = envelope.get("authority_watermarks") or {}
        final_watermarks = final.get("authority_watermarks") or {}
        envelope_lifecycle_watermark = envelope_watermarks.get("lifecycle_watermark")
        final_lifecycle_watermark = final_watermarks.get("lifecycle_watermark")
        if (
            isinstance(envelope_lifecycle_watermark, bool)
            or not isinstance(envelope_lifecycle_watermark, int)
            or isinstance(final_lifecycle_watermark, bool)
            or not isinstance(final_lifecycle_watermark, int)
            or envelope_lifecycle_watermark != final_lifecycle_watermark
        ):
            raise PolicyInvalidTransition(
                "final impact lifecycle watermark drifted outside the envelope"
            )
        movement = envelope.get("permitted_authority_movement") or {}
        governance_range = movement.get("governance_revision") or {}
        lifecycle_range = movement.get("lifecycle_watermark") or {}
        final_governance_revision = final_watermarks.get("governance_revision")
        governance_minimum = governance_range.get("minimum")
        governance_maximum = governance_range.get("maximum")
        if not (
            isinstance(final_governance_revision, int)
            and not isinstance(final_governance_revision, bool)
            and isinstance(governance_minimum, int)
            and not isinstance(governance_minimum, bool)
            and isinstance(governance_maximum, int)
            and not isinstance(governance_maximum, bool)
            and governance_minimum <= final_governance_revision
            <= governance_maximum
        ):
            raise PolicyInvalidTransition(
                "final impact governance revision drifted outside the envelope"
            )
        lifecycle_minimum = lifecycle_range.get("minimum")
        lifecycle_maximum = lifecycle_range.get("maximum")
        if not (
            isinstance(final_lifecycle_watermark, int)
            and not isinstance(final_lifecycle_watermark, bool)
            and isinstance(lifecycle_minimum, int)
            and not isinstance(lifecycle_minimum, bool)
            and isinstance(lifecycle_maximum, int)
            and not isinstance(lifecycle_maximum, bool)
            and lifecycle_minimum <= final_lifecycle_watermark
            <= lifecycle_maximum
        ):
            raise PolicyInvalidTransition(
                "final impact lifecycle watermark drifted outside the permitted movement"
            )
        envelope_dependency = envelope.get("dependency_index") or {}
        final_dependency = final.get("dependency_index") or {}
        if str(envelope_dependency.get("index_digest") or "") != str(
            final_dependency.get("index_digest") or ""
        ):
            raise PolicyInvalidTransition(
                "final impact dependency index drifted outside the envelope"
            )
        if (
            envelope_dependency.get("complete") is not True
            or final_dependency.get("complete") is not True
            or envelope_dependency.get("oracle_version")
            != DEPENDENCY_INDEX_VERSION
            or final_dependency.get("oracle_version")
            != DEPENDENCY_INDEX_VERSION
        ):
            raise PolicyInvalidTransition(
                "final impact dependency index is incomplete"
            )
        if (
            envelope.get("predecessor", {}).get("candidate_id")
            != final.get("predecessor", {}).get("candidate_id")
            or envelope.get("predecessor", {}).get("manifest_digest")
            != final.get("predecessor", {}).get("manifest_digest")
            or envelope.get("candidate", {}).get("candidate_id")
            != final.get("candidate", {}).get("candidate_id")
            or envelope.get("candidate", {}).get("manifest_digest")
            != final.get("candidate", {}).get("manifest_digest")
        ):
            raise PolicyInvalidTransition(
                "final impact predecessor/candidate drifted outside the envelope"
            )
        preview_members = {
            (str(member["application_id"]), int(member["cycle"]))
            for member in preview.get("members", [])
        }
        final_members = {
            (str(member["application_id"]), int(member["cycle"]))
            for member in final.get("members", [])
        }
        removed = preview_members - final_members
        if removed:
            raise PolicyInvalidTransition(
                "final impact removed members outside the approved envelope"
            )
        added = final_members - preview_members
        max_added = int(envelope.get("member_delta_rules", {}).get("max_added", 0))
        if len(added) > max_added:
            raise PolicyInvalidTransition(
                "final impact expanded beyond the approved member delta"
            )
        max_total = int(envelope.get("count_ceilings", {}).get("max_total", 0))
        if len(final_members) > max_total:
            raise PolicyInvalidTransition(
                "final impact exceeds the approved count ceiling"
            )
        per_partition = envelope.get("count_ceilings", {}).get(
            "per_partition", {}
        )
        final_partition_counts: dict[str, int] = {}
        for member in final.get("members", []):
            partition = str(member.get("partition") or "")
            final_partition_counts[partition] = (
                final_partition_counts.get(partition, 0) + 1
            )
        for partition, ceiling in per_partition.items():
            if final_partition_counts.get(str(partition), 0) > int(ceiling):
                raise PolicyInvalidTransition(
                    "final impact exceeds the approved partition ceiling"
                )

    def _impose_hold(
        self,
        staged: SQLiteTargetStore,
        *,
        principal: PolicyPrincipal,
        reason_code: str,
        hold_scope: str,
        evidence_digest: str | None = None,
        outbox: bool = True,
    ) -> dict[str, Any]:
        """Append one immutable Policy Safety Hold fact: reason, actor,
        authority revision, evidence digest, audit binding and the fixed
        recovery criterion.  Holds never auto-expire; only an explicit
        governed recovery command appends the release."""
        event = self._append_governance_event(
            staged,
            kind="hold_imposed",
            principal=principal,
            reason_code=reason_code,
            details={
                "hold_id": None,
                "scope": principal.scope,
                "hold_scope": hold_scope,
                "authority_revision": len(staged.policy_governance_events),
                "evidence_digest": (
                    evidence_digest
                    if isinstance(evidence_digest, str) and evidence_digest
                    else None
                ),
                "recovery_criterion_id": HOLD_RECOVERY_CRITERION_ID,
                "recovery_criterion_digest": s09_content_digest(
                    {
                        "criterion_id": HOLD_RECOVERY_CRITERION_ID,
                        "reason_code": reason_code,
                        "hold_scope": hold_scope,
                    }
                ),
            },
        )
        event["hold_id"] = event["event_id"]
        event["details"] = event.get("details") or {}
        event["details"]["hold_id"] = event["event_id"]
        if outbox:
            AuditOutboxOwner(staged).append_outbox(
                {
                    "event_id": self._stable_id(
                        "outbox", f"{event['event_id']}:hold"
                    ),
                    "kind": "s09_hold_imposed",
                    "scope": principal.scope,
                    "hold_id": event["event_id"],
                    "reason_code": reason_code,
                    "hold_scope": hold_scope,
                    "status": "pending",
                }
            )
        self._append_audit(
            staged,
            action="impose_hold",
            principal=principal,
            result="accepted",
            reason_code="S09_HOLD_IMPOSED",
            details={
                "hold_id": event["event_id"],
                "hold_reason": reason_code,
                "hold_scope": hold_scope,
                "governance_event_id": event["event_id"],
            },
        )
        return event

    def _validate_hold_scope(
        self, owner: SQLiteTargetStore, hold_scope: str
    ) -> str:
        """Validate one hold scope against the served scope or a
        Lifecycle-authoritative application identity before any fact is
        appended.  ``open_cycle`` and the served scope are always provable;
        a concrete application identity must exist in the Lifecycle
        snapshot.  A narrow scope under the served prefix that cannot be
        proved expands to the smallest trustworthy parent (the served
        scope), so the hold retains its protective effect instead of
        becoming an active no-op.  Any other scope is rejected with zero
        Governance/audit-success/idempotency/outbox business delta."""
        if hold_scope in {"open_cycle", S08_SCOPE}:
            return hold_scope
        snapshot = self._lifecycle_impact_snapshot(owner, None)
        known = {
            str(app.get("application_id") or "")
            for app in snapshot.get("applications", [])
            if isinstance(app, dict) and app.get("application_id")
        }
        if hold_scope in known:
            return hold_scope
        if hold_scope.startswith("C-DEMO/"):
            return S08_SCOPE
        raise PolicyInvalidTransition(
            "hold scope is not a provable served scope or application identity"
        )

    def impose_hold(
        self,
        *,
        principal: PolicyPrincipal,
        reason_code: str,
        hold_scope: str,
        idempotency_key: str,
        expected_governance_revision: int | None,
    ) -> dict[str, Any]:
        """Impose a scoped Policy Safety Hold: automatic routing, new
        RunSpec publication and current completion fail closed while any
        hold in the union is active.  The hold is append-only and carries
        the fixed recovery criterion; a timer never releases it.  The scope
        is validated against the served scope or a Lifecycle-authoritative
        application identity before any fact is appended."""
        _validate_principal(principal)
        if principal.role != "operator":
            raise PolicyInvalidTransition(
                "only the restricted activation operator may impose a hold"
            )
        if (
            not isinstance(reason_code, str)
            or not reason_code
            or reason_code.strip() != reason_code
        ):
            raise PolicyInvalidTransition("hold reason is invalid")
        if (
            not isinstance(hold_scope, str)
            or not hold_scope
            or hold_scope.strip() != hold_scope
        ):
            raise PolicyInvalidTransition("hold scope is invalid")
        fingerprint = self._fingerprint("impose_hold", reason_code, hold_scope)

        def mutate(staged: SQLiteTargetStore, key: str) -> dict[str, Any]:
            self._verify_governance_revision(staged, expected_governance_revision)
            resolved_scope = self._validate_hold_scope(staged, hold_scope)
            event = self._impose_hold(
                staged,
                principal=principal,
                reason_code=reason_code,
                hold_scope=resolved_scope,
            )
            result = {
                "status": "accepted",
                "hold_id": event["event_id"],
                "hold_scope": resolved_scope,
                "reason_code": reason_code,
                "recovery_criterion_id": HOLD_RECOVERY_CRITERION_ID,
                "recovery_criterion_digest": event["recovery_criterion_digest"],
                "governance_event_id": event["event_id"],
                "governance_revision": len(staged.policy_governance_events),
            }
            staged.idempotency[key] = (fingerprint, result)
            return result

        return self._run_command(
            principal, "impose_hold", idempotency_key, fingerprint, mutate
        )

    def _hold_events(
        self, owner: SQLiteTargetStore, scope: str
    ) -> list[dict[str, Any]]:
        return [
            event
            for event in owner.policy_governance_events
            if event.get("kind") in {"hold_imposed", "hold_released"}
            and event.get("scope") == scope
        ]

    def load_final_impact(
        self,
        digest: str,
        *,
        store: SQLiteTargetStore | None = None,
    ) -> dict[str, Any] | None:
        """The read-only seam Lifecycle uses to consume one immutable final
        impact manifest.  Never mutates anything."""
        owner = store if store is not None else self._store
        if store is None:
            owner.reload()
        events = [
            event
            for event in owner.policy_governance_events
            if event.get("kind") == "impact_finalized"
            and event.get("digest") == digest
        ]
        if not events:
            return None
        manifest = events[-1].get("manifest")
        if not isinstance(manifest, dict):
            return None
        if (
            manifest.get("schema_version") != IMPACT_MANIFEST_SCHEMA
            or not verify_impact_manifest_digest(manifest)
            or manifest.get("digest") != digest
        ):
            return None
        return manifest

    def recover_hold(
        self,
        *,
        principal: PolicyPrincipal,
        hold_id: str,
        recovery_generation: int,
        idempotency_key: str,
        expected_governance_revision: int | None,
    ) -> dict[str, Any]:
        """The separate, idempotent, separation-of-duties recovery command:
        release one exact Policy Safety Hold only after the recovery
        generation is the current active generation, every final-impact
        member has a reconcilable disposition, and no durable rerun
        obligation remains.  Appends only hold_released, audit,
        idempotency and outbox facts; it never restores an old success and
        never rewinds the Ledger."""
        _validate_principal(principal)
        if principal.role != "approver":
            raise PolicyInvalidTransition(
                "only the independent Policy Approver may confirm recovery"
            )
        if (
            not isinstance(hold_id, str)
            or not hold_id
            or hold_id.strip() != hold_id
        ):
            raise PolicyInvalidTransition("hold identity is invalid")
        if (
            isinstance(recovery_generation, bool)
            or not isinstance(recovery_generation, int)
            or recovery_generation < 1
        ):
            raise PolicyInvalidTransition("recovery generation is invalid")
        fingerprint = self._fingerprint(
            "recover_hold", hold_id, recovery_generation
        )

        def mutate(staged: SQLiteTargetStore, key: str) -> dict[str, Any]:
            self._verify_governance_revision(staged, expected_governance_revision)
            hold_events = [
                event
                for event in staged.policy_governance_events
                if event.get("kind") == "hold_imposed"
                and event.get("hold_id") == hold_id
            ]
            if not hold_events:
                raise PolicyNotFound("hold is unavailable")
            hold = hold_events[-1]
            released_events = [
                event
                for event in staged.policy_governance_events
                if event.get("kind") == "hold_released"
                and event.get("hold_id") == hold_id
            ]
            if released_events:
                # The hold is no longer active.  A second recovery key for
                # the same exact recovery generation replays the original
                # semantic result with zero new event/audit/outbox delta; a
                # different generation conflicts (a hold can never be
                # released twice).
                last_release = released_events[-1]
                if (
                    int(last_release.get("recovery_generation") or 0)
                    == int(recovery_generation)
                ):
                    return {
                        "status": "accepted",
                        "hold_id": hold_id,
                        "hold_released_event_id": last_release["event_id"],
                        "recovery_generation": recovery_generation,
                        "governance_revision": len(
                            staged.policy_governance_events
                        ),
                        "replayed": True,
                    }
                raise PolicyInvalidTransition(
                    "hold was already released at a different recovery generation"
                )
            if str(hold.get("actor", {}).get("subject") or "") == principal.subject:
                raise PolicyInvalidTransition(
                    "the hold actor cannot confirm its own release"
                )
            active = self._fold_active_projection(
                staged.policy_governance_events, S08_SCOPE
            )
            if active is None:
                raise PolicyInvalidTransition("no active governed release exists")
            if int(active["active_generation"]) != int(recovery_generation):
                raise PolicyInvalidTransition(
                    "recovery generation is not the current active generation"
                )
            # Active-hold and fixed-criterion proof: the named hold is still
            # in the append-only active union (no release fact exists) and
            # its recovery criterion identity/digest re-verify exactly.
            if (
                str(hold.get("recovery_criterion_id") or "")
                != HOLD_RECOVERY_CRITERION_ID
                or str(hold.get("recovery_criterion_digest") or "")
                != s09_content_digest(
                    {
                        "criterion_id": HOLD_RECOVERY_CRITERION_ID,
                        "reason_code": hold.get("reason_code"),
                        "hold_scope": hold.get("hold_scope"),
                    }
                )
            ):
                raise PolicyInvalidTransition(
                    "hold recovery criterion is not verifiable"
                )
            # Integrity and protected-baseline proof: the active release
            # manifest and its bound validation/approval evidence must
            # still verify against the Registry before any release.
            active_manifest = self._verify_pinned_manifest(
                staged,
                active["manifest_id"],
                active["manifest_digest"],
            )
            self._verify_bound_evidence(
                staged,
                active_manifest,
                candidate_id=active["candidate_id"],
                validation_bundle_id=active["validation_bundle_id"],
                validation_bundle_digest=active["validation_bundle_digest"],
                approval_binding_id=active["approval_binding_id"],
                approval_binding_digest=active["approval_binding_digest"],
            )
            final_digest = active.get("final_impact_digest")
            if not final_digest and not active.get("bootstrap"):
                # An active release without a final impact digest is only
                # the bootstrap baseline: a non-bootstrap active without the
                # final impact can never be released as a complete empty
                # application set.
                raise PolicyInvalidTransition(
                    "active release without a final impact is not a bootstrap hold"
                )
            snapshot = self._lifecycle_impact_snapshot(staged, final_digest)
            unconsumed = int(snapshot.get("unconsumed_count") or 0)
            outstanding = int(snapshot.get("outstanding_count") or 0)
            if unconsumed or outstanding:
                raise PolicyInvalidTransition(
                    "recovery requires every final-impact disposition"
                )
            # Hold-delivery proof: the exact hold fact must have reached
            # every application covered by its scope -- Lifecycle consumed
            # the imposed hold (the hold identity is in the application's
            # active hold set) and the application sits in the hold's
            # Unprocessable frame with no current run, so old run/route/
            # work/exception references are non-operable (claims and review
            # work only proceed from other phases, and every work/exception
            # record binds a run id).  A pending hold outbox also rejects
            # recovery.  This replaces the bootstrap empty-application
            # shortcut: an unconsumed hold can never be released as a
            # complete empty application set.
            if any(
                event.get("kind") == "s09_hold_imposed"
                and event.get("hold_id") == hold_id
                and event.get("status") == "pending"
                for event in staged.outbox
            ):
                raise PolicyInvalidTransition("hold outbox delivery is pending")
            hold_scope = str(hold.get("hold_scope") or "")
            covered: list[dict[str, Any]] = []
            for app_entry in snapshot.get("applications", []):
                if not isinstance(app_entry, dict):
                    continue
                application_id = str(app_entry.get("application_id") or "")
                phase = str(app_entry.get("phase") or "")
                if phase in {"Verification Completed", "Terminated"}:
                    continue
                if not (
                    hold_scope in {"open_cycle", S08_SCOPE}
                    or hold_scope == application_id
                ):
                    continue
                covered.append(app_entry)
            for app_entry in covered:
                active_hold_ids = app_entry.get("active_hold_ids")
                if (
                    not isinstance(active_hold_ids, list)
                    or hold_id not in {str(item) for item in active_hold_ids}
                ):
                    raise PolicyInvalidTransition(
                        "hold delivery is not reconciled to a covered application"
                    )
                if app_entry.get("old_references_operable") is not False:
                    raise PolicyInvalidTransition(
                        "an old current reference remains operable"
                    )
                if str(app_entry.get("phase") or "") != "Unprocessable":
                    raise PolicyInvalidTransition(
                        "hold consumption is not in force for a covered application"
                    )
                # Old route/work/exception references are non-operable by
                # construction once the hold frame is in force: Lifecycle
                # claims and review work only proceed from other phases, and
                # every work/exception record binds a run id, so with no
                # current run and the Unprocessable hold frame no old
                # reference can be operated.  (The ``route`` string itself
                # may carry the disposition's stale/recheck value while the
                # hold is still active; the phase is the operability gate.)
            # Durable rerun obligations: every open-cycle member that
            # received an applied disposition must carry its Operational
            # Re-evaluation job, unless the hold itself blocked the
            # reevaluation (released only by this command) or the member is
            # under a durable assembly obligation (dependency-context
            # change: the application stays in Assembly, proven by the
            # Lifecycle snapshot, with the disposition as its durable
            # receipt).
            if final_digest:
                manifest = self.load_final_impact(final_digest, store=staged)
                if manifest is not None:
                    coverage = snapshot.get("member_coverage") or {}
                    for member in manifest.get("members", []):
                        if str(member.get("partition") or "") != "open_cycle":
                            continue
                        key = (
                            f"{str(member.get('application_id') or '')}:"
                            f"{int(member.get('cycle') or 0)}"
                        )
                        info = coverage.get(key) or {}
                        member_phase = next(
                            (
                                str(item.get("phase") or "")
                                for item in snapshot.get("applications") or []
                                if str(item.get("application_id") or "")
                                == str(member.get("application_id") or "")
                                and int(item.get("cycle") or 0)
                                == int(member.get("cycle") or 0)
                            ),
                            "",
                        )
                        if (
                            info.get("disposition") == "applied"
                            and int(info.get("reevaluation_job_count") or 0) < 1
                            and not info.get("blocked_by_hold")
                            and member_phase != "Assembly"
                        ):
                            raise PolicyInvalidTransition(
                                "recovery requires durable rerun obligations"
                            )
            event = self._append_governance_event(
                staged,
                kind="hold_released",
                principal=principal,
                reason_code="S09_HOLD_RELEASED",
                details={
                    "hold_id": hold_id,
                    "release_reason": "S09_RECOVERY_CRITERION_PASSED",
                    "recovery_generation": recovery_generation,
                    "released_hold_reason_code": hold.get("reason_code"),
                    "released_hold_scope": hold.get("hold_scope"),
                },
            )
            AuditOutboxOwner(staged).append_outbox(
                {
                    "event_id": self._stable_id(
                        "outbox", f"{event['event_id']}:release"
                    ),
                    "kind": "s09_hold_released",
                    "scope": S08_SCOPE,
                    "hold_id": hold_id,
                    "recovery_generation": recovery_generation,
                    "status": "pending",
                }
            )
            self._append_audit(
                staged,
                action="recover_hold",
                principal=principal,
                result="accepted",
                reason_code="S09_HOLD_RELEASED",
                details={
                    "hold_id": hold_id,
                    "governance_event_id": event["event_id"],
                    "recovery_generation": recovery_generation,
                },
            )
            result = {
                "status": "accepted",
                "hold_id": hold_id,
                "hold_released_event_id": event["event_id"],
                "recovery_generation": recovery_generation,
                "governance_revision": len(staged.policy_governance_events),
            }
            staged.idempotency[key] = (fingerprint, result)
            return result

        return self._run_command(
            principal, "recover_hold", idempotency_key, fingerprint, mutate
        )

    def propose_rollback(
        self,
        *,
        principal: PolicyPrincipal,
        release_candidate_id: str,
        reason_code: str,
        idempotency_key: str,
        expected_governance_revision: int | None,
    ) -> dict[str, Any]:
        """Propose a governed rollback: revalidate the exact historical
        release against the current semantic field catalog, policy
        constraints, input contract, checker, artifact integrity and
        protected baseline, then stage a fresh rollback candidate that
        continues through the ordinary preview/approval/schedule/activation
        pipeline at current time.  A failed gate leaves the hold in place
        and the only publication path is a governed forward fix; nothing is
        ever rewound and no historical active flag is restored."""
        _validate_principal(principal)
        if principal.role != "operator":
            raise PolicyInvalidTransition(
                "only the restricted incident principal may propose a rollback"
            )
        if (
            not isinstance(release_candidate_id, str)
            or not release_candidate_id
            or release_candidate_id.strip() != release_candidate_id
        ):
            raise PolicyInvalidTransition("release identity is invalid")
        if (
            not isinstance(reason_code, str)
            or not reason_code
            or reason_code.strip() != reason_code
        ):
            raise PolicyInvalidTransition("rollback reason is invalid")
        fingerprint = self._fingerprint(
            "propose_rollback", release_candidate_id, reason_code
        )

        def mutate(staged: SQLiteTargetStore, key: str) -> dict[str, Any]:
            self._verify_governance_revision(staged, expected_governance_revision)
            states = self._fold_candidates(staged)
            release = states.get(release_candidate_id)
            if (
                release is None
                or release.get("status") not in {"active", "superseded"}
            ):
                # Only a historically governed release is rollback-eligible;
                # anything else keeps the hold and requires a forward fix.
                raise PolicyInvalidTransition(
                    "ROLLBACK_INCOMPATIBLE_RELEASE_NOT_GOVERNED"
                )
            # The exact content-addressed manifest and every component must
            # still verify against the Registry: no latest/nearest/
            # reconstructed substitution is ever allowed.
            manifest = self._verify_pinned_manifest(
                staged,
                release["manifest_id"],
                release["manifest_digest"],
            )
            draft_id = self._stable_id(
                "draft", f"rollback:{release_candidate_id}:{fingerprint}"
            )
            candidate_id = self._stable_id(
                "candidate",
                f"{draft_id}:1:{release['manifest_digest']}",
            )
            if candidate_id in states:
                raise PolicyConflict("rollback candidate identity already exists")
            historical_frozen_events = [
                event
                for event in staged.policy_governance_events
                if event.get("kind") == "candidate_frozen"
                and event.get("candidate_id") == release_candidate_id
            ]
            if len(historical_frozen_events) != 1:
                raise PolicyUnavailable(
                    "historical release frozen evidence is unavailable"
                )
            historical_frozen = historical_frozen_events[-1]
            if not any(
                item.get("manifest_id") == manifest["manifest_id"]
                for item in staged.policy_manifests
            ):
                staged.policy_manifests.append(manifest)
            # The rollback candidate reuses the exact historical manifest
            # and mapping ledger; its frozen fact is appended before fresh
            # validation so the validator reads the immutable snapshot the
            # same way it reads any candidate (nothing is persisted on a
            # failed gate, so protected delta stays zero).
            self._append_governance_event(
                staged,
                kind="candidate_frozen",
                principal=PolicyPrincipal(
                    subject=self._operator_subject,
                    role="operator",
                    scope=S08_SCOPE,
                    source_id="s08-rollback",
                ),
                reason_code="S09_ROLLBACK_CANDIDATE_FROZEN",
                details={
                    "candidate_id": candidate_id,
                    "manifest_id": manifest["manifest_id"],
                    "manifest_digest": manifest["digest"],
                    "components": copy.deepcopy(release.get("components") or []),
                    "mapping_ledger_id": historical_frozen.get("mapping_ledger_id"),
                    "mapping_ledger_digest": historical_frozen.get(
                        "mapping_ledger_digest"
                    ),
                    "metadata": {
                        "scope": S08_SCOPE,
                        "validity": {"valid_from": "2000-01-01T00:00:00Z"},
                        "source": f"rollback:{release_candidate_id}",
                        "reason": reason_code,
                    },
                    "rollback": True,
                    "rollback_target_id": release_candidate_id,
                },
            )
            # Fresh validation against the current gates (semantic field
            # catalog, operators, input contract, protected baseline).
            rollback_state = {
                **copy.deepcopy(release),
                "candidate_id": candidate_id,
            }
            bundle, outcome = self._validate_candidate(staged, rollback_state)
            if outcome != "validated":
                raise PolicyInvalidTransition("ROLLBACK_INCOMPATIBLE_VALIDATION")
            staged.policy_drafts[draft_id] = {
                "draft_id": draft_id,
                "schema_version": "s08-draft/1",
                "scope": S08_SCOPE,
                "status": "draft",
                "bootstrap": False,
                "rollback": True,
                "rollback_target_id": release_candidate_id,
                "created_by": principal.subject,
                "created_at": self._trusted_time(),
                "revision": 1,
                "source_bundle_id": str(release.get("source_bundle_id") or ""),
                "source_sha256": str(release.get("source_sha256") or ""),
                "knowledge_sha256": str(release.get("knowledge_sha256") or ""),
                "mapping_ledger_id": str(release.get("mapping_ledger_id") or ""),
                "mapping_ledger_digest": str(
                    release.get("mapping_ledger_digest") or ""
                ),
                "artifact_ids": [],
                "components": copy.deepcopy(release.get("components") or []),
                "metadata": {
                    "scope": S08_SCOPE,
                    "validity": {"valid_from": "2000-01-01T00:00:00Z"},
                    "source": f"rollback:{release_candidate_id}",
                    "reason": reason_code,
                },
                "candidate_id": candidate_id,
            }
            staged.policy_artifacts.append(bundle["artifact"])
            operator = PolicyPrincipal(
                subject=self._operator_subject,
                role="operator",
                scope=S08_SCOPE,
                source_id="s08-rollback",
            )
            self._append_governance_event(
                staged,
                kind="rollback_proposed",
                principal=principal,
                reason_code="S09_ROLLBACK_PROPOSED",
                details={
                    "release_candidate_id": release_candidate_id,
                    "rollback_reason": reason_code,
                    "manifest_id": manifest["manifest_id"],
                    "manifest_digest": manifest["digest"],
                },
            )
            self._append_governance_event(
                staged,
                kind="validated",
                principal=operator,
                reason_code="S09_ROLLBACK_VALIDATED",
                details={
                    "candidate_id": candidate_id,
                    "validation_bundle_id": bundle["validation_bundle_id"],
                    "validation_bundle_digest": bundle["digest"],
                },
            )
            self._append_audit(
                staged,
                action="propose_rollback",
                principal=principal,
                result="accepted",
                reason_code="S09_ROLLBACK_PROPOSED",
                details={
                    "release_candidate_id": release_candidate_id,
                    "rollback_candidate_id": candidate_id,
                    "rollback_reason": reason_code,
                },
            )
            result = {
                "status": "accepted",
                "candidate_id": candidate_id,
                "manifest_id": manifest["manifest_id"],
                "manifest_digest": manifest["digest"],
                "validation_bundle_id": bundle["validation_bundle_id"],
                "validation_bundle_digest": bundle["digest"],
                "rollback_target_id": release_candidate_id,
                "compatibility": {
                    "compatible": True,
                    "reason_code": "S09_ROLLBACK_COMPATIBLE",
                },
                "governance_revision": len(staged.policy_governance_events),
            }
            staged.idempotency[key] = (fingerprint, result)
            return result

        return self._run_command(
            principal,
            "propose_rollback",
            idempotency_key,
            fingerprint,
            mutate,
        )

    # -------------------------------------------------- S09 replay/simulation

    def _diagnostic_view(
        self,
        owner: SQLiteTargetStore,
        *,
        namespace: str,
        release_candidate_id: str,
        application_id: str,
    ) -> S09DiagnosticView | dict[str, Any]:
        """The store-owned least-privilege factory: resolve the exact
        governed release and one fixed evidence snapshot, then hand the
        isolated runner only the read-only capability view.  Missing or
        mismatched artifacts yield closed INVALID/UNREPRODUCIBLE bundles;
        latest/nearest/reconstructed substitution is forbidden; the view
        and every failure bundle carry identity fields only -- never raw
        field values, OCR text, attachment locators or free text."""
        states = self._fold_candidates(owner)
        release_state = states.get(release_candidate_id)
        if (
            release_state is None
            or release_state.get("status") not in {"active", "superseded"}
        ):
            return {
                "schema_version": "s09-diagnostic-bundle/1",
                "namespace": namespace,
                "release_candidate_id": release_candidate_id,
                "application_id": application_id,
                "bundle_id": None,
                "outcome": "INVALID",
                "reason_code": "RELEASE_NOT_GOVERNED",
                "business_revision_delta": 0,
            }
        try:
            manifest = self._verify_pinned_manifest(
                owner,
                release_state["manifest_id"],
                release_state["manifest_digest"],
            )
            checker_artifact = self._artifact(
                owner, self._component_id(manifest, "checker")
            )
            release = TargetRelease.from_artifact(checker_artifact)
        except Exception:
            return {
                "schema_version": "s09-diagnostic-bundle/1",
                "namespace": namespace,
                "release_candidate_id": release_candidate_id,
                "application_id": application_id,
                "bundle_id": None,
                "outcome": "UNREPRODUCIBLE",
                "reason_code": "ARTIFACT_UNAVAILABLE",
                "business_revision_delta": 0,
            }
        provider = self._diagnostic_snapshot_provider
        if provider is None:
            return {
                "schema_version": "s09-diagnostic-bundle/1",
                "namespace": namespace,
                "release_candidate_id": release_candidate_id,
                "application_id": application_id,
                "bundle_id": None,
                "outcome": "UNREPRODUCIBLE",
                "reason_code": "FIXED_SNAPSHOT_UNAVAILABLE",
                "business_revision_delta": 0,
            }
        try:
            snapshot = provider(owner, application_id)
        except Exception:
            snapshot = None
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("schema_version") != "s09-diagnostic-snapshot/1"
            or snapshot.get("complete") is not True
            or snapshot.get("application_id") != application_id
            or not isinstance(snapshot.get("run_id"), str)
            or not isinstance(snapshot.get("run_spec"), dict)
            or snapshot["run_spec"].get("run_id") != snapshot["run_id"]
            or snapshot["run_spec"].get("application_id") != application_id
            or snapshot["run_spec"].get("cycle") != snapshot.get("cycle")
            or snapshot["run_spec"].get("evidence_snapshot_id")
            != snapshot.get("evidence_snapshot_id")
            or snapshot["run_spec"].get("evidence_snapshot_digest")
            != snapshot.get("evidence_snapshot_digest")
        ):
            return {
                "schema_version": "s09-diagnostic-bundle/1",
                "namespace": namespace,
                "release_candidate_id": release_candidate_id,
                "application_id": application_id,
                "bundle_id": None,
                "outcome": "UNREPRODUCIBLE",
                "reason_code": "FIXED_SNAPSHOT_UNAVAILABLE",
                "business_revision_delta": 0,
            }
        fixed_spec = copy.deepcopy(snapshot["run_spec"])
        public = release.public_manifest()
        run_spec = {
            **copy.deepcopy(fixed_spec),
            "run_id": f"{namespace}:{release_candidate_id}:{application_id}",
            "release_id": public["release_id"],
            "release_digest": public["digest"],
            "checker_build": public["checker_build"],
            "limits": copy.deepcopy(public["limits"]),
            "applicable_check_ids": copy.deepcopy(
                public["applicable_check_ids"]
            ),
            "applicable_check_count": public["applicable_check_count"],
            "baseline_release": copy.deepcopy(public),
        }
        worker_identity = (
            "s09-replay-worker"
            if namespace == "s09-replay"
            else "s09-simulation-worker"
        )
        return S09DiagnosticView(
            namespace=namespace,
            release_candidate_id=release_candidate_id,
            application_id=application_id,
            release_manifest_id=manifest["manifest_id"],
            release_manifest_digest=manifest["digest"],
            approval_binding_id=str(
                release_state.get("approval_binding_id") or ""
            ),
            worker_identity=worker_identity,
            release=release,
            fixed_run_spec=json.dumps(
                run_spec, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        )

    def _diagnostic_run_bundle(self, view: S09DiagnosticView) -> dict[str, Any]:
        """The isolated runner seam: execute one diagnostic bundle only from
        the read-only capability view -- never from a store.  The runner
        cannot persist, resolve current state or write business audit; it
        writes only its own immutable content-addressed bundle."""
        writer = S09DiagnosticBundleWriter(
            namespace=view.namespace,
            worker_identity=view.worker_identity,
        )
        return S09DiagnosticRunner(view.worker_identity, writer).run(view)

    def _require_diagnostic_principal(
        self, principal: PolicyPrincipal, namespace: str
    ) -> None:
        """Least-privilege diagnostic identity: one separate operator role
        per namespace (replay vs simulation), never the activation operator
        and never a cross-namespace credential."""
        _validate_principal(principal)
        expected_role = {
            "replay": "replay_operator",
            "simulation": "simulation_operator",
        }.get(namespace)
        if principal.role != expected_role:
            raise PolicyInvalidTransition(
                f"only the isolated {namespace} workload may run diagnostics"
            )

    def _run_diagnostic_command(
        self,
        *,
        principal: PolicyPrincipal,
        action: str,
        namespace: str,
        release_candidate_id: str,
        application_id: str,
        idempotency_key: str,
        expected_governance_revision: int | None,
    ) -> dict[str, Any]:
        """Claim, run and settle one durable isolated diagnostic command."""
        fingerprint = self._fingerprint(action, release_candidate_id, application_id)
        operation_key = self._idempotency_key(
            principal, action, idempotency_key
        )
        worker_identity = (
            "s09-replay-worker"
            if namespace == "s09-replay"
            else "s09-simulation-worker"
        )
        job_id = self._stable_id(
            "policy_job", f"s09-diagnostic:{operation_key}"
        )
        claimed_job: dict[str, Any] | None = None
        view_or_bundle: S09DiagnosticView | dict[str, Any] | None = None
        authority_revision = 0
        claim_started_at = 0

        for _ in range(3):
            with self._lock:
                self._store.reload()
                replay = self._replay_or_conflict(
                    self._store, operation_key, fingerprint
                )
                if replay is not None:
                    return self._result(replay[1], replayed=True)
                self._verify_governance_revision(
                    self._store, expected_governance_revision
                )
                candidates = self._fold_candidates(self._store)
                if release_candidate_id not in candidates:
                    raise PolicyNotFound(release_candidate_id)
                claim_started_at = self._trusted_time()
                jobs = [
                    item
                    for item in self._store.policy_jobs
                    if item.get("policy_job_id") == job_id
                ]
                if len(jobs) > 1:
                    raise PolicyUnavailable(
                        "diagnostic command authority is not unique"
                    )
                current = jobs[0] if jobs else None
                if current is not None:
                    if current.get("fingerprint") != fingerprint:
                        raise PolicyConflict(
                            "diagnostic command fingerprint conflicts"
                        )
                    if current.get("status") == "diagnostic_complete":
                        raise PolicyUnavailable(
                            "diagnostic result binding is unavailable"
                        )
                    if (
                        current.get("status") == "diagnostic_running"
                        and int(current.get("lease_until") or 0)
                        > claim_started_at
                    ):
                        raise PolicyConflict(
                            "diagnostic operation is already running"
                        )
                    if current.get("status") != "diagnostic_running":
                        raise PolicyUnavailable(
                            "diagnostic command state is not recoverable"
                        )
                view_or_bundle = self._diagnostic_view(
                    self._store,
                    namespace=namespace,
                    release_candidate_id=release_candidate_id,
                    application_id=application_id,
                )
                authority_revision = len(self._store.policy_governance_events)
                if isinstance(view_or_bundle, dict):
                    input_digest = s09_content_digest(view_or_bundle)
                else:
                    input_digest = s09_content_digest(
                        {
                            "namespace": view_or_bundle.namespace,
                            "release_candidate_id": (
                                view_or_bundle.release_candidate_id
                            ),
                            "application_id": view_or_bundle.application_id,
                            "release_manifest_id": (
                                view_or_bundle.release_manifest_id
                            ),
                            "release_manifest_digest": (
                                view_or_bundle.release_manifest_digest
                            ),
                            "approval_binding_id": (
                                view_or_bundle.approval_binding_id
                            ),
                            "worker_identity": view_or_bundle.worker_identity,
                            "fixed_run_spec_sha256": hashlib.sha256(
                                view_or_bundle.fixed_run_spec.encode("utf-8")
                            ).hexdigest(),
                        }
                    )
                staged = copy.deepcopy(self._store)
                staged_jobs = [
                    item
                    for item in staged.policy_jobs
                    if item.get("policy_job_id") == job_id
                ]
                if staged_jobs:
                    job = staged_jobs[0]
                    if job.get("input_digest") != input_digest:
                        raise PolicyConflict(
                            "diagnostic fixed input changed after lease expiry"
                        )
                    job["fence"] = int(job.get("fence") or 0) + 1
                    job["attempt_no"] = int(job.get("attempt_no") or 0) + 1
                else:
                    job = {
                        "policy_job_id": job_id,
                        "kind": "s09_diagnostic",
                        "namespace": namespace,
                        "candidate_id": release_candidate_id,
                        "application_id": application_id,
                        "operation_key": operation_key,
                        "fingerprint": fingerprint,
                        "input_digest": input_digest,
                        "fence": 1,
                        "attempt_no": 1,
                        "created_at": claim_started_at,
                    }
                    staged.policy_jobs.append(job)
                job.update(
                    {
                        "status": "diagnostic_running",
                        "worker_id": worker_identity,
                        "lease_until": claim_started_at + 30,
                    }
                )
                staged.policy_attempts.append(
                    self._attempt_record(
                        job, status="running", started_at=claim_started_at
                    )
                )
                try:
                    staged.persist()
                except StaleStoreRevision:
                    self._store.reload()
                    continue
                self._store = staged
                claimed_job = copy.deepcopy(job)
                break
        if claimed_job is None or view_or_bundle is None:
            raise PolicyConflict("diagnostic claim raced with another writer")

        try:
            bundle = (
                view_or_bundle
                if isinstance(view_or_bundle, dict)
                else self._diagnostic_run_bundle(view_or_bundle)
            )
        except Exception:
            bundle = {
                "schema_version": "s09-diagnostic-bundle/1",
                "namespace": namespace,
                "release_candidate_id": release_candidate_id,
                "application_id": application_id,
                "bundle_id": None,
                "outcome": "UNREPRODUCIBLE",
                "reason_code": "CHECKER_EXECUTION_FAILED",
                "business_revision_delta": 0,
            }
        result = {
            "status": "accepted",
            "namespace": namespace,
            "release_candidate_id": release_candidate_id,
            "bundle_count": 1,
            "bundles": [bundle],
            "business_revision_delta": 0,
            "governance_revision": authority_revision,
        }
        for _ in range(3):
            with self._lock:
                self._store.reload()
                replay = self._replay_or_conflict(
                    self._store, operation_key, fingerprint
                )
                if replay is not None:
                    return self._result(replay[1], replayed=True)
                current_jobs = [
                    item
                    for item in self._store.policy_jobs
                    if item.get("policy_job_id") == job_id
                ]
                if len(current_jobs) != 1:
                    raise PolicyConflict(
                        "diagnostic claim authority is unavailable"
                    )
                current = current_jobs[0]
                if not (
                    current.get("status") == "diagnostic_running"
                    and current.get("worker_id")
                    == claimed_job.get("worker_id")
                    and current.get("fence") == claimed_job.get("fence")
                    and current.get("attempt_no")
                    == claimed_job.get("attempt_no")
                    and int(current.get("lease_until") or 0)
                    > self._trusted_time()
                ):
                    raise PolicyConflict("diagnostic claim is stale")
                staged = copy.deepcopy(self._store)
                settled = next(
                    item
                    for item in staged.policy_jobs
                    if item.get("policy_job_id") == job_id
                )
                settled["status"] = "diagnostic_complete"
                settled["bundle_id"] = bundle.get("bundle_id")
                settled["bundle_digest"] = bundle.get("bundle_digest")
                settled["outcome"] = bundle.get("outcome")
                settled.pop("lease_until", None)
                staged.policy_attempts.append(
                    self._attempt_record(
                        claimed_job,
                        status="complete",
                        started_at=claim_started_at,
                        result={
                            "outcome": bundle.get("outcome"),
                            "bundle_id": bundle.get("bundle_id"),
                            "bundle_digest": bundle.get("bundle_digest"),
                        },
                    )
                )
                staged.idempotency[operation_key] = (fingerprint, result)
                try:
                    staged.persist()
                except StaleStoreRevision:
                    continue
                self._store = staged
                return self._result(result)
        raise PolicyConflict("diagnostic result commit raced with another writer")

    def replay_release(
        self,
        *,
        principal: PolicyPrincipal,
        release_candidate_id: str,
        application_id: str,
        idempotency_key: str,
        expected_governance_revision: int | None,
    ) -> dict[str, Any]:
        """Reproduction Replay: run the exact historical release over a
        fixed evidence snapshot with a namespaced diagnostic identity.
        Read-only: zero business, lifecycle, evidence, decision, routing,
        delivery, work or audit revisions are ever produced.  One explicit
        application identity is required: an omitted identity never
        enumerates the run universe outside an authorized scope."""
        self._require_diagnostic_principal(principal, "replay")
        if (
            not isinstance(release_candidate_id, str)
            or not release_candidate_id
            or release_candidate_id.strip() != release_candidate_id
        ):
            raise PolicyInvalidTransition("release identity is invalid")
        if (
            not isinstance(application_id, str)
            or not application_id
            or application_id.strip() != application_id
        ):
            raise PolicyInvalidTransition("application identity is invalid")
        if (
            not isinstance(idempotency_key, str)
            or not idempotency_key
            or idempotency_key.strip() != idempotency_key
            or len(idempotency_key) > 200
        ):
            raise PolicyInvalidTransition("idempotency key is invalid")
        if not self.audit_available or not self.storage_available:
            raise PolicyUnavailable(
                "required audit or storage authority is unavailable"
            )
        return self._run_diagnostic_command(
            principal=principal,
            action="replay_release",
            namespace="s09-replay",
            release_candidate_id=release_candidate_id,
            application_id=application_id,
            idempotency_key=idempotency_key,
            expected_governance_revision=expected_governance_revision,
        )

    def simulate_release(
        self,
        *,
        principal: PolicyPrincipal,
        release_candidate_id: str,
        application_id: str,
        idempotency_key: str,
        expected_governance_revision: int | None,
    ) -> dict[str, Any]:
        """Counterfactual Simulation: evaluate a candidate release over the
        current fixed evidence snapshot with a namespaced diagnostic
        identity.  Read-only; a diagnostic result can never become current.
        One explicit application identity is required: an omitted identity
        never enumerates the run universe outside an authorized scope."""
        self._require_diagnostic_principal(principal, "simulation")
        if (
            not isinstance(release_candidate_id, str)
            or not release_candidate_id
            or release_candidate_id.strip() != release_candidate_id
        ):
            raise PolicyInvalidTransition("release identity is invalid")
        if (
            not isinstance(application_id, str)
            or not application_id
            or application_id.strip() != application_id
        ):
            raise PolicyInvalidTransition("application identity is invalid")
        if (
            not isinstance(idempotency_key, str)
            or not idempotency_key
            or idempotency_key.strip() != idempotency_key
            or len(idempotency_key) > 200
        ):
            raise PolicyInvalidTransition("idempotency key is invalid")
        if not self.audit_available or not self.storage_available:
            raise PolicyUnavailable(
                "required audit or storage authority is unavailable"
            )
        return self._run_diagnostic_command(
            principal=principal,
            action="simulate_release",
            namespace="s09-simulation",
            release_candidate_id=release_candidate_id,
            application_id=application_id,
            idempotency_key=idempotency_key,
            expected_governance_revision=expected_governance_revision,
        )

    def cancel(
        self,
        *,
        principal: PolicyPrincipal,
        candidate_id: str,
        reason_code: str,
        idempotency_key: str,
        expected_governance_revision: int | None,
    ) -> dict[str, Any]:
        _validate_principal(principal)
        if principal.role != "admin":
            raise PolicyInvalidTransition("only the Rule Administrator may cancel")
        if (
            not isinstance(reason_code, str)
            or not reason_code
            or reason_code.strip() != reason_code
        ):
            raise PolicyInvalidTransition("cancellation reason is invalid")
        fingerprint = self._fingerprint("cancel", candidate_id, reason_code)

        def mutate(staged: SQLiteTargetStore, key: str) -> dict[str, Any]:
            self._verify_governance_revision(staged, expected_governance_revision)
            state = self._require_candidate_state(staged, candidate_id)
            if state["status"] in {"active", "superseded", "rejected", "cancelled"}:
                raise PolicyInvalidTransition(
                    f"candidate {candidate_id} cannot be cancelled from {state['status']}"
                )
            event = self._append_governance_event(
                staged,
                kind="cancelled",
                principal=principal,
                reason_code=reason_code,
                details={"candidate_id": candidate_id},
            )
            if state["status"] == "scheduled" and state.get("reservation_id"):
                reservation = staged.policy_schedule_reservations.get(
                    state["reservation_id"]
                )
                if reservation is not None and reservation.get("status") == "pending":
                    reservation["status"] = "cancelled"
                    staged.policy_schedule_reservations[state["reservation_id"]] = (
                        reservation
                    )
            self._append_audit(
                staged,
                action="cancel",
                principal=principal,
                result="accepted",
                reason_code="S08_CANDIDATE_CANCELLED",
                details={
                    "candidate_id": candidate_id,
                    "governance_event_id": event["event_id"],
                },
            )
            result = {
                "status": "accepted",
                "candidate_id": candidate_id,
                "reason_code": reason_code,
                "governance_revision": len(staged.policy_governance_events),
            }
            staged.idempotency[key] = (fingerprint, result)
            return result

        return self._run_command(
            principal, "cancel", idempotency_key, fingerprint, mutate
        )


    # -------------------------------------------------------------- worker

    def process_next_policy_job(
        self, worker_id: str = "s08-activator", now: int | None = None
    ) -> dict[str, Any]:
        """Claim and execute one durable policy job (validation or activation)."""
        claim = self._claim_policy_job(worker_id, now)
        if isinstance(claim, dict):
            return claim
        job, observed_now, snapshot = claim
        # Run the job body outside the lock: validation/activation compute
        # from the claimed snapshot (their own deep copy), then re-acquire
        # the lock for the guarded result write.  Holding the lock across
        # the long computation queued every read query behind the worker
        # and pushed poll requests past the HTTP response contract.
        if job["kind"] == "validation":
            return self._run_validation_job(job, observed_now, snapshot)
        return self._run_activation_job(job, observed_now, snapshot)

    def _claim_policy_job(
        self, worker_id: str, now: int | None = None
    ) -> dict[str, Any] | tuple[dict[str, Any], int, SQLiteTargetStore]:
        """Claim one ready job under the lock and persist the claimed
        attempt identity (worker/fence/attempt/lease) in the same short
        transaction, so a crash after claim leaves a first durable attempt.

        Returns ``(job, observed_now, snapshot)`` with ``snapshot`` the
        immutable claim-time store the compute phase must use -- never a
        later copy of the mutable store -- or a ``{"status": ...}`` dict for
        authority-unavailable / idle / contention outcomes."""
        with self._lock:
            if not self.audit_available or not self.storage_available:
                return {
                    "status": "failed",
                    "reason_code": "POLICY_AUTHORITY_UNAVAILABLE",
                }
            self._store.reload()
            observed_now = int(self._clock() if now is None else now)
            for _ in range(2):
                staged = copy.deepcopy(self._store)
                selected = next(
                    (
                        job
                        for job in staged.policy_jobs
                        if job.get("status") in {"queued", "leased"}
                        and not (
                            job.get("status") == "leased"
                            and int(job.get("lease_until", 0)) > observed_now
                        )
                        and (
                            job.get("kind") != "activation"
                            or int(job.get("activation_at", 0)) <= observed_now
                        )
                    ),
                    None,
                )
                if selected is None:
                    return {"status": "idle", "reason_code": "NO_READY_POLICY_JOB"}
                selected["status"] = "leased"
                selected["worker_id"] = worker_id
                selected["fence"] = int(selected.get("fence", 0)) + 1
                selected["attempt_no"] = int(selected.get("attempt_no", 0)) + 1
                selected["lease_until"] = observed_now + 30
                staged.policy_attempts.append(
                    self._attempt_record(
                        selected, status="running", started_at=observed_now
                    )
                )
                try:
                    staged.persist()
                except StaleStoreRevision:
                    self._store.reload()
                    continue
                self._store = staged
                # ``staged`` is this thread's private snapshot: every other
                # mutation replaces the store object instead of editing it in
                # place, so the compute phase needs a snapshot that is a
                # *different* object from the live store.  Return an
                # independent claim-time deep copy: a later reload of
                # ``self._store`` (a public query between claim and compute)
                # must never mutate what the compute phase reads.
                return copy.deepcopy(selected), observed_now, copy.deepcopy(staged)
            return {
                "status": "blocked",
                "reason_code": "POLICY_JOB_CLAIM_CONTENTION",
            }

    def _run_validation_job(
        self, job: dict[str, Any], now: int, snapshot: SQLiteTargetStore
    ) -> dict[str, Any]:
        candidate_id = job["candidate_id"]
        state: dict[str, Any] | None = None
        try:
            owner = copy.deepcopy(snapshot)
            state = self._require_candidate_state(owner, candidate_id)
            if state["status"] != "candidate":
                raise PolicyInvalidTransition("candidate is no longer pending validation")
            bundle, outcome = self._validate_candidate(owner, state)
            with self._lock:
                self._before_write("s08.validation")
                # Authoritative store before any terminal classification: a
                # concurrent writer in another service must be visible here,
                # never replaced by a stale in-memory copy.
                self._store.reload()
                staged = copy.deepcopy(self._store)
                # Fresh trusted settlement time: the claim-time ``now`` is
                # started_at only.  A long computation must prove its lease
                # is still live at the write point, not at the claim point.
                settlement_now = self._trusted_time()
                # Exact ownership gate: the job row must still be leased by
                # this worker/fence/attempt under the owned lease.  A stale
                # worker (reclaimed, restarted or completed by another fence)
                # settles only its own discarded attempt and never touches
                # the job row or any domain fact.
                current_job = self._owned_job(staged, job, settlement_now)
                if current_job is None:
                    return self._settle_stale_attempt(staged, job, now)
                # The candidate must still be pending validation when the
                # result lands: a command issued while the computation ran
                # (cancel) must not be overwritten by a stale verdict.  The
                # job settles as discarded -- never diagnostic -- so a
                # cancelled candidate is not surfaced as a validation
                # failure.
                current = self._require_candidate_state(staged, candidate_id)
                if current["status"] != state["status"]:
                    return self._settle_discarded(
                        staged, job, now, settlement_now
                    )
                current_job["status"] = "complete"
                current_job.pop("lease_until", None)
                staged.policy_attempts.append(
                    self._attempt_record(
                        job,
                        status="complete",
                        started_at=now,
                        result={
                            "validation_bundle_id": bundle["validation_bundle_id"],
                            "validation_bundle_digest": bundle["digest"],
                            "outcome": outcome,
                        },
                    )
                )
                staged.policy_artifacts.append(bundle["artifact"])
                self._append_governance_event(
                    staged,
                    kind="validated" if outcome == "validated" else "rejected",
                    principal=PolicyPrincipal(
                        subject=self._operator_subject,
                        role="operator",
                        scope=job["scope"],
                        source_id="s08-policy-worker",
                    ),
                    reason_code=(
                        "S08_VALIDATION_PASSED"
                        if outcome == "validated"
                        else "S08_VALIDATION_REJECTED"
                    ),
                    details={
                        "candidate_id": candidate_id,
                        "validation_bundle_id": bundle["validation_bundle_id"],
                        "validation_bundle_digest": bundle["digest"],
                    },
                    trusted_time=settlement_now,
                )
                AuditOutboxOwner(staged).append_outbox(
                    {
                        "event_id": self._stable_id(
                            "outbox", f"s08:validation:{candidate_id}"
                        ),
                        "kind": "s08_validation_completed",
                        "scope": job["scope"],
                        "candidate_id": candidate_id,
                        "validation_bundle_id": bundle["validation_bundle_id"],
                        "outcome": outcome,
                        "status": "pending",
                    }
                )
                if not self._persist_worker(staged):
                    return {
                        "status": "retry",
                        "kind": "validation",
                        "candidate_id": candidate_id,
                    }
            return {
                "status": "complete",
                "kind": "validation",
                "candidate_id": candidate_id,
                "validation_bundle_id": bundle["validation_bundle_id"],
                "outcome": outcome,
                "governance_revision": len(staged.policy_governance_events),
            }
        except (
            PolicyInvalidTransition,
            PolicyUnavailable,
            PolicyConflict,
            KeyError,
            RuntimeError,
        ) as error:
            with self._lock:
                # Authoritative store before any terminal classification: a
                # stale in-memory copy must never decide that a cancelled
                # candidate's unchanged state is an applicable failure.
                self._store.reload()
                staged = copy.deepcopy(self._store)
                try:
                    settlement_now = self._trusted_time()
                except PolicyUnavailable:
                    return {
                        "status": "retry",
                        "kind": "validation",
                        "candidate_id": candidate_id,
                    }
                current_job = self._owned_job(staged, job, settlement_now)
                if current_job is None:
                    return self._settle_stale_attempt(staged, job, now)
                # The candidate must still be in the state the worker
                # validated against even when the compute phase raised: a
                # concurrent cancel invalidates the still-owned job, which
                # settles as discarded -- never a diagnostic lie.  Only an
                # owned, live compute failure whose claim-time candidate was
                # pending AND whose fresh candidate is still pending may
                # become diagnostic; a cancelled candidate that never changed
                # between claim and settlement (e.g. a reclaimed job) is not
                # an applicable failure either.
                try:
                    current = self._require_candidate_state(staged, candidate_id)
                except (PolicyNotFound, KeyError):
                    return self._settle_discarded(
                        staged, job, now, settlement_now
                    )
                if not (
                    state is not None
                    and state["status"] == "candidate"
                    and current["status"] == "candidate"
                ):
                    return self._settle_discarded(
                        staged, job, now, settlement_now
                    )
                reason_code = self._worker_reason_code("validation", error)
                current_job["status"] = "diagnostic"
                current_job["reason_code"] = reason_code
                staged.policy_attempts.append(
                    self._attempt_record(
                        job,
                        status="failed",
                        started_at=now,
                        result={"reason_code": reason_code},
                    )
                )
                if not self._persist_worker(staged):
                    return {
                        "status": "retry",
                        "kind": "validation",
                        "candidate_id": candidate_id,
                    }
            return {
                "status": "failed",
                "kind": "validation",
                "candidate_id": candidate_id,
                "reason_code": reason_code,
            }

    def _run_activation_job(
        self, job: dict[str, Any], now: int, snapshot: SQLiteTargetStore
    ) -> dict[str, Any]:
        candidate_id = job["candidate_id"]
        scope = job["scope"]
        state: dict[str, Any] | None = None
        claim_hold = False
        try:
            owner = copy.deepcopy(snapshot)
            state = self._require_candidate_state(owner, candidate_id)
            claim_hold = (
                self._activation_hold(owner, scope) is not None
                and not state.get("rollback")
            )
            if claim_hold:
                raise PolicyInvalidTransition("activation hold is in effect")
            if state["status"] not in {"approved", "scheduled"}:
                raise PolicyInvalidTransition(
                    "candidate is no longer approved at activation"
                )
            binding = self._artifact(owner, job["approval_binding_id"])
            if binding.get("schema_version") != APPROVAL_BINDING_SCHEMA:
                raise PolicyUnavailable("approval binding is not verifiable")
            if (
                binding.get("candidate_id") != candidate_id
                or binding.get("activation_time") != job.get("activation_at")
                or binding.get("recovery_release_id") != state.get("recovery_release_id")
            ):
                raise PolicyUnavailable("approval binding no longer matches the schedule")
            self._require_candidate_scope_valid_at(owner, candidate_id, now)
            self._require_current_approval_review(owner, state, scope, binding)
            validation = self._artifact(owner, state["validation_bundle_id"])
            if validation.get("status") != "validated":
                raise PolicyUnavailable("validation bundle is not validated")
            # Revalidate the candidate's own schedule revision: the job pins
            # the governance revision of the candidate's scheduled event, so
            # unrelated candidates may advance the ledger without disturbing
            # this activation, while a cancel/re-schedule of this candidate
            # is caught.
            scheduled_events = [
                event
                for event in owner.policy_governance_events
                if event.get("kind") == "scheduled"
                and event.get("candidate_id") == candidate_id
            ]
            if (
                not scheduled_events
                or scheduled_events[-1].get("revision")
                != job.get("expected_governance_revision")
            ):
                raise PolicyConflict(
                    "candidate schedule revision changed since scheduling"
                )
            candidate_manifest = self._verify_pinned_manifest(
                owner,
                state["manifest_id"],
                state["manifest_digest"],
            )
            self._verify_bound_evidence(
                owner,
                candidate_manifest,
                candidate_id=candidate_id,
                validation_bundle_id=state["validation_bundle_id"],
                validation_bundle_digest=state["validation_bundle_digest"],
                approval_binding_id=state["approval_binding_id"],
                approval_binding_digest=state["approval_binding_digest"],
                require_current_validator=True,
            )
            # Fully re-verify the recovery release from append-only facts
            # before any protected write: its manifest, every component,
            # validation bundle and approval binding must verify against the
            # Registry.  Missing or corrupt recovery content leaves every
            # protected effect at delta zero.
            recovery_release_id = state.get("recovery_release_id")
            if not isinstance(recovery_release_id, str) or not recovery_release_id:
                raise PolicyUnavailable("recovery release identity is missing")
            recovery_state = self._require_candidate_state(
                owner, recovery_release_id
            )
            if not all(
                isinstance(recovery_state.get(key), str) and recovery_state.get(key)
                for key in (
                    "manifest_id",
                    "manifest_digest",
                    "validation_bundle_id",
                    "validation_bundle_digest",
                    "approval_binding_id",
                    "approval_binding_digest",
                )
            ):
                raise PolicyUnavailable(
                    "recovery release evidence is incomplete"
                )
            recovery_manifest = self._verify_pinned_manifest(
                owner,
                recovery_state["manifest_id"],
                recovery_state["manifest_digest"],
            )
            self._verify_bound_evidence(
                owner,
                recovery_manifest,
                candidate_id=recovery_release_id,
                validation_bundle_id=recovery_state["validation_bundle_id"],
                validation_bundle_digest=recovery_state[
                    "validation_bundle_digest"
                ],
                approval_binding_id=recovery_state["approval_binding_id"],
                approval_binding_digest=recovery_state[
                    "approval_binding_digest"
                ],
            )
            if not self.audit_available or not self.storage_available:
                raise PolicyUnavailable(
                    "required audit or storage authority is unavailable"
                )
            with self._lock:
                self._before_write("s08.activation")
                # Authoritative store before any terminal classification: a
                # concurrent writer in another service must be visible here,
                # never replaced by a stale in-memory copy.
                self._store.reload()
                staged = copy.deepcopy(self._store)
                # Fresh trusted settlement time: the claim-time ``now`` is
                # started_at only; the lease must still be live at the write
                # point after a long activation computation.
                settlement_now = self._trusted_time()
                # Exact ownership gate: the job row must still be leased by
                # this worker/fence/attempt under the owned lease.  A stale
                # worker (reclaimed, restarted or completed by another fence)
                # settles only its own discarded attempt and never touches
                # the job row or any domain fact.
                current_job = self._owned_job(staged, job, settlement_now)
                if current_job is None:
                    return self._settle_stale_attempt(staged, job, now)
                # The candidate must still be in the state the worker
                # validated against: a command issued while the computation
                # ran must not be overwritten by a stale activation.  The
                # job settles as discarded so a cancelled candidate is not
                # surfaced as an activation failure.
                current = self._require_candidate_state(staged, candidate_id)
                if (
                    current["status"] != state["status"]
                    or (
                        self._activation_hold(staged, scope) is not None
                        and not current.get("rollback")
                    )
                ):
                    return self._settle_discarded(
                        staged, job, now, settlement_now
                    )
                # The frozen scope/validity window is revalidated at the
                # trusted settlement time, not only at claim time: a
                # validity expiry crossed during compute while the lease is
                # still live must settle as discarded and leave the prior
                # active release/generation untouched.
                try:
                    self._require_candidate_scope_valid_at(
                        staged, candidate_id, settlement_now
                    )
                except PolicyInvalidTransition:
                    return self._settle_discarded(
                        staged, job, now, settlement_now
                    )
                current_job["status"] = "complete"
                current_job.pop("lease_until", None)
                prior = self._fold_active_projection(
                    staged.policy_governance_events, scope
                )
                generation = (
                    int(prior["active_generation"]) + 1 if prior is not None else 1
                )
                # S09: the activation-time final impact is computed inside
                # the settlement transaction and must stay inside the
                # approved envelope.  A final expansion outside the envelope
                # stops activation with zero protected delta; unprovable
                # completeness rejects activation and establishes the
                # corresponding Policy Safety Hold.
                final_impact_manifest = None
                final_impact_event_id = None
                # P-1: a non-bootstrap activation always settles a final
                # impact inside the approved envelope.  A legacy approval
                # binding without the bound envelope can never activate an
                # arbitrary changed candidate; only the internal bootstrap
                # transaction (which never passes through this worker)
                # carries the pre-S09 baseline.
                envelope = binding.get("impact_envelope")
                if not isinstance(envelope, dict):
                    raise PolicyInvalidTransition(
                        "approval binding carries no impact envelope"
                    )
                preview_manifest_id = binding.get("preview_manifest_id")
                if not isinstance(preview_manifest_id, str) or not preview_manifest_id:
                    raise PolicyInvalidTransition(
                        "approval envelope has no bound preview manifest"
                    )
                preview = self._preview_manifest(staged, preview_manifest_id)
                if (
                    str(preview.get("candidate", {}).get("candidate_id"))
                    != candidate_id
                ):
                    raise PolicyInvalidTransition(
                        "bound preview does not belong to the candidate"
                    )
                final_impact_manifest = build_impact_manifest(
                    self._impact_manifest_request(
                        staged,
                        phase="final",
                        candidate=state,
                        generation=generation,
                        envelope=envelope,
                    )
                )
                self._verify_final_impact_within_envelope(
                    envelope=envelope,
                    preview=preview,
                    final=final_impact_manifest,
                )
                final_event = self._append_governance_event(
                    staged,
                    kind="impact_finalized",
                    principal=PolicyPrincipal(
                        subject=self._operator_subject,
                        role="operator",
                        scope=scope,
                        source_id="s08-policy-worker",
                    ),
                    reason_code="S09_IMPACT_FINALIZED",
                    details={
                        "candidate_id": candidate_id,
                        "manifest_id": final_impact_manifest["manifest_id"],
                        "digest": final_impact_manifest["digest"],
                        "phase": final_impact_manifest["phase"],
                        "member_count": len(final_impact_manifest["members"]),
                        "partition_counts": {
                            name: info["count"]
                            for name, info in final_impact_manifest[
                                "partitions"
                            ].items()
                        },
                        "zero_hit_proof": (
                            final_impact_manifest["zero_hit_proof"] is not None
                        ),
                        "target_generation": generation,
                        "predecessor": final_impact_manifest["predecessor"],
                        "envelope_digest": envelope.get("digest"),
                        "manifest": final_impact_manifest,
                    },
                    trusted_time=settlement_now,
                )
                final_impact_event_id = final_event["event_id"]
                self._before_write("s09.activation.impact_finalized")
                activation_event = self._append_governance_event(
                    staged,
                    kind="activated",
                    principal=PolicyPrincipal(
                        subject=self._operator_subject,
                        role="operator",
                        scope=scope,
                        source_id="s08-policy-worker",
                    ),
                    reason_code="S08_ACTIVATED",
                    details={
                        "candidate_id": candidate_id,
                        "approval_binding_id": job["approval_binding_id"],
                        "validation_bundle_id": state["validation_bundle_id"],
                        "validation_bundle_digest": state["validation_bundle_digest"],
                        "manifest_id": state["manifest_id"],
                        "manifest_digest": state["manifest_digest"],
                        "recovery_release_id": state.get("recovery_release_id"),
                        "active_generation": generation,
                        "activation_event_id": None,
                        "bootstrap": False,
                        "final_impact_digest": (
                            final_impact_manifest["digest"]
                            if final_impact_manifest is not None
                            else None
                        ),
                        "final_impact_manifest_id": (
                            final_impact_manifest["manifest_id"]
                            if final_impact_manifest is not None
                            else None
                        ),
                        "final_impact_member_count": (
                            len(final_impact_manifest["members"])
                            if final_impact_manifest is not None
                            else None
                        ),
                        "final_impact_event_id": final_impact_event_id,
                    },
                    trusted_time=settlement_now,
                )
                activation_event["activation_event_id"] = activation_event["event_id"]
                activation_event["active_generation"] = generation
                activation_event_id = activation_event["event_id"]
                # The terminal attempt must record the exact result this
                # transaction publishes: finalize the activated event
                # identity and generation first, then persist them in the
                # complete attempt -- no placeholder nulls survive restart.
                staged.policy_attempts.append(
                    self._attempt_record(
                        job,
                        status="complete",
                        started_at=now,
                        result={
                            "approval_binding_id": job["approval_binding_id"],
                            "activation_event_id": activation_event_id,
                            "active_generation": generation,
                        },
                    )
                )
                if prior is not None:
                    self._append_governance_event(
                        staged,
                        kind="superseded",
                        principal=PolicyPrincipal(
                            subject=self._operator_subject,
                            role="operator",
                            scope=scope,
                            source_id="s08-policy-worker",
                        ),
                        reason_code="S08_SUPERSEDED",
                        details={
                            "candidate_id": prior["candidate_id"],
                            "successor_candidate_id": candidate_id,
                            "successor_activation_event_id": activation_event_id,
                            "active_generation": generation,
                        },
                        trusted_time=settlement_now,
                    )
                manifest = candidate_manifest
                staged.policy_active_projections[scope] = {
                    "schema_version": "s08-active-projection/1",
                    "scope": scope,
                    "active_generation": generation,
                    "activation_event_id": activation_event_id,
                    "candidate_id": candidate_id,
                    "manifest_id": manifest["manifest_id"],
                    "manifest_digest": manifest["digest"],
                    "approval_binding_id": job["approval_binding_id"],
                    "approval_binding_digest": content_digest(binding),
                    "validation_bundle_id": state["validation_bundle_id"],
                    "validation_bundle_digest": state["validation_bundle_digest"],
                    "recovery_release_id": state["recovery_release_id"],
                    "activated_at": settlement_now,
                    "bootstrap": False,
                    "components": manifest["components"],
                    "final_impact_digest": (
                        final_impact_manifest["digest"]
                        if final_impact_manifest is not None
                        else None
                    ),
                    "final_impact_manifest_id": (
                        final_impact_manifest["manifest_id"]
                        if final_impact_manifest is not None
                        else None
                    ),
                    "final_impact_member_count": (
                        len(final_impact_manifest["members"])
                        if final_impact_manifest is not None
                        else None
                    ),
                }
                reservation = staged.policy_schedule_reservations.get(job["reservation_id"])
                if reservation is None or reservation.get("status") != "pending":
                    raise PolicyUnavailable("schedule reservation is no longer pending")
                reservation["status"] = "completed"
                staged.policy_schedule_reservations[job["reservation_id"]] = reservation
                AuditOutboxOwner(staged).append_outbox(
                    {
                        "event_id": self._stable_id("outbox", activation_event_id),
                        "kind": "s08_activated",
                        "scope": scope,
                        "candidate_id": candidate_id,
                        "activation_event_id": activation_event_id,
                        "active_generation": generation,
                        "final_impact_digest": (
                            final_impact_manifest["digest"]
                            if final_impact_manifest is not None
                            else None
                        ),
                        "status": "pending",
                    }
                )
                if final_impact_manifest is not None:
                    self._before_write("s09.activation.impact_outbox")
                    AuditOutboxOwner(staged).append_outbox(
                        {
                            "event_id": self._stable_id(
                                "outbox",
                                f"{activation_event_id}:final-impact",
                            ),
                            "kind": "s09_impact_activated",
                            "scope": scope,
                            "candidate_id": candidate_id,
                            "activation_event_id": activation_event_id,
                            "active_generation": generation,
                            "final_impact_digest": final_impact_manifest["digest"],
                            "final_impact_member_count": len(
                                final_impact_manifest["members"]
                            ),
                            "status": "pending",
                        }
                    )
                # Stable operation identity: the activation job id doubles as the
                # idempotency key, so response loss is reconciled by replaying
                # the original operation instead of guessing a fresh key.
                operation_key = self._idempotency_key(
                    PolicyPrincipal(
                        subject=self._operator_subject,
                        role="operator",
                        scope=scope,
                        source_id="s08-policy-worker",
                    ),
                    "activate",
                    job["policy_job_id"],
                )
                activation_fingerprint = self._fingerprint(
                    "activate",
                    candidate_id,
                    job["approval_binding_id"],
                    job.get("activation_at"),
                )
                staged.idempotency[operation_key] = (
                    activation_fingerprint,
                    {
                        "status": "accepted",
                        "activation_event_id": activation_event_id,
                        "active_generation": generation,
                        "candidate_id": candidate_id,
                        "final_impact_digest": (
                            final_impact_manifest["digest"]
                            if final_impact_manifest is not None
                            else None
                        ),
                        "final_impact_member_count": (
                            len(final_impact_manifest["members"])
                            if final_impact_manifest is not None
                            else None
                        ),
                    },
                )
                self._append_audit(
                    staged,
                    action="activation",
                    principal=PolicyPrincipal(
                        subject=self._operator_subject,
                        role="operator",
                        scope=scope,
                        source_id="s08-policy-worker",
                    ),
                    result="accepted",
                    reason_code="S08_ACTIVATED",
                    details={
                        "candidate_id": candidate_id,
                        "activation_event_id": activation_event_id,
                        "active_generation": generation,
                        "operation_key": operation_key,
                    },
                )
                if not self._persist_worker(staged):
                    return {
                        "status": "retry",
                        "kind": "activation",
                        "candidate_id": candidate_id,
                    }
            return {
                "status": "complete",
                "kind": "activation",
                "candidate_id": candidate_id,
                "activation_event_id": activation_event_id,
                "active_generation": generation,
                "governance_revision": len(staged.policy_governance_events),
            }
        except (
            PolicyInvalidTransition,
            PolicyUnavailable,
            PolicyConflict,
            ImpactUnprovable,
            KeyError,
            ValueError,
            RuntimeError,
        ) as error:
            with self._lock:
                # Authoritative store before any terminal classification: a
                # stale in-memory copy must never decide that a cancelled or
                # held candidate's unchanged state is an applicable failure.
                self._store.reload()
                staged = copy.deepcopy(self._store)
                try:
                    settlement_now = self._trusted_time()
                except PolicyUnavailable:
                    return {
                        "status": "retry",
                        "kind": "activation",
                        "candidate_id": candidate_id,
                    }
                current_job = self._owned_job(staged, job, settlement_now)
                if current_job is None:
                    return self._settle_stale_attempt(staged, job, now)
                # The candidate must still be in the state the worker
                # validated against even when the compute phase raised: a
                # concurrent cancel or hold invalidates the still-owned job,
                # which settles as discarded -- never a diagnostic lie and
                # never a fake activation failure.  Only an owned, live
                # compute failure whose claim-time state was approved or
                # scheduled with no hold AND whose fresh state/hold still
                # apply may become diagnostic; an already-cancelled or
                # already-held candidate that never changed between claim
                # and settlement (e.g. a reclaimed job) is not applicable
                # either.
                try:
                    current = self._require_candidate_state(staged, candidate_id)
                except (PolicyNotFound, KeyError):
                    return self._settle_discarded(
                        staged, job, now, settlement_now
                    )
                claim_applicable = (
                    state is not None
                    and state["status"] in {"approved", "scheduled"}
                    and not claim_hold
                )
                try:
                    fresh_hold = (
                        self._activation_hold(staged, scope) is not None
                        and not current.get("rollback")
                    )
                except KeyError:
                    # Corrupt hold evidence at settlement: fail closed --
                    # the worker must not classify any diagnostic on
                    # unverifiable state, so the attempt settles discarded.
                    fresh_hold = True
                fresh_applicable = (
                    current["status"] in {"approved", "scheduled"}
                    and not fresh_hold
                )
                if not (claim_applicable and fresh_applicable):
                    return self._settle_discarded(
                        staged, job, now, settlement_now
                    )
                # The frozen scope/validity window is revalidated at the
                # trusted settlement time on the failure path too: a compute
                # exception landing after valid_to crossed (lease still
                # live, state/hold unchanged) must settle as discarded --
                # never a diagnostic -- because the activation cannot
                # legally land at the settlement time.
                try:
                    self._require_candidate_scope_valid_at(
                        staged, candidate_id, settlement_now
                    )
                except PolicyInvalidTransition:
                    return self._settle_discarded(
                        staged, job, now, settlement_now
                    )
                except PolicyUnavailable as scope_error:
                    error = scope_error
                reason_code = self._worker_reason_code("activation", error)
                current_job["status"] = "diagnostic"
                current_job["reason_code"] = reason_code
                staged.policy_attempts.append(
                    self._attempt_record(
                        job,
                        status="failed",
                        started_at=now,
                        result={"reason_code": reason_code},
                    )
                )
                if isinstance(error, ImpactUnprovable):
                    # Unprovable impact completeness rejects activation and
                    # establishes the corresponding scoped Policy Safety
                    # Hold in the same settlement transaction: no partial
                    # activation, no silent diagnostic.
                    self._impose_hold(
                        staged,
                        principal=PolicyPrincipal(
                            subject=self._operator_subject,
                            role="operator",
                            scope=scope,
                            source_id="s08-policy-worker",
                        ),
                        reason_code=f"IMPACT_UNPROVABLE_{error.reason_code}",
                        hold_scope=str(error.hold_scope or scope),
                    )
                # A failed activation must not leave the schedule
                # reservation pending forever: the scope may only re-open
                # through a fresh preview/approval/schedule pipeline.
                reservation = staged.policy_schedule_reservations.get(
                    job.get("reservation_id")
                )
                if (
                    isinstance(reservation, dict)
                    and reservation.get("status") == "pending"
                ):
                    reservation["status"] = "cancelled"
                if not self._persist_worker(staged):
                    return {
                        "status": "retry",
                        "kind": "activation",
                        "candidate_id": candidate_id,
                    }
            return {
                "status": "failed",
                "kind": "activation",
                "candidate_id": candidate_id,
                "reason_code": reason_code,
            }

    # ------------------------------------------------------------ bootstrap

    def bootstrap_once(self) -> dict[str, Any]:
        """One-time, idempotent migration: import, validate, approve and
        activate the server-owned legacy baseline as the bootstrap release.

        Once a bootstrap activation exists, restart only reads the Registry
        and Ledger; the source files are never a runtime fallback."""
        with self._lock:
            if not self.audit_available:
                return {
                    "status": "blocked",
                    "reason_code": "AUDIT_UNAVAILABLE",
                }
            if not self.storage_available:
                return {
                    "status": "blocked",
                    "reason_code": "STORAGE_UNAVAILABLE",
                }
            self._store.reload()
            if any(
                event.get("kind") == "activated"
                and event.get("bootstrap")
                for event in self._store.policy_governance_events
            ):
                folded = self._fold_active_projection(
                    self._store.policy_governance_events, S08_SCOPE
                )
                return {
                    "status": "already_active",
                    "active_generation": (
                        folded["active_generation"] if folded is not None else 1
                    ),
                }
            rules_path = self._source_rules_path
            kb_path = self._source_kb_path
            if rules_path is None or not rules_path.is_file():
                return {"status": "blocked", "reason_code": "SOURCE_BUNDLE_MISSING"}
            if kb_path is None or not kb_path.is_file():
                return {"status": "blocked", "reason_code": "KNOWLEDGE_BUNDLE_MISSING"}
            try:
                result = self._bootstrap_once_transaction()
            except (
                ValueError,
                yaml.YAMLError,
                json.JSONDecodeError,
                OSError,
                PolicyInvalidTransition,
                PolicyUnavailable,
            ) as error:
                return {"status": "blocked", "reason_code": str(error)}
            return result

    def _bootstrap_once_transaction(self) -> dict[str, Any]:
        rules_bytes = self._source_rules_path.read_bytes()
        kb_bytes = self._source_kb_path.read_bytes()
        rules_digest = raw_digest(rules_bytes)
        kb_digest = raw_digest(kb_bytes)
        cfg = _load_rules_bytes(rules_bytes)
        knowledge = _load_knowledge_bytes(kb_bytes)
        release = TargetRelease.compile(cfg, rules_digest, knowledge=knowledge)
        staged = copy.deepcopy(self._store)
        mapping_ledger = self._build_mapping_ledger(rules_bytes, kb_bytes, release)
        ledger_artifact = self._stage_json_artifact(
            staged, type="mapping_ledger", content=mapping_ledger["material"]
        )
        mapping_ledger_id = ledger_artifact["id"]
        source_artifacts = [
            self._stage_raw_artifact(
                staged, kind="check_policy", content=rules_bytes, digest=rules_digest
            ),
            self._stage_raw_artifact(
                staged, kind="entity_knowledge", content=kb_bytes, digest=kb_digest
            ),
        ]
        component_ids: list[dict[str, str]] = []
        for entry in release.component_artifacts():
            component_ids.append(self._stage_json_artifact(staged, **entry))
        checker_component = self._stage_json_artifact(
            staged, type="checker", content=release.to_artifact()
        )
        component_ids.append(checker_component)
        component_ids.extend(
            {
                "type": artifact["kind"],
                "id": artifact["artifact_id"],
                "digest": artifact["content_sha256"],
            }
            for artifact in source_artifacts
        )
        admin = PolicyPrincipal(
            subject=self._migration_admin_subject,
            role="admin",
            scope=S08_SCOPE,
            source_id="s08-bootstrap",
        )
        approver = PolicyPrincipal(
            subject=self._approver_subject,
            role="approver",
            scope=S08_SCOPE,
            source_id="s08-bootstrap",
        )
        draft_id = self._stable_id(
            "draft", f"{S08_SCOPE}:bootstrap:{rules_digest}"
        )
        draft = {
            "draft_id": draft_id,
            "schema_version": "s08-draft/1",
            "scope": S08_SCOPE,
            "status": "draft",
            "bootstrap": True,
            "created_by": admin.subject,
            "created_at": self._trusted_time(),
            "revision": 1,
            "source_bundle_id": SOURCE_BUNDLE_ID,
            "source_sha256": rules_digest,
            "knowledge_sha256": kb_digest,
            "mapping_ledger_id": mapping_ledger_id,
            "mapping_ledger_digest": mapping_ledger["digest"],
            "artifact_ids": [artifact["artifact_id"] for artifact in source_artifacts],
            "components": component_ids,
            "metadata": {
                "scope": S08_SCOPE,
                "validity": {"valid_from": "2000-01-01T00:00:00Z"},
                "source": SOURCE_BUNDLE_ID,
                "reason": _BOOTSTRAP_REASON,
            },
            "candidate_id": None,
        }
        staged.policy_drafts[draft_id] = draft
        self._append_governance_event(
            staged,
            kind="imported",
            principal=admin,
            reason_code="S08_BOOTSTRAP_IMPORTED",
            details={
                "draft_id": draft_id,
                "source_bundle_id": SOURCE_BUNDLE_ID,
                "source_sha256": rules_digest,
                "knowledge_sha256": kb_digest,
                "mapping_ledger_id": mapping_ledger_id,
                "mapping_ledger_digest": mapping_ledger["digest"],
                "importer_version": IMPORTER_VERSION,
                "payload": {"bootstrap": True},
            },
        )
        components = copy.deepcopy(component_ids)
        manifest_material = {
            "schema_version": MANIFEST_SCHEMA,
            "scope": S08_SCOPE,
            "components": components,
            "compatibility": {
                "checker_build": _TARGET_CHECKER_BUILD,
                "input_contract_schema": "s01-evidence-snapshot/1",
                "evidence_readiness_policy": _READINESS_POLICY_ID,
            },
        }
        manifest_digest = content_digest(manifest_material)
        manifest_id = f"manifest_sha256_{manifest_digest}"
        staged.policy_manifests.append(
            {"manifest_id": manifest_id, "digest": manifest_digest, **manifest_material}
        )
        candidate_id = self._stable_id("candidate", f"{draft_id}:{manifest_digest}")
        draft["candidate_id"] = candidate_id
        staged.policy_drafts[draft_id] = draft
        self._append_governance_event(
            staged,
            kind="candidate_frozen",
            principal=admin,
            reason_code="S08_BOOTSTRAP_CANDIDATE_FROZEN",
            details={
                "candidate_id": candidate_id,
                "draft_id": draft_id,
                "manifest_id": manifest_id,
                "manifest_digest": manifest_digest,
                "components": components,
                "metadata": copy.deepcopy(draft["metadata"]),
                "mapping_ledger_id": mapping_ledger_id,
                "mapping_ledger_digest": mapping_ledger["digest"],
                "source_sha256": rules_digest,
                "knowledge_sha256": kb_digest,
            },
        )
        candidate_state = {
            "candidate_id": candidate_id,
            "manifest_id": manifest_id,
            "manifest_digest": manifest_digest,
        }
        bundle, outcome = self._validate_candidate(staged, candidate_state)
        if outcome != "validated":
            raise PolicyInvalidTransition("bootstrap validation failed")
        staged.policy_artifacts.append(bundle["artifact"])
        self._append_governance_event(
            staged,
            kind="validated",
            principal=admin,
            reason_code="S08_BOOTSTRAP_VALIDATED",
            details={
                "candidate_id": candidate_id,
                "validation_bundle_id": bundle["validation_bundle_id"],
                "validation_bundle_digest": bundle["digest"],
            },
        )
        self._append_governance_event(
            staged,
            kind="in_review",
            principal=admin,
            reason_code="S08_BOOTSTRAP_IN_REVIEW",
            details={
                "candidate_id": candidate_id,
                "validation_bundle_id": bundle["validation_bundle_id"],
                "validation_bundle_digest": bundle["digest"],
            },
        )
        binding_material = {
            "schema_version": APPROVAL_BINDING_SCHEMA,
            "candidate_id": candidate_id,
            "candidate_digest": manifest_digest,
            "validation_bundle_id": bundle["validation_bundle_id"],
            "validation_bundle_digest": bundle["digest"],
            "diff": {
                "schema_version": "s08-machine-diff/1",
                "scope": "legacy-vs-bootstrap",
                "behavior_delta": "none",
                "changes": [],
            },
            "scope": S08_SCOPE,
            "activation_time": self._trusted_time(),
            "recovery_release_id": candidate_id,
            "approved_by": approver.subject,
        }
        binding_digest = content_digest(binding_material)
        approval_binding_id = f"approval_sha256_{binding_digest}"
        staged.policy_artifacts.append(
            {
                "artifact_id": approval_binding_id,
                "schema_version": ARTIFACT_SCHEMA,
                "kind": "approval_binding",
                "content_sha256": binding_digest,
                "content_bytes": len(canonical_bytes(binding_material)),
                "canonical_json": canonical_bytes(binding_material).decode("utf-8"),
                "raw_hex": None,
                "importer_version": None,
            }
        )
        self._append_governance_event(
            staged,
            kind="approved",
            principal=approver,
            reason_code="S08_BOOTSTRAP_APPROVED",
            details={
                "candidate_id": candidate_id,
                "approval_binding_id": approval_binding_id,
                "approval_binding_digest": binding_digest,
                "validation_bundle_id": bundle["validation_bundle_id"],
                "validation_bundle_digest": bundle["digest"],
                "activation_time": self._trusted_time(),
                "recovery_release_id": candidate_id,
            },
        )
        reservation_id = self._stable_id(
            "reservation", f"{approval_binding_id}:bootstrap"
        )
        staged.policy_schedule_reservations[reservation_id] = {
            "reservation_id": reservation_id,
            "schema_version": RESERVATION_SCHEMA,
            "scope": S08_SCOPE,
            "approval_binding_id": approval_binding_id,
            "candidate_id": candidate_id,
            "activation_at": self._trusted_time(),
            "status": "completed",
            "created_by": admin.subject,
            "created_at": self._trusted_time(),
        }
        self._append_governance_event(
            staged,
            kind="scheduled",
            principal=admin,
            reason_code="S08_BOOTSTRAP_SCHEDULED",
            details={
                "candidate_id": candidate_id,
                "approval_binding_id": approval_binding_id,
                "reservation_id": reservation_id,
                "activation_at": self._trusted_time(),
                "scheduled_at": self._trusted_time(),
            },
        )
        activation_event = self._append_governance_event(
            staged,
            kind="activated",
            principal=PolicyPrincipal(
                subject=self._operator_subject,
                role="operator",
                scope=S08_SCOPE,
                source_id="s08-bootstrap",
            ),
            reason_code="S08_BOOTSTRAP_ACTIVATED",
            details={
                "candidate_id": candidate_id,
                "approval_binding_id": approval_binding_id,
                "validation_bundle_id": bundle["validation_bundle_id"],
                "validation_bundle_digest": bundle["digest"],
                "manifest_id": manifest_id,
                "manifest_digest": manifest_digest,
                "recovery_release_id": candidate_id,
                "active_generation": 1,
                "activation_event_id": None,
                "bootstrap": True,
                "payload": {"bootstrap": True},
            },
        )
        activation_event["activation_event_id"] = activation_event["event_id"]
        activation_event["active_generation"] = 1
        staged.policy_active_projections[S08_SCOPE] = {
            "schema_version": "s08-active-projection/1",
            "scope": S08_SCOPE,
            "active_generation": 1,
            "activation_event_id": activation_event["event_id"],
            "candidate_id": candidate_id,
            "manifest_id": manifest_id,
            "manifest_digest": manifest_digest,
            "approval_binding_id": approval_binding_id,
            "approval_binding_digest": binding_digest,
            "validation_bundle_id": bundle["validation_bundle_id"],
            "validation_bundle_digest": bundle["digest"],
            "recovery_release_id": candidate_id,
            "activated_at": self._trusted_time(),
            "bootstrap": True,
            "components": components,
        }
        AuditOutboxOwner(staged).append_outbox(
            {
                "event_id": self._stable_id("outbox", activation_event["event_id"]),
                "kind": "s08_bootstrap_activated",
                "scope": S08_SCOPE,
                "candidate_id": candidate_id,
                "activation_event_id": activation_event["event_id"],
                "active_generation": 1,
                "status": "pending",
            }
        )
        self._append_audit(
            staged,
            action="bootstrap_once",
            principal=admin,
            result="accepted",
            reason_code="S08_BOOTSTRAP_ACTIVATED",
            details={
                "candidate_id": candidate_id,
                "approval_binding_id": approval_binding_id,
                "activation_event_id": activation_event["event_id"],
            },
        )
        staged.persist()
        self._store = staged
        return {
            "status": "activated",
            "candidate_id": candidate_id,
            "manifest_id": manifest_id,
            "manifest_digest": manifest_digest,
            "activation_event_id": activation_event["event_id"],
            "active_generation": 1,
        }

    # ------------------------------------------------------------ importer

    def _build_mapping_ledger(
        self,
        rules_bytes: bytes,
        kb_bytes: bytes,
        release: TargetRelease,
    ) -> dict[str, Any]:
        """Structured traversal of every server-owned source item.

        Each ledger entry pins the real source location and the digest of
        the resolved value (never the pointer text), the target reference,
        an explicit classification, a stable reason and a result digest.
        Unknown or unmapped runtime items are recorded as ``unsupported``
        and therefore block validation: nothing is silently dropped.
        """
        rules_data = _load_yaml_source(rules_bytes)
        kb_data = _load_json_source(kb_bytes)
        items: list[dict[str, Any]] = []
        if not isinstance(rules_data, dict):
            raise ValueError("rules source must be an object")
        if not isinstance(kb_data, dict):
            raise ValueError("knowledge source must be an object")
        for pointer in _RULES_OPTION_POINTERS:
            key = pointer.lstrip("/")
            if key not in rules_data:
                continue
            value = rules_data[key]
            items.append(
                {
                    "source_ref": "rules",
                    "source_pointer": pointer,
                    "source_digest": content_digest(("option", pointer, value)),
                    "classification": "exact",
                    "target_ref": f"checker.options{pointer}",
                    "importer_version": IMPORTER_VERSION,
                    "reason": "declarative option mapped to canonical checker options",
                    "result_digest": content_digest(("checker_option", pointer, value)),
                }
            )
        aliases = rules_data.get("field_aliases")
        if isinstance(aliases, dict):
            for canonical, names in sorted(aliases.items()):
                names_tuple = tuple(names)
                items.append(
                    {
                        "source_ref": "rules",
                        "source_pointer": f"/field_aliases/{canonical}",
                        "source_digest": content_digest(
                            ("aliases", canonical, names_tuple)
                        ),
                        "classification": "exact",
                        "target_ref": f"checker.aliases/{canonical}",
                        "importer_version": IMPORTER_VERSION,
                        "reason": "field alias chain mapped to canonical aliases",
                        "result_digest": content_digest(
                            ("aliases", canonical, names_tuple)
                        ),
                    }
                )
        rules = rules_data.get("rules")
        if not isinstance(rules, list) or not rules:
            raise ValueError("rules source must contain a rules list")
        for index, rule in enumerate(rules):
            if not isinstance(rule, dict) or not rule.get("id"):
                raise ValueError("rules source contains an invalid rule")
            rule_id = str(rule["id"])
            items.append(
                {
                    "source_ref": "rules",
                    "source_pointer": f"/rules/{index}",
                    "source_digest": content_digest(("rule_source", index, rule)),
                    "classification": "exact",
                    "target_ref": f"checker.rules/{rule_id}",
                    "importer_version": IMPORTER_VERSION,
                    "reason": "declarative check mapped to canonical compiled rule",
                    "result_digest": content_digest(("rule_compiled", rule_id, rule)),
                }
            )
            # Every rule field is traversed at its JSON pointer.  Fields the
            # loader actually compiles bind their compiled target values;
            # unknown nested fields become explicit unsupported entries that
            # block validation instead of being silently dropped.
            rule_def = RuleDef.from_dict(rule, index=index)
            for field in sorted(_RULE_KNOWN_FIELDS):
                if field not in rule:
                    continue
                raw_value = rule[field]
                compiled_value = getattr(rule_def, field)
                items.append(
                    {
                        "source_ref": "rules",
                        "source_pointer": f"/rules/{index}/{field}",
                        "source_digest": content_digest(
                            ("rule_field_source", index, field, raw_value)
                        ),
                        "classification": "exact",
                        "target_ref": f"checker.rules/{rule_id}.{field}",
                        "importer_version": IMPORTER_VERSION,
                        "reason": (
                            "rule field compiled into the canonical target rule"
                        ),
                        "result_digest": content_digest(
                            ("rule_field_compiled", rule_id, field, compiled_value)
                        ),
                    }
                )
            for field in sorted(set(rule) - _RULE_KNOWN_FIELDS):
                raw_value = rule[field]
                items.append(
                    {
                        "source_ref": "rules",
                        "source_pointer": f"/rules/{index}/{field}",
                        "source_digest": content_digest(
                            ("rule_field_source", index, field, raw_value)
                        ),
                        "classification": "unsupported",
                        "target_ref": None,
                        "importer_version": IMPORTER_VERSION,
                        "reason": (
                            "unrecognized rule field; runtime effect unknown"
                        ),
                        "result_digest": content_digest(
                            ("rule_field_unknown", rule_id, field, raw_value)
                        ),
                    }
                )
        items.append(
            {
                "source_ref": "rules",
                "source_pointer": "/changelog",
                "source_digest": content_digest(("changelog", rules_data.get("changelog"))),
                "classification": "non_runtime_excluded",
                "target_ref": None,
                "importer_version": IMPORTER_VERSION,
                "reason": "documentation only; cannot affect runtime",
                "result_digest": content_digest(("changelog",)),
            }
        )
        for section in ("version", "description"):
            if section not in kb_data:
                continue
            value = kb_data[section]
            items.append(
                {
                    "source_ref": "knowledge",
                    "source_pointer": f"/{section}",
                    "source_digest": content_digest(("kb", section, value)),
                    "classification": "non_runtime_excluded",
                    "target_ref": None,
                    "importer_version": IMPORTER_VERSION,
                    "reason": "metadata only; cannot affect runtime",
                    "result_digest": content_digest(("kb", section, value)),
                }
            )
        if "graph" in kb_data:
            items.extend(_graph_mapping_items(kb_data["graph"], release))
        for section in ("address_aliases", "org_aliases", "plate_prefixes"):
            values = kb_data.get(section)
            if not isinstance(values, dict):
                raise ValueError(f"knowledge section {section} is invalid")
            for key in sorted(values):
                value = values[key]
                items.append(
                    {
                        "source_ref": "knowledge",
                        "source_pointer": f"/{section}/{key}",
                        "source_digest": content_digest(("kb_alias", section, key, value)),
                        "classification": "exact",
                        "target_ref": f"checker.knowledge/{section}/{key}",
                        "importer_version": IMPORTER_VERSION,
                        "reason": "entity alias mapped into canonical knowledge",
                        "result_digest": content_digest(("kb_alias", section, key, value)),
                    }
                )
        # Unknown top-level keys are explicit unsupported entries: validation
        # counts them and rejects the candidate instead of silently dropping
        # a possibly-runtime item.
        for key in sorted(set(rules_data) - _RULES_KNOWN_TOP_LEVEL):
            value = rules_data[key]
            items.append(
                {
                    "source_ref": "rules",
                    "source_pointer": f"/{key}",
                    "source_digest": content_digest(("unknown_rules_item", key, value)),
                    "classification": "unsupported",
                    "target_ref": None,
                    "importer_version": IMPORTER_VERSION,
                    "reason": "unrecognized top-level rules option; runtime effect unknown",
                    "result_digest": content_digest(("unknown_rules_item", key, value)),
                }
            )
        for key in sorted(set(kb_data) - _KB_KNOWN_SECTIONS):
            value = kb_data[key]
            items.append(
                {
                    "source_ref": "knowledge",
                    "source_pointer": f"/{key}",
                    "source_digest": content_digest(("unknown_kb_item", key, value)),
                    "classification": "unsupported",
                    "target_ref": None,
                    "importer_version": IMPORTER_VERSION,
                    "reason": "unrecognized knowledge section; runtime effect unknown",
                    "result_digest": content_digest(("unknown_kb_item", key, value)),
                }
            )
        material = {
            "schema_version": MAPPING_LEDGER_SCHEMA,
            "importer_version": IMPORTER_VERSION,
            "source_refs": {
                "rules_bundle_id": SOURCE_BUNDLE_ID,
                "rules_sha256": raw_digest(rules_bytes),
                "knowledge_sha256": raw_digest(kb_bytes),
            },
            "items": items,
        }
        digest = content_digest(material)
        return {"digest": digest, "material": material}

    # ------------------------------------------------------------- artifacts

    def _stage_raw_artifact(
        self,
        staged: SQLiteTargetStore,
        *,
        kind: str,
        content: bytes,
        digest: str,
    ) -> dict[str, Any]:
        artifact_id = f"artifact_sha256_{digest}"
        for existing in staged.policy_artifacts:
            if existing.get("artifact_id") == artifact_id:
                return existing
        artifact = {
            "artifact_id": artifact_id,
            "schema_version": ARTIFACT_SCHEMA,
            "kind": kind,
            "content_sha256": digest,
            "content_bytes": len(content),
            "canonical_json": None,
            "raw_hex": content.hex(),
            "importer_version": IMPORTER_VERSION,
        }
        staged.policy_artifacts.append(artifact)
        return artifact

    def _stage_json_artifact(
        self,
        staged: SQLiteTargetStore,
        *,
        type: str,
        content: Any,
    ) -> dict[str, str]:
        encoded = canonical_bytes(content)
        digest = hashlib.sha256(encoded).hexdigest()
        artifact_id = f"artifact_sha256_{digest}"
        for existing in staged.policy_artifacts:
            if existing.get("artifact_id") == artifact_id:
                return {
                    "type": type,
                    "id": artifact_id,
                    "digest": digest,
                }
        staged.policy_artifacts.append(
            {
                "artifact_id": artifact_id,
                "schema_version": ARTIFACT_SCHEMA,
                "kind": type,
                "content_sha256": digest,
                "content_bytes": len(encoded),
                "canonical_json": encoded.decode("utf-8"),
                "raw_hex": None,
                "importer_version": None,
            }
        )
        return {"type": type, "id": artifact_id, "digest": digest}

    @staticmethod
    def _activation_hold(
        owner: SQLiteTargetStore, scope: str
    ) -> dict[str, Any] | None:
        projection = PolicyGovernanceService._fold_active_projection(
            owner.policy_governance_events, scope
        )
        if projection is None:
            return None
        hold = projection.get("activation_hold")
        if isinstance(hold, dict):
            return hold
        union = projection.get("hold_union")
        if isinstance(union, list) and union:
            return {
                "event_id": union[0]["hold_id"],
                "reason_code": union[0]["reason_code"],
                "stopped_at": union[0]["imposed_at"],
                "stopped_by": union[0]["imposed_by"],
            }
        return None

    @staticmethod
    def _active_hold_union(
        events: list[dict[str, Any]], scope: str
    ) -> list[dict[str, Any]]:
        """The append-only Policy Safety Hold union: every ``hold_imposed``
        event still lacking a matching ``hold_released`` event.  Holds never
        auto-expire; time only triggers alerts."""
        imposed = [
            event
            for event in events
            if event.get("kind") == "hold_imposed"
            and event.get("scope") == scope
        ]
        released = {
            str(event.get("hold_id") or "")
            for event in events
            if event.get("kind") == "hold_released"
            and event.get("scope") == scope
            and str(event.get("hold_id") or "")
        }
        active = []
        for event in imposed:
            hold_id = str(event.get("hold_id") or event.get("event_id") or "")
            if not hold_id or hold_id in released:
                continue
            actor = event.get("actor") or {}
            active.append(
                {
                    "hold_id": hold_id,
                    "event_id": event.get("event_id"),
                    "reason_code": str(event.get("reason_code") or ""),
                    "scope": str(event.get("scope") or ""),
                    "hold_scope": str(event.get("hold_scope") or ""),
                    "imposed_by": str(actor.get("subject") or ""),
                    "imposed_at": event.get("trusted_time"),
                    "authority_revision": event.get("authority_revision"),
                    "evidence_digest": event.get("evidence_digest"),
                    "recovery_criterion_id": event.get("recovery_criterion_id"),
                    "recovery_criterion_digest": event.get(
                        "recovery_criterion_digest"
                    ),
                }
            )
        return active

    def _require_draft(
        self, owner: SQLiteTargetStore, draft_id: str
    ) -> dict[str, Any]:
        draft = owner.policy_drafts.get(draft_id)
        if draft is None:
            raise PolicyNotFound(draft_id)
        return draft

    def _require_candidate_state(
        self, owner: SQLiteTargetStore, candidate_id: str
    ) -> dict[str, Any]:
        state = self._candidate_state(owner.policy_governance_events, candidate_id)
        if state.get("status") is None:
            raise PolicyNotFound(candidate_id)
        return state

    # ------------------------------------------------------------ validation

    @staticmethod
    def _runtime_behavior_digest(release: TargetRelease) -> str:
        material = release.to_artifact()
        material.pop("release_id", None)
        material.pop("rules_digest", None)
        return content_digest(material)

    def _validate_candidate(
        self, owner: SQLiteTargetStore, state: dict[str, Any]
    ) -> tuple[dict[str, Any], str]:
        """Structure, refs/digests, mapping completeness, compatibility and
        protected-baseline checks.  Returns (bundle_material, outcome)."""
        manifest = self._manifest(owner, state["manifest_id"])
        if manifest.get("digest") != state["manifest_digest"]:
            raise PolicyUnavailable("candidate manifest digest does not match")
        checks: list[dict[str, Any]] = []
        component_digests = {
            item.get("type"): item.get("digest") for item in manifest["components"]
        }
        component_digest_problems: list[str] = []
        for item in manifest["components"]:
            artifact_id = item.get("id")
            digest = item.get("digest")
            rows = [
                row
                for row in owner.policy_artifacts
                if row.get("artifact_id") == artifact_id
            ]
            if len(rows) != 1 or rows[0].get("content_sha256") != digest:
                component_digest_problems.append(
                    f"component {item.get('type')} is missing or digest mismatch"
                )
                continue
            row = rows[0]
            if row.get("canonical_json"):
                try:
                    content = json.loads(row["canonical_json"])
                except (TypeError, json.JSONDecodeError):
                    content = None
                if content is None or content_digest(content) != digest:
                    component_digest_problems.append(
                        f"component {item.get('type')} content mismatch"
                    )
            elif not row.get("raw_hex") or raw_digest(bytes.fromhex(row["raw_hex"])) != digest:
                component_digest_problems.append(
                    f"component {item.get('type')} raw content mismatch"
                )
        checks.append(
            {
                "check_id": "component_digest",
                "outcome": "pass" if not component_digest_problems else "fail",
                "detail": (
                    "every manifest component is digest-bound"
                    if not component_digest_problems
                    else "; ".join(component_digest_problems)
                ),
            }
        )
        checks.append(
            {
                "check_id": "component_completeness",
                "outcome": (
                    "pass"
                    if set(component_digests)
                    >= {
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
                    }
                    else "fail"
                ),
                "detail": "complete component manifest",
            }
        )
        checker = self._artifact(owner, self._component_id(manifest, "checker"))
        release = None
        compatibility_problem: str | None = None
        protected_problem: str | None = None
        try:
            release = TargetRelease.from_artifact(checker)
        except ProtectedInvariantError as error:
            protected_problem = str(error)
        except ValueError as error:
            compatibility_problem = str(error)
        checks.append(
            {
                "check_id": "checker_compatibility",
                "outcome": "pass" if release is not None else "fail",
                "detail": (
                    "checker artifact materializes with schema/build compatibility"
                    if release is not None
                    else compatibility_problem or "checker artifact is incompatible"
                ),
            }
        )
        if release is not None:
            protected = set(release.public_manifest()["applicable_check_ids"]) >= (
                _PROTECTED_CHECK_IDS
            )
            # Protected invariants span checker and comparison policy: the
            # comparison/waiver policy component must reproduce the
            # compile-time waiver digest of the materialized checker.
            comparison_component = self._component_id(manifest, "comparison_policy")
            comparison_artifact = self._artifact(owner, comparison_component)
            comparison_matches = (
                content_digest(comparison_artifact)
                == release.waiver_policy_digest
            )
            protected = protected and comparison_matches
            checks.append(
                {
                    "check_id": "protected_baseline",
                    "outcome": (
                        "protected_fail"
                        if protected_problem is not None
                        else "pass" if protected else "fail"
                    ),
                    "detail": (
                        protected_problem
                        if protected_problem is not None
                        else (
                            "protected VIN/engine/identity invariants hold "
                            "across checker and comparison policy"
                            if protected
                            else "protected baseline or comparison policy mismatch"
                        )
                    ),
                }
            )
            normalizer_expected = release.normalizer_digest
            normalization_component = self._component_id(manifest, "normalization_policy")
            normalization_artifact = self._artifact(owner, normalization_component)
            checks.append(
                {
                    "check_id": "normalization_policy",
                    "outcome": (
                        "pass"
                        if content_digest(normalization_artifact)
                        == normalizer_expected
                        else "fail"
                    ),
                    "detail": "normalization policy digest matches compile-time digest",
                }
            )
        elif protected_problem is not None:
            checks.append(
                {
                    "check_id": "protected_baseline",
                    "outcome": "protected_fail",
                    "detail": protected_problem,
                }
            )
        if release is None:
            behavior_identity = False
            behavior_detail = "checker artifact is not materializable"
        else:
            try:
                active = self._fold_active_projection(
                    owner.policy_governance_events, S08_SCOPE
                )
                anchor_release = release
                if active is not None:
                    anchor_manifest = self._verify_pinned_manifest(
                        owner, active["manifest_id"], active["manifest_digest"]
                    )
                    anchor_release = TargetRelease.from_artifact(
                        self._artifact(
                            owner,
                            self._component_id(anchor_manifest, "checker"),
                        )
                    )
                behavior_identity = self._runtime_behavior_digest(
                    release
                ) == self._runtime_behavior_digest(anchor_release)
                behavior_detail = (
                    "runtime material matches the active anchor"
                    if behavior_identity
                    else "runtime material differs from the active anchor"
                )
            except (PolicyUnavailable, ValueError) as error:
                behavior_identity = False
                behavior_detail = str(error)
        checks.append(
            {
                "check_id": "runtime_behavior_identity",
                "outcome": "pass" if behavior_identity else "fail",
                "detail": behavior_detail,
            }
        )
        # Validation reads only the immutable candidate snapshot pinned at
        # freeze time; later draft edits can never influence it.
        frozen_events = [
            event
            for event in owner.policy_governance_events
            if event.get("kind") == "candidate_frozen"
            and event.get("candidate_id") == state.get("candidate_id")
        ]
        frozen = frozen_events[-1] if len(frozen_events) == 1 else None
        mapping_ledger_id = frozen.get("mapping_ledger_id") if frozen else None
        mapping_ledger_digest = (
            frozen.get("mapping_ledger_digest") if frozen else None
        )
        draft_metadata = frozen.get("metadata") if frozen else None
        ledger = (
            self._find_mapping_ledger(
                owner, mapping_ledger_id, mapping_ledger_digest
            )
            if mapping_ledger_id and mapping_ledger_digest
            else None
        )
        unsupported = (
            sum(
                item.get("classification") == "unsupported"
                for item in ledger["items"]
            )
            if ledger
            else 0
        )
        checks.append(
            {
                "check_id": "mapping_ledger",
                "outcome": (
                    "pass" if ledger is not None and unsupported == 0 else "fail"
                ),
                "detail": (
                    "complete mapping ledger without unsupported runtime items"
                    if ledger is not None and unsupported == 0
                    else "mapping ledger missing or contains unsupported items"
                ),
            }
        )
        if release is not None:
            checks.append(self._scope_validity_check(draft_metadata))
            checks.append(self._semantic_entity_safety_check(release))
            checks.append(self._operators_check(release))
            checks.append(self._input_contract_check(owner, manifest, release))
        else:
            checks.append(
                {
                    "check_id": "scope_validity",
                    "outcome": "fail",
                    "detail": "checker artifact is not materializable",
                }
            )
            checks.append(
                {
                    "check_id": "semantic_entity_safety",
                    "outcome": "fail",
                    "detail": "checker artifact is not materializable",
                }
            )
            checks.append(
                {
                    "check_id": "operators",
                    "outcome": "fail",
                    "detail": "checker artifact is not materializable",
                }
            )
            checks.append(
                {
                    "check_id": "input_contract",
                    "outcome": "fail",
                    "detail": "checker artifact is not materializable",
                }
            )
        try:
            (
                determinism,
                corpus_diff,
                corpus_manifest,
                raw_outcomes,
            ) = self._fresh_process_evidence(owner, release)
        except PolicyUnavailable as error:
            reason = str(error)
            determinism = {
                "runs": 0,
                "equal": False,
                "digest": None,
                "reason": reason,
            }
            corpus_diff = {
                "anchor": None,
                "applications_compared": 0,
                "applications_skipped": 0,
                "checks_equal": False,
                "selection_equal": False,
                "normalization_equal": False,
                "verdicts_equal": False,
                "route_equal": False,
                "corpus_digest": None,
                "equal": False,
                "reason": reason,
            }
            corpus_manifest = None
            raw_outcomes = None
        checks.append(
            {
                "check_id": "determinism",
                "outcome": "pass" if determinism["equal"] else "fail",
                "detail": (
                    "two fresh-process runs over the frozen corpus agree"
                    if determinism["equal"]
                    else determinism["reason"]
                ),
            }
        )
        checks.append(
            {
                "check_id": "corpus_zero_diff",
                "outcome": "pass" if corpus_diff["equal"] else "fail",
                "detail": (
                    "complete frozen-corpus zero behavior difference"
                    if corpus_diff["equal"]
                    else corpus_diff["reason"]
                ),
            }
        )
        if corpus_manifest is None:
            checks.append(
                {
                    "check_id": "corpus_bound",
                    "outcome": "fail",
                    "detail": "server-owned frozen corpus manifest is unavailable",
                }
            )
        else:
            checks.append(
                {
                    "check_id": "corpus_bound",
                    "outcome": (
                        "pass"
                        if corpus_manifest["count"] > 0 and corpus_manifest["digest"]
                        else "fail"
                    ),
                    "detail": (
                        f"frozen corpus {corpus_manifest['count']} items bound to "
                        f"{corpus_manifest['digest']}"
                        if corpus_manifest["count"] > 0 and corpus_manifest["digest"]
                        else "frozen corpus is not digest-bound"
                    ),
                }
            )
        failed = [
            check
            for check in checks
            if check["outcome"] in {"fail", "protected_fail"}
        ]
        outcome = "rejected" if failed else "validated"
        material = {
            "schema_version": VALIDATION_BUNDLE_SCHEMA,
            "candidate_id": state.get("candidate_id"),
            "manifest_id": manifest["manifest_id"],
            "manifest_digest": manifest["digest"],
            "validation_suite": VALIDATOR_SUITE,
            "validator_build": VALIDATOR_BUILD,
            "validator": {
                "suite": VALIDATOR_SUITE,
                "build": VALIDATOR_BUILD,
                "code_sha256": _VALIDATOR_CODE_DIGEST,
                "python": platform.python_version(),
                "machine": platform.machine(),
            },
            "inputs": {
                "component_digests": component_digests,
                "mapping_ledger_id": mapping_ledger_id,
                "mapping_ledger_digest": mapping_ledger_digest,
                "corpus": corpus_manifest,
            },
            "results": {
                "checks": checks,
                "failed_count": len(failed),
                "determinism": determinism,
                "corpus_diff": corpus_diff,
                "raw_outcomes": raw_outcomes,
            },
            "status": outcome,
        }
        digest = content_digest(material)
        return (
            {
                "validation_bundle_id": f"validation_sha256_{digest}",
                "digest": digest,
                "artifact": {
                    "artifact_id": f"validation_sha256_{digest}",
                    "schema_version": ARTIFACT_SCHEMA,
                    "kind": "validation_bundle",
                    "content_sha256": digest,
                    "content_bytes": len(canonical_bytes(material)),
                    "canonical_json": canonical_bytes(material).decode("utf-8"),
                    "raw_hex": None,
                    "importer_version": None,
                },
            },
            outcome,
        )

    def _require_candidate_scope_valid_at(
        self,
        owner: SQLiteTargetStore,
        candidate_id: str,
        at: int,
    ) -> None:
        frozen = [
            event
            for event in owner.policy_governance_events
            if event.get("kind") == "candidate_frozen"
            and event.get("candidate_id") == candidate_id
        ]
        if len(frozen) != 1:
            raise PolicyUnavailable("candidate frozen scope is unavailable")
        outcome = self._scope_validity_check(frozen[0].get("metadata"), at=at)
        if outcome["outcome"] != "pass":
            raise PolicyInvalidTransition(str(outcome["detail"]))

    def _scope_validity_check(
        self, metadata: dict[str, Any] | None, *, at: int | None = None
    ) -> dict[str, Any]:
        if not isinstance(metadata, dict):
            return {
                "check_id": "scope_validity",
                "outcome": "fail",
                "detail": "governance scope metadata is missing",
            }
        validity = metadata.get("validity")
        if not isinstance(validity, dict):
            return {
                "check_id": "scope_validity",
                "outcome": "fail",
                "detail": "governance validity window is missing",
            }
        now = self._trusted_time() if at is None else at

        def parse_iso(value: Any) -> int | None:
            if not isinstance(value, str) or not value:
                return None
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp())

        valid_from = parse_iso(validity.get("valid_from"))
        valid_to = parse_iso(validity.get("valid_to"))
        if valid_from is None or valid_to is not None and valid_to <= valid_from:
            return {
                "check_id": "scope_validity",
                "outcome": "fail",
                "detail": "governance validity window is invalid",
            }
        if valid_to is not None and valid_to < now:
            return {
                "check_id": "scope_validity",
                "outcome": "fail",
                "detail": "governance scope has expired",
            }
        if valid_from > now:
            return {
                "check_id": "scope_validity",
                "outcome": "fail",
                "detail": "governance scope is not yet valid",
            }
        return {
            "check_id": "scope_validity",
            "outcome": "pass",
            "detail": "governance scope is valid at trusted time",
        }

    def _semantic_entity_safety_check(self, release: TargetRelease) -> dict[str, Any]:
        """Semantic/entity safety: alias conflicts, alias cycles, critical
        fuzzy prohibition, and no executable/I-O/path/URL/credential
        content anywhere in the declarative release."""
        problems: list[str] = []
        knowledge_sections = {
            section: dict(values) for section, values in release.knowledge
        }
        for key, value in knowledge_sections.get("address_aliases", {}).items():
            try:
                _validate_address_alias(key, value)
            except ValueError as error:
                problems.append(str(error))
        alias_names: dict[str, str] = {}
        for canonical, names in release.aliases:
            for name in names:
                previous = alias_names.setdefault(name, canonical)
                if previous != canonical:
                    problems.append(
                        f"alias {name!r} is shared by {previous!r} and {canonical!r}"
                    )
        graph: dict[str, str] = {}
        for section, values in release.knowledge:
            for key, value in values:
                graph[str(key)] = str(value)
        nodes = set(graph) | {value for value in graph.values() if value}
        for node in sorted(nodes):
            if graph.get(node) == node:
                # identity mapping (surface == canonical) is a no-op, not a cycle
                continue
            visited: set[str] = set()
            current = node
            while current in graph and current not in visited:
                visited.add(current)
                current = graph[current]
                if current == node:
                    problems.append(f"alias cycle detected at {node!r}")
                    break
        suspicious = re.compile(
            r"https?://|ftp://|file://|/etc/|/usr/|/home/|/tmp/|[A-Za-z]:[\\/]|"
            r"BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|password\s*=|"
            r"api[_-]?key\s*=|secret\s*=|\$\(",
            re.IGNORECASE,
        )
        for canonical, names in release.aliases:
            for name in (*names, canonical):
                if suspicious.search(name):
                    problems.append(f"alias {name!r} contains executable/I-O content")
        for section, values in release.knowledge:
            for key, value in values:
                if suspicious.search(str(key)) or suspicious.search(str(value)):
                    problems.append(
                        f"knowledge {section}/{key} contains executable/I-O content"
                    )
        for rule in release.rules:
            if rule.rule_type == "fuzzy" and rule.field in {
                "vin",
                "engine_no",
                "id_number",
            }:
                problems.append(
                    f"rule {rule.rule_id} applies fuzzy matching to a critical identity"
                )
        return {
            "check_id": "semantic_entity_safety",
            "outcome": "pass" if not problems else "protected_fail",
            "detail": (
                "semantic/entity aliases are stable and conflict-free"
                if not problems
                else "; ".join(sorted(problems))
            ),
        }

    def _operators_check(self, release: TargetRelease) -> dict[str, Any]:
        allowed = {
            "exact",
            "fuzzy",
            "numeric_tolerance",
            "list_contains",
            "conditional_required",
        }
        unknown = sorted(
            {rule.rule_type for rule in release.rules if rule.rule_type not in allowed}
        )
        return {
            "check_id": "operators",
            "outcome": "pass" if not unknown else "protected_fail",
            "detail": (
                "all compiled rules use registered deterministic operators"
                if not unknown
                else f"unknown operators: {unknown}"
            ),
        }

    def _input_contract_check(
        self,
        owner: SQLiteTargetStore,
        manifest: dict[str, Any],
        release: TargetRelease,
    ) -> dict[str, Any]:
        """The manifest input-contract component must exactly match the
        checker's input semantic contract; any drift is a protected
        compatibility failure."""
        try:
            component = self._component_id(manifest, "input_contract")
            artifact = self._artifact(owner, component)
            expected = content_digest(release.input_contract())
            matches = content_digest(artifact) == expected
        except PolicyUnavailable:
            matches = False
        return {
            "check_id": "input_contract",
            "outcome": "pass" if matches else "fail",
            "detail": (
                "input contract component matches the checker input semantic contract"
                if matches
                else "input contract component drifted from the checker contract"
            ),
        }

    def _fresh_process_evidence(
        self, owner: SQLiteTargetStore, release: TargetRelease | None
    ) -> tuple[
        dict[str, Any], dict[str, Any], dict[str, Any] | None, dict[str, Any] | None
    ]:
        """Determinism (two fresh-process runs) and frozen-corpus zero
        behavior difference against the bootstrap anchor.  The third return
        value is the digest-bound server-owned corpus manifest; the fourth
        is the complete raw per-run outcomes bound into the validation
        bundle.  Missing or invalid corpora fail closed with
        PolicyUnavailable instead of producing vacuous evidence."""
        if release is None or self._corpus_root is None:
            return (
                {
                    "runs": 0,
                    "equal": False,
                    "digest": None,
                    "reason": "checker or frozen corpus is not configured",
                },
                {
                    "anchor": None,
                    "applications_compared": 0,
                    "applications_skipped": 0,
                    "checks_equal": False,
                    "selection_equal": False,
                    "normalization_equal": False,
                    "verdicts_equal": False,
                    "route_equal": False,
                    "corpus_digest": None,
                    "equal": False,
                    "reason": "checker or frozen corpus is not configured",
                },
                None,
                None,
            )
        corpus = self._load_corpus()
        corpus_manifest = self._corpus_manifest(corpus)
        corpus_digest = corpus_manifest["digest"]
        anchor_release = release
        active = self._fold_active_projection(
            owner.policy_governance_events, S08_SCOPE
        )
        if active is not None:
            try:
                anchor_checker = self._artifact(owner, self._component_id(
                    self._manifest(owner, active["manifest_id"]), "checker"
                ))
                anchor_release = TargetRelease.from_artifact(anchor_checker)
            except (PolicyUnavailable, ValueError):
                anchor_release = None
        if anchor_release is None:
            return (
                {
                    "runs": 0,
                    "equal": False,
                    "digest": None,
                    "reason": "bootstrap anchor is unavailable",
                },
                {
                    "anchor": None,
                    "applications_compared": 0,
                    "applications_skipped": 0,
                    "checks_equal": False,
                    "selection_equal": False,
                    "normalization_equal": False,
                    "verdicts_equal": False,
                    "route_equal": False,
                    "corpus_digest": corpus_digest,
                    "equal": False,
                    "reason": "bootstrap anchor is unavailable",
                },
                corpus_manifest,
                None,
            )
        # The three fresh-process passes are independent evidence runs; run
        # them concurrently so a cold bootstrap does not serialize three
        # interpreter starts on the startup path (evidence semantics are
        # unchanged: one anchor pass and two determinism passes).
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            anchor_future = pool.submit(self._run_fresh_process, anchor_release, corpus)
            candidate_future = pool.submit(self._run_fresh_process, release, corpus)
            again_future = pool.submit(self._run_fresh_process, release, corpus)
            anchor_outcomes = anchor_future.result()
            candidate_outcomes = candidate_future.result()
            candidate_again = again_future.result()
        if (
            anchor_outcomes is None
            or candidate_outcomes is None
            or candidate_again is None
            or not isinstance(candidate_outcomes.get("outcome_schema"), dict)
            or anchor_outcomes.get("outcome_schema")
            != candidate_outcomes.get("outcome_schema")
            or candidate_again.get("outcome_schema")
            != candidate_outcomes.get("outcome_schema")
        ):
            reason = "fresh-process checker run failed"
            return (
                {
                    "runs": 2,
                    "equal": False,
                    "digest": None,
                    "reason": reason,
                },
                {
                    "anchor": "bootstrap" if active is not None else "self",
                    "applications_compared": len(corpus),
                    "applications_skipped": 0,
                    "checks_equal": False,
                    "selection_equal": False,
                    "normalization_equal": False,
                    "verdicts_equal": False,
                    "route_equal": False,
                    "corpus_digest": corpus_digest,
                    "equal": False,
                    "reason": reason,
                },
                corpus_manifest,
                None,
            )
        determinism = {
            "runs": 2,
            "equal": candidate_outcomes["digest"] == candidate_again["digest"],
            "digest": candidate_outcomes["digest"],
            "reason": (
                ""
                if candidate_outcomes["digest"] == candidate_again["digest"]
                else "fresh-process runs disagree"
            ),
        }
        diff = self._compare_corpus(
            anchor_outcomes["outcomes"], candidate_outcomes["outcomes"]
        )
        corpus_diff = {
            "anchor": "bootstrap" if active is not None else "self",
            "applications_compared": diff["compared"],
            "applications_skipped": diff["skipped"],
            "checks_equal": diff["checks_equal"],
            "selection_equal": diff["selection_equal"],
            "normalization_equal": diff["normalization_equal"],
            "verdicts_equal": diff["verdicts_equal"],
            "route_equal": diff["route_equal"],
            "corpus_digest": corpus_digest,
            "equal": all(
                diff[key]
                for key in (
                    "checks_equal",
                    "selection_equal",
                    "normalization_equal",
                    "verdicts_equal",
                    "route_equal",
                )
            )
            and diff["skipped"] == 0,
            "reason": (
                ""
                if all(
                    diff[key]
                    for key in (
                        "checks_equal",
                        "selection_equal",
                        "normalization_equal",
                        "verdicts_equal",
                        "route_equal",
                    )
                )
                and diff["skipped"] == 0
                else "corpus behavior differs or fixtures are uncovered"
            ),
        }
        outcome_sets: dict[str, list[dict[str, Any]]] = {}
        runs: dict[str, dict[str, str]] = {}
        for name, result in (
            ("anchor", anchor_outcomes),
            ("candidate", candidate_outcomes),
            ("again", candidate_again),
        ):
            digest = result["digest"]
            outcome_sets.setdefault(digest, result["outcomes"])
            runs[name] = {"outcome_set_digest": digest}
        raw_outcomes = {
            "schema": candidate_outcomes["outcome_schema"],
            "runs": runs,
            "outcome_sets": outcome_sets,
        }
        return determinism, corpus_diff, corpus_manifest, raw_outcomes

    def _load_corpus(self) -> list[dict[str, Any]]:
        """Load the server-owned frozen corpus, failing closed on missing,
        non-directory, empty or invalid input.  An empty or unreadable
        corpus must never produce vacuous zero-diff evidence."""
        if self._corpus_root is None:
            raise PolicyUnavailable("frozen corpus is not configured")
        corpus_root = Path(self._corpus_root)
        if not corpus_root.is_dir():
            raise PolicyUnavailable("frozen corpus directory is unavailable")
        items: list[dict[str, Any]] = []
        for path in sorted(corpus_root.glob("*.json")):
            raw = path.read_bytes()
            try:
                fixture = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise PolicyUnavailable(
                    f"frozen corpus fixture is invalid: {path.name}"
                ) from None
            if not isinstance(fixture, dict):
                raise PolicyUnavailable(
                    f"frozen corpus fixture is not an object: {path.name}"
                )
            items.append(
                {
                    "name": path.name,
                    "sha256": raw_digest(raw),
                    "fixture": fixture,
                }
            )
        if not items:
            raise PolicyUnavailable("frozen corpus is empty")
        if len(items) > _MAX_CORPUS_ITEMS:
            raise PolicyUnavailable("frozen corpus exceeds the item limit")
        return items

    @staticmethod
    def _corpus_manifest(corpus: list[dict[str, Any]]) -> dict[str, Any]:
        digest = content_digest(
            [
                (str(item["name"]), item["sha256"])
                for item in sorted(corpus, key=lambda item: item["name"])
            ]
        )
        return {
            "track": "C-DEV-REG",
            "count": len(corpus),
            "digest": digest,
            "items": [
                {"name": str(item["name"]), "sha256": item["sha256"]}
                for item in sorted(corpus, key=lambda item: item["name"])
            ],
        }

    def _run_fresh_process(
        self, release: TargetRelease, corpus: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        payload = {
            "checker_artifact": release.to_artifact(),
            "corpus": [
                {
                    "item_id": str(item.get("name") or f"corpus-item-{index}"),
                    "fixture": item["fixture"],
                }
                for index, item in enumerate(corpus)
                if isinstance(item["fixture"], dict)
            ],
        }
        payload_bytes = json.dumps(payload).encode("utf-8")
        if len(payload_bytes) > _MAX_EVIDENCE_INPUT_BYTES:
            return None
        module_root = Path(__file__).resolve().parents[2]
        env = {
            "PYTHONDONTWRITEBYTECODE": "1",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
        # Stream stdout/stderr while the child runs and kill it the moment
        # either stream exceeds its cap: the OS buffers can never grow past
        # the declared limits before rejection.
        proc: subprocess.Popen[bytes] | None = None
        try:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "task4_consistency.controlled.s08_validate",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(module_root),
                env=env,
            )
        except OSError:
            return None
        assert proc.stdout is not None and proc.stderr is not None
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        counters = {"stdout": 0, "stderr": 0}
        stream_lock = threading.Lock()

        def _drain(stream: Any, target: list[bytes], counter: str) -> None:
            limit = (
                _MAX_SUBPROCESS_STDOUT_BYTES
                if counter == "stdout"
                else _MAX_SUBPROCESS_STDERR_BYTES
            )
            try:
                for chunk in iter(lambda: stream.read(65536), b""):
                    with stream_lock:
                        counters[counter] += len(chunk)
                        if counters[counter] > limit:
                            try:
                                proc.kill()
                            except OSError:
                                pass
                            return
                        target.append(chunk)
            finally:
                try:
                    stream.close()
                except OSError:
                    pass

        stdout_thread = threading.Thread(
            target=_drain,
            args=(proc.stdout, stdout_chunks, "stdout"),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_drain,
            args=(proc.stderr, stderr_chunks, "stderr"),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        try:
            proc.stdin.write(payload_bytes)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            stdout_thread.join(timeout=10)
            stderr_thread.join(timeout=10)
            return None
        stdout_thread.join(timeout=10)
        stderr_thread.join(timeout=10)
        if stdout_thread.is_alive() or stderr_thread.is_alive():
            try:
                proc.kill()
            except OSError:
                pass
            return None
        with stream_lock:
            stdout_overflow = (
                counters["stdout"] > _MAX_SUBPROCESS_STDOUT_BYTES
            )
            stderr_overflow = (
                counters["stderr"] > _MAX_SUBPROCESS_STDERR_BYTES
            )
        if proc.returncode != 0 or stdout_overflow or stderr_overflow:
            return None
        try:
            return json.loads(b"".join(stdout_chunks).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    @staticmethod
    def _compare_corpus(
        anchor: list[dict[str, Any]], candidate: list[dict[str, Any]]
    ) -> dict[str, Any]:
        compared = 0
        skipped = 0
        checks_equal = len(anchor) == len(candidate)
        selection_equal = True
        normalization_equal = True
        verdicts_equal = True
        route_equal = True
        for index in range(max(len(anchor), len(candidate))):
            left = anchor[index] if index < len(anchor) else None
            right = candidate[index] if index < len(candidate) else None
            if left is None or right is None:
                checks_equal = False
                continue
            if left.get("corpus_item_id") != right.get("corpus_item_id"):
                checks_equal = False
                continue
            if "skipped" in left or "skipped" in right:
                skipped += 1
                continue
            compared += 1
            checks_equal = (
                checks_equal
                and left["applicable"] == right["applicable"]
                and left["checks"] == right["checks"]
            )
            selection_equal = (
                selection_equal and left["selection"] == right["selection"]
            )
            normalization_equal = (
                normalization_equal and left["normalization"] == right["normalization"]
            )
            verdicts_equal = verdicts_equal and left["verdicts"] == right["verdicts"]
            route_equal = route_equal and left["route"] == right["route"]
        return {
            "compared": compared,
            "skipped": skipped,
            "checks_equal": checks_equal,
            "selection_equal": selection_equal,
            "normalization_equal": normalization_equal,
            "verdicts_equal": verdicts_equal,
            "route_equal": route_equal,
        }

    @staticmethod
    def _component_id(manifest: dict[str, Any], component_type: str) -> str:
        matches = [
            item
            for item in manifest["components"]
            if item.get("type") == component_type
        ]
        if len(matches) != 1:
            raise PolicyUnavailable(
                f"manifest component {component_type} is unavailable"
            )
        return str(matches[0]["id"])

    def _find_mapping_ledger(
        self,
        owner: SQLiteTargetStore,
        ledger_id: str,
        ledger_digest: str,
    ) -> dict[str, Any]:
        if ledger_id != f"artifact_sha256_{ledger_digest}":
            raise PolicyUnavailable("mapping ledger identity does not verify")
        content = self._artifact(owner, ledger_id)
        if (
            content.get("schema_version") != MAPPING_LEDGER_SCHEMA
            or content_digest(content) != ledger_digest
        ):
            raise PolicyUnavailable("mapping ledger content does not verify")
        return content

    # ------------------------------------------------------------ resolver

    @classmethod
    def _fold_active_projection(
        cls, events: list[dict[str, Any]], scope: str
    ) -> dict[str, Any] | None:
        """Rebuild the active projection purely from append-only governance
        facts.  The highest active generation wins; a stop event adds the
        activation hold; S09 Policy Safety Holds compose as an append-only
        union of imposed/released events.  The mutable projection table is
        only a rebuildable cache and can never override this fold."""
        activated = [
            event
            for event in events
            if event.get("kind") == "activated"
            and event.get("scope") == scope
        ]
        if not activated:
            return None
        latest = max(
            activated,
            key=lambda event: (
                int(event.get("active_generation", 0) or 0),
                int(event.get("revision", 0) or 0),
            ),
        )
        approval_binding_id = str(latest.get("approval_binding_id") or "")
        approval_binding_digest = approval_binding_id.removeprefix(
            "approval_sha256_"
        )
        holds = [
            event
            for event in events
            if event.get("kind") == "activation_stopped"
            and event.get("scope") == scope
        ]
        hold = None
        if holds:
            last_hold = holds[-1]
            hold = {
                "event_id": last_hold["event_id"],
                "reason_code": last_hold.get("reason_code"),
                "stopped_at": last_hold.get("trusted_time"),
                "stopped_by": last_hold.get("actor", {}).get("subject"),
            }
        hold_union = cls._active_hold_union(events, scope)
        final_impact_digest = latest.get("final_impact_digest")
        final_impact_manifest_id = latest.get("final_impact_manifest_id")
        final_impact_member_count = latest.get("final_impact_member_count")
        return {
            "schema_version": "s08-active-projection/1",
            "scope": scope,
            "active_generation": int(latest.get("active_generation", 0) or 0),
            "activation_event_id": str(latest.get("activation_event_id") or ""),
            "candidate_id": str(latest.get("candidate_id") or ""),
            "manifest_id": str(latest.get("manifest_id") or ""),
            "manifest_digest": str(latest.get("manifest_digest") or ""),
            "approval_binding_id": approval_binding_id,
            "approval_binding_digest": approval_binding_digest,
            "validation_bundle_id": str(latest.get("validation_bundle_id") or ""),
            "validation_bundle_digest": str(
                latest.get("validation_bundle_digest") or ""
            ),
            "recovery_release_id": str(latest.get("recovery_release_id") or ""),
            "activated_at": latest.get("trusted_time"),
            "bootstrap": bool(latest.get("bootstrap")),
            "activation_hold": hold,
            "hold_union": hold_union,
            "final_impact_digest": (
                str(final_impact_digest) if final_impact_digest else None
            ),
            "final_impact_manifest_id": (
                str(final_impact_manifest_id) if final_impact_manifest_id else None
            ),
            "final_impact_member_count": (
                int(final_impact_member_count)
                if isinstance(final_impact_member_count, int)
                and not isinstance(final_impact_member_count, bool)
                else None
            ),
            "components": None,
        }

    @staticmethod
    def _release_run_spec_pin(release: TargetRelease) -> dict[str, Any]:
        public = release.public_manifest()
        return {
            "baseline_release": copy.deepcopy(public),
            "release_id": public["release_id"],
            "release_digest": public["digest"],
            "checker_build": public["checker_build"],
            "limits": copy.deepcopy(public["limits"]),
            "applicable_check_ids": public["applicable_check_ids"],
            "applicable_check_count": public["applicable_check_count"],
        }

    def resolve_run_pin(
        self,
        scope: str,
        now: int,
        *,
        store: SQLiteTargetStore | None = None,
    ) -> dict[str, Any] | None:
        """Resolve one complete active governed release for the scope, or None
        when no governed activation exists (pre-cutover compatibility).

        The active generation is folded from the append-only Governance
        Ledger; the mutable projection table is only a rebuildable cache and
        is never read as authority here.  Every manifest component and the
        bound validation/approval evidence are fully re-verified against the
        Registry, and required audit/storage availability is part of the
        resolution contract."""
        if not self.audit_available or not self.storage_available:
            raise PolicyUnavailable(
                "required audit or storage trust is unavailable for resolution"
            )
        if isinstance(now, bool) or not isinstance(now, int) or now < 1:
            raise PolicyUnavailable("trusted resolution time is invalid")
        owner = store if store is not None else self._store
        if store is None:
            owner.reload()
        active = self._fold_active_projection(
            owner.policy_governance_events, scope
        )
        if active is None:
            return None
        try:
            self._require_candidate_scope_valid_at(
                owner, active["candidate_id"], now
            )
        except PolicyInvalidTransition as error:
            raise PolicyUnavailable(
                "active release scope is not valid at the requested time"
            ) from error
        manifest = self._verify_pinned_manifest(
            owner, active["manifest_id"], active["manifest_digest"]
        )
        self._verify_bound_evidence(
            owner,
            manifest,
            candidate_id=active["candidate_id"],
            validation_bundle_id=active["validation_bundle_id"],
            validation_bundle_digest=active["validation_bundle_digest"],
            approval_binding_id=active["approval_binding_id"],
            approval_binding_digest=active["approval_binding_digest"],
        )
        checker_artifact_id = self._component_id(manifest, "checker")
        checker = self._artifact(owner, checker_artifact_id)
        release = TargetRelease.from_artifact(checker)
        public = release.public_manifest()
        release_run_spec_pin = self._release_run_spec_pin(release)
        return {
            "policy_scope": scope,
            "activation_event_id": active["activation_event_id"],
            "active_generation": active["active_generation"],
            "candidate_id": active["candidate_id"],
            "manifest_id": manifest["manifest_id"],
            "manifest_digest": manifest["digest"],
            "validation_bundle_id": active["validation_bundle_id"],
            "validation_bundle_digest": active["validation_bundle_digest"],
            "approval_binding_id": active["approval_binding_id"],
            "approval_binding_digest": active["approval_binding_digest"],
            "components": manifest["components"],
            "final_impact_digest": active.get("final_impact_digest"),
            "final_impact_manifest_id": active.get("final_impact_manifest_id"),
            "final_impact_member_count": active.get("final_impact_member_count"),
            "hold_union": copy.deepcopy(active.get("hold_union") or []),
            **release_run_spec_pin,
            "release": {
                "release_id": public["release_id"],
                "digest": public["digest"],
                "checker_build": public["checker_build"],
                "rules_digest": public["rules_digest"],
                "knowledge_digest": public["knowledge_digest"],
                "normalizer_digest": public["normalizer_digest"],
                "waiver_policy_id": public["waiver_policy_id"],
                "waiver_policy_digest": public["waiver_policy_digest"],
                "limits": public["limits"],
                "applicable_check_ids": public["applicable_check_ids"],
                "applicable_check_count": public["applicable_check_count"],
                "target_release": release,
            },
        }

    def load_pinned_release(self, run_spec: dict[str, Any]) -> TargetRelease:
        """The single full-pin verification seam for a governed RunSpec.

        Reloads the Registry and re-verifies the complete RunSpec pin: the
        manifest ID/digest with every component artifact (missing, duplicate
        or digest-mismatched content is rejected), plus the pinned
        validation bundle and approval binding IDs/digests and their
        candidate/manifest references.  Only then is the checker
        materialized.  Never reads files, current/latest or the global KB.
        """
        if not self.audit_available or not self.storage_available:
            raise PolicyUnavailable(
                "required audit or storage trust is unavailable for resolution"
            )
        manifest_id = run_spec.get("manifest_id")
        manifest_digest = run_spec.get("manifest_digest")
        policy_scope = run_spec.get("policy_scope")
        activation_event_id = run_spec.get("activation_event_id")
        active_generation = run_spec.get("active_generation")
        candidate_id = run_spec.get("candidate_id")
        validation_bundle_id = run_spec.get("validation_bundle_id")
        validation_bundle_digest = run_spec.get("validation_bundle_digest")
        approval_binding_id = run_spec.get("approval_binding_id")
        approval_binding_digest = run_spec.get("approval_binding_digest")
        components = run_spec.get("components")
        if not all(
            isinstance(value, str) and value
            for value in (
                policy_scope,
                activation_event_id,
                manifest_id,
                manifest_digest,
                candidate_id,
                validation_bundle_id,
                validation_bundle_digest,
                approval_binding_id,
                approval_binding_digest,
            )
        ) or (
            isinstance(active_generation, bool)
            or not isinstance(active_generation, int)
            or active_generation < 1
            or not isinstance(components, (list, tuple))
            or not components
        ):
            raise PolicyUnavailable("RunSpec policy pin is incomplete")
        self._store.reload()
        activations = [
            event
            for event in self._store.policy_governance_events
            if event.get("kind") == "activated"
            and event.get("event_id") == activation_event_id
            and event.get("activation_event_id") == activation_event_id
        ]
        if len(activations) != 1:
            raise PolicyUnavailable(
                "RunSpec activation pin does not match the governance ledger"
            )
        activation = activations[0]
        expected_activation = {
            "scope": policy_scope,
            "active_generation": active_generation,
            "candidate_id": candidate_id,
            "manifest_id": manifest_id,
            "manifest_digest": manifest_digest,
            "validation_bundle_id": validation_bundle_id,
            "validation_bundle_digest": validation_bundle_digest,
            "approval_binding_id": approval_binding_id,
        }
        if any(
            activation.get(key) != value
            for key, value in expected_activation.items()
        ) or approval_binding_digest != str(approval_binding_id).removeprefix(
            "approval_sha256_"
        ):
            raise PolicyUnavailable(
                "RunSpec release pin does not match its activation fact"
            )
        manifest = self._verify_pinned_manifest(
            self._store, manifest_id, manifest_digest
        )
        if canonical_bytes(components) != canonical_bytes(manifest["components"]):
            raise PolicyUnavailable(
                "RunSpec components do not match the activated manifest"
            )
        self._verify_bound_evidence(
            self._store,
            manifest,
            candidate_id=candidate_id,
            validation_bundle_id=validation_bundle_id,
            validation_bundle_digest=validation_bundle_digest,
            approval_binding_id=approval_binding_id,
            approval_binding_digest=approval_binding_digest,
        )
        checker = self._artifact(
            self._store, self._component_id(manifest, "checker")
        )
        try:
            release = TargetRelease.from_artifact(checker)
            expected_release_pin = self._release_run_spec_pin(release)
            mismatch = any(
                canonical_bytes(run_spec.get(key)) != canonical_bytes(expected)
                for key, expected in expected_release_pin.items()
            )
        except (KeyError, TypeError, ValueError) as error:
            raise PolicyUnavailable(
                "RunSpec release material is not verifiable"
            ) from error
        if mismatch:
            raise PolicyUnavailable(
                "RunSpec release material does not match the activated checker"
            )
        return release

    def _verify_pinned_manifest(
        self, owner: SQLiteTargetStore, manifest_id: str, manifest_digest: str
    ) -> dict[str, Any]:
        """Verify a pinned manifest and its complete component list: every
        {type, id, digest} must resolve to exactly one Registry artifact
        whose content digest matches, with no duplicate or incomplete
        entries.  Missing/duplicated/mismatched content fails closed."""
        manifest = self._manifest(owner, manifest_id)
        if manifest.get("digest") != manifest_digest:
            raise PolicyUnavailable(
                "pinned manifest digest does not match the registry"
            )
        seen_ids: set[str] = set()
        for item in manifest["components"]:
            component_type = item.get("type")
            artifact_id = item.get("id")
            digest = item.get("digest")
            if not all(
                isinstance(value, str) and value
                for value in (component_type, artifact_id, digest)
            ):
                raise PolicyUnavailable("manifest component is incomplete")
            if artifact_id in seen_ids:
                raise PolicyUnavailable("manifest component id is duplicated")
            seen_ids.add(artifact_id)
            rows = [
                row
                for row in owner.policy_artifacts
                if row.get("artifact_id") == artifact_id
            ]
            if len(rows) != 1 or rows[0].get("content_sha256") != digest:
                raise PolicyUnavailable(
                    f"manifest component {component_type} is missing or "
                    "digest mismatch"
                )
            row = rows[0]
            if row.get("canonical_json"):
                try:
                    content = json.loads(row["canonical_json"])
                except (TypeError, json.JSONDecodeError):
                    content = None
                if content is None or content_digest(content) != digest:
                    raise PolicyUnavailable(
                        f"manifest component {component_type} content mismatch"
                    )
            elif not row.get("raw_hex") or raw_digest(
                bytes.fromhex(row["raw_hex"])
            ) != digest:
                raise PolicyUnavailable(
                    f"manifest component {component_type} raw content mismatch"
                )
        return manifest

    def _verify_bound_evidence(
        self,
        owner: SQLiteTargetStore,
        manifest: dict[str, Any],
        *,
        candidate_id: str,
        validation_bundle_id: str,
        validation_bundle_digest: str,
        approval_binding_id: str,
        approval_binding_digest: str,
        require_current_validator: bool = False,
    ) -> None:
        """Verify the validation and approval facts bound to one candidate."""
        state = self._require_candidate_state(owner, candidate_id)
        expected_state = {
            "manifest_id": manifest["manifest_id"],
            "manifest_digest": manifest["digest"],
            "validation_bundle_id": validation_bundle_id,
            "validation_bundle_digest": validation_bundle_digest,
            "approval_binding_id": approval_binding_id,
            "approval_binding_digest": approval_binding_digest,
        }
        if any(
            state.get(key) != value for key, value in expected_state.items()
        ) or canonical_bytes(state.get("components")) != canonical_bytes(
            manifest["components"]
        ):
            raise PolicyUnavailable(
                "pinned evidence does not match the governance ledger"
            )
        validation = self._artifact(owner, validation_bundle_id)
        if require_current_validator:
            self._verify_validator_identity(validation)
        if (
            validation_bundle_id
            != f"validation_sha256_{validation_bundle_digest}"
            or validation.get("status") != "validated"
            or validation.get("candidate_id") != candidate_id
            or validation.get("manifest_id") != manifest["manifest_id"]
            or validation.get("manifest_digest") != manifest["digest"]
        ):
            raise PolicyUnavailable(
                "pinned validation bundle does not match the candidate manifest"
            )
        binding = self._artifact(owner, approval_binding_id)
        if (
            approval_binding_id
            != f"approval_sha256_{approval_binding_digest}"
            or binding.get("schema_version") != APPROVAL_BINDING_SCHEMA
            or binding.get("candidate_id") != candidate_id
            or binding.get("candidate_digest") != manifest["digest"]
            or binding.get("validation_bundle_id") != validation_bundle_id
            or binding.get("validation_bundle_digest")
            != validation_bundle_digest
            or binding.get("scope") != S08_SCOPE
            or binding.get("activation_time") != state.get("activation_time")
            or binding.get("recovery_release_id")
            != state.get("recovery_release_id")
        ):
            raise PolicyUnavailable(
                "pinned approval binding does not match the candidate manifest"
            )

    def has_governed_activation(self, scope: str) -> bool:
        """Ledger-owned admission seam: True only when the append-only
        Governance Ledger contains an activation for the scope.  The mutable
        projection cache is never consulted, so a missing or corrupt cache
        row cannot flip a governed runtime back to a legacy path."""
        with self._lock:
            self._store.reload()
            return (
                self._fold_active_projection(
                    self._store.policy_governance_events, scope
                )
                is not None
            )

    def load_compat_release(self, run_spec: dict[str, Any]) -> TargetRelease:
        """Exact-map a pre-cutover RunSpec to the Registry checker artifact
        that carries the same release identity (the bootstrap migration
        release).  Never reads legacy YAML/JSON files; a RunSpec with no
        matching Registry artifact fails closed."""
        if not self.audit_available or not self.storage_available:
            raise PolicyUnavailable(
                "required audit or storage trust is unavailable for resolution"
            )
        release_digest = run_spec.get("release_digest")
        release_id = run_spec.get("release_id")
        checker_build = run_spec.get("checker_build")
        if not all(
            isinstance(value, str) and value
            for value in (release_digest, release_id, checker_build)
        ):
            raise PolicyUnavailable("RunSpec compatibility pin is incomplete")
        self._store.reload()
        activations = sorted(
            (
                event
                for event in self._store.policy_governance_events
                if event.get("kind") == "activated" and event.get("bootstrap")
            ),
            key=lambda event: int(event.get("revision", 0)),
            reverse=True,
        )
        for activation in activations:
            try:
                manifest = self._verify_pinned_manifest(
                    self._store,
                    str(activation.get("manifest_id") or ""),
                    str(activation.get("manifest_digest") or ""),
                )
                approval_binding_id = str(
                    activation.get("approval_binding_id") or ""
                )
                self._verify_bound_evidence(
                    self._store,
                    manifest,
                    candidate_id=str(activation.get("candidate_id") or ""),
                    validation_bundle_id=str(
                        activation.get("validation_bundle_id") or ""
                    ),
                    validation_bundle_digest=str(
                        activation.get("validation_bundle_digest") or ""
                    ),
                    approval_binding_id=approval_binding_id,
                    approval_binding_digest=approval_binding_id.removeprefix(
                        "approval_sha256_"
                    ),
                )
                checker = self._artifact(
                    self._store, self._component_id(manifest, "checker")
                )
            except PolicyUnavailable:
                continue
            try:
                release = TargetRelease.from_artifact(checker)
            except ValueError:
                continue
            public = release.public_manifest()
            if (
                public["digest"] == release_digest
                and public["release_id"] == release_id
                and public["checker_build"] == checker_build
            ):
                return release
        raise PolicyUnavailable(
            "no Registry checker artifact matches the pre-cutover RunSpec"
        )

    def load_pinned_checker(
        self, run_spec: dict[str, Any]
    ) -> TargetChecker:
        """Materialize the checker only from RunSpec-pinned Registry facts.

        The Registry is revalidated on every call; the cache is a
        non-authoritative accelerator keyed by release digest, so a deleted
        or tampered artifact can never be masked by a warm object."""
        manifest_digest = run_spec.get("manifest_digest")
        if not isinstance(manifest_digest, str):
            raise PolicyUnavailable("RunSpec policy pin is incomplete")
        release = self.load_pinned_release(run_spec)
        cached = self._checker_cache.get(manifest_digest)
        if cached is not None:
            cached_release_digest, cached_checker = cached
            if cached_release_digest == release.release_digest:
                return cached_checker
        built = TargetChecker(release)
        self._checker_cache[manifest_digest] = (release.release_digest, built)
        return built

    # -------------------------------------------------------------- queries

    def query_status(
        self, principal: PolicyPrincipal
    ) -> dict[str, Any]:
        _validate_principal(principal)
        with self._lock:
            self._store.reload()
            active = self._fold_active_projection(
                self._store.policy_governance_events, principal.scope
            )
            hold = self._activation_hold(self._store, principal.scope)
            return {
                "track": "C-DEMO",
                "capability_gate": "G3",
                "scope": principal.scope,
                "governance_revision": len(self._store.policy_governance_events),
                "active_generation": (
                    active.get("active_generation") if active is not None else None
                ),
                "bootstrap": bool(active.get("bootstrap")) if active else False,
                "activation_hold": hold,
                "holds": (
                    copy.deepcopy(active.get("hold_union") or [])
                    if active is not None
                    else []
                ),
                "final_impact_digest": (
                    active.get("final_impact_digest") if active is not None else None
                ),
                "watermark": self._store.projection_watermark,
            }

    def query_active(self, principal: PolicyPrincipal) -> dict[str, Any]:
        _validate_principal(principal)
        with self._lock:
            self._store.reload()
            active = self._fold_active_projection(
                self._store.policy_governance_events, principal.scope
            )
            if active is None:
                return {
                    "status": "none",
                    "track": "C-DEMO",
                    "capability_gate": "G3",
                    "scope": principal.scope,
                }
            manifest = self._manifest(self._store, active["manifest_id"])
            return {
                "status": "active",
                "track": "C-DEMO",
                "capability_gate": "G3",
                "scope": principal.scope,
                "active_generation": active["active_generation"],
                "activation_event_id": active["activation_event_id"],
                "candidate_id": active["candidate_id"],
                "manifest_id": manifest["manifest_id"],
                "manifest_digest": manifest["digest"],
                "approval_binding_id": active["approval_binding_id"],
                "approval_binding_digest": active["approval_binding_digest"],
                "validation_bundle_id": active["validation_bundle_id"],
                "validation_bundle_digest": active["validation_bundle_digest"],
                "recovery_release_id": active["recovery_release_id"],
                "activated_at": active["activated_at"],
                "bootstrap": bool(active.get("bootstrap")),
                "activation_hold": active.get("activation_hold"),
                "holds": copy.deepcopy(active.get("hold_union") or []),
                "final_impact_digest": active.get("final_impact_digest"),
                "final_impact_manifest_id": active.get("final_impact_manifest_id"),
                "final_impact_member_count": active.get("final_impact_member_count"),
                "components": manifest["components"],
            }

    def query_candidates(self, principal: PolicyPrincipal) -> dict[str, Any]:
        _validate_principal(principal)
        with self._lock:
            self._store.reload()
            candidates = [
                {
                    "candidate_id": candidate["candidate_id"],
                    "status": candidate["status"],
                    "manifest_id": candidate.get("manifest_id"),
                    "manifest_digest": candidate.get("manifest_digest"),
                    "validation_bundle_id": candidate.get("validation_bundle_id"),
                    "approval_binding_id": candidate.get("approval_binding_id"),
                    "active_generation": candidate.get("active_generation"),
                    "author_subject": candidate.get("author_subject"),
                }
                for candidate in sorted(
                    self._fold_candidates(self._store).values(),
                    key=lambda item: item["candidate_id"],
                )
            ]
            return {
                "track": "C-DEMO",
                "capability_gate": "G3",
                "scope": principal.scope,
                "candidates": candidates,
            }

    def query_candidate(
        self, principal: PolicyPrincipal, candidate_id: str
    ) -> dict[str, Any]:
        _validate_principal(principal)
        with self._lock:
            self._store.reload()
            state = self._require_candidate_state(self._store, candidate_id)
            workspace: dict[str, Any] = {
                "track": "C-DEMO",
                "capability_gate": "G3",
                "candidate_id": candidate_id,
                "status": state["status"],
                "manifest_id": state.get("manifest_id"),
                "manifest_digest": state.get("manifest_digest"),
                "validation_bundle_id": state.get("validation_bundle_id"),
                "validation_bundle_digest": state.get("validation_bundle_digest"),
                "approval_binding_id": state.get("approval_binding_id"),
                "approval_binding_digest": state.get("approval_binding_digest"),
                "activation_event_id": state.get("activation_event_id"),
                "active_generation": state.get("active_generation"),
                "author_subject": state.get("author_subject"),
                "recovery_release_id": state.get("recovery_release_id"),
                "activation_time": state.get("activation_time"),
            }
            if state.get("manifest_id") and state["status"] not in {
                "candidate",
                "cancelled",
            }:
                workspace["manifest"] = self._manifest(
                    self._store, state["manifest_id"]
                )
            if state.get("validation_bundle_id"):
                validation_bundle = self._artifact(
                    self._store, state["validation_bundle_id"]
                )
                # Raw frozen-corpus outcomes remain sealed Registry evidence;
                # the browser workspace exposes only minimized validation facts.
                validation_bundle["results"].pop("raw_outcomes", None)
                workspace["validation_bundle"] = validation_bundle
            if state.get("approval_binding_id"):
                workspace["approval_binding"] = self._artifact(
                    self._store, state["approval_binding_id"]
                )
            # The prospective review material (component changes,
            # applicable-check delta, behavior result, mapping ledger and
            # unsupported report) is exposed before approval; approve()
            # binds exactly these recomputed bytes.
            if state.get("manifest_id") and state["status"] not in {
                "candidate",
                "cancelled",
            }:
                workspace["review_material"] = self._review_material(
                    self._store, state, principal.scope
                )
            return workspace

    # Server-owned workspace actions: the single authority for the command
    # surface a (status, role) pair may issue.  Every entry is exactly a
    # command the service guards accept for this (status, role); the HTTP
    # adapter and the React panel both consume this projection and never
    # re-derive a transition table.
    _S08_ADMIN_ACTIONS_BY_STATUS: dict[str, tuple[str, ...]] = {
        "candidate": ("request_validation", "cancel"),
        "validated": ("submit_review", "cancel"),
        "in_review": ("cancel",),
        "approved": ("schedule", "cancel"),
        "scheduled": ("cancel",),
        "active": (),
        "superseded": (),
        "rejected": (),
        "cancelled": (),
    }
    _S08_APPROVER_ACTIONS_BY_STATUS: dict[str, tuple[str, ...]] = {
        "candidate": (),
        "validated": ("reject",),
        "in_review": ("approve", "reject"),
        "approved": (),
        "scheduled": (),
        "active": (),
        "superseded": (),
        "rejected": (),
        "cancelled": (),
    }

    @classmethod
    def _candidate_actions(cls, status: str, role: str) -> list[str]:
        """The exact command names the backend accepts for this candidate
        status and role.  Approval also requires the candidate author to
        differ from the approver; only the Rule Administrator can author a
        candidate, so the approver can never approve their own work and the
        admin table never offers approve."""
        table = (
            cls._S08_ADMIN_ACTIONS_BY_STATUS
            if role == "admin"
            else cls._S08_APPROVER_ACTIONS_BY_STATUS
        )
        return list(table.get(status, ()))

    def query_candidate_workspace(
        self, principal: PolicyPrincipal, candidate_id: str
    ) -> dict[str, Any]:
        """The single atomic candidate workspace: one lock hold and one store
        reload builds the authoritative status, revision, prior-active anchor,
        events, job outcomes and server-owned actions together, so a worker
        transition can never land between the reads and yield an inconsistent
        projection.  The HTTP adapter maps this snapshot into the closed DTO
        and owns no domain transition rules."""
        _validate_principal(principal)
        with self._lock:
            self._store.reload()
            state = self._require_candidate_state(self._store, candidate_id)
            workspace: dict[str, Any] = {
                "track": "C-DEMO",
                "capability_gate": "G3",
                "candidate_id": candidate_id,
                "status": state["status"],
                "manifest_id": state.get("manifest_id"),
                "manifest_digest": state.get("manifest_digest"),
                "validation_bundle_id": state.get("validation_bundle_id"),
                "validation_bundle_digest": state.get("validation_bundle_digest"),
                "approval_binding_id": state.get("approval_binding_id"),
                "approval_binding_digest": state.get("approval_binding_digest"),
                "activation_event_id": state.get("activation_event_id"),
                "active_generation": state.get("active_generation"),
                "author_subject": state.get("author_subject"),
                "recovery_release_id": state.get("recovery_release_id"),
                "activation_time": state.get("activation_time"),
            }
            if state.get("manifest_id") and state["status"] not in {
                "candidate",
                "cancelled",
            }:
                workspace["manifest"] = self._manifest(
                    self._store, state["manifest_id"]
                )
            if state.get("validation_bundle_id"):
                validation_bundle = self._artifact(
                    self._store, state["validation_bundle_id"]
                )
                validation_bundle["results"].pop("raw_outcomes", None)
                workspace["validation_bundle"] = validation_bundle
            if state.get("approval_binding_id"):
                workspace["approval_binding"] = self._artifact(
                    self._store, state["approval_binding_id"]
                )
            # The prospective review material (component changes,
            # applicable-check delta, behavior result, mapping ledger and
            # unsupported report) is exposed before approval; approve()
            # binds exactly these recomputed bytes.
            if state.get("manifest_id") and state["status"] not in {
                "candidate",
                "cancelled",
            }:
                workspace["review_material"] = self._review_material(
                    self._store, state, principal.scope
                )
            events = self._store.policy_governance_events
            scope_events = [
                event
                for event in events
                if event.get("scope") == principal.scope
            ]
            # The fencing revision is the global append-only ledger length,
            # exactly the same value query_status reports and
            # _verify_governance_revision validates against; the candidate
            # timeline below stays scope-filtered for display.
            workspace["governance_revision"] = len(events)
            workspace["actor_role"] = principal.role
            workspace["actions"] = self._candidate_actions(
                state["status"], principal.role
            )
            active = self._fold_active_projection(events, principal.scope)
            workspace["active_anchor"] = (
                {
                    "candidate_id": active["candidate_id"],
                    "manifest_digest": active["manifest_digest"],
                }
                if active is not None
                else None
            )
            # The candidate's full governance timeline: every event of the
            # candidate itself plus the events of the originating draft (the
            # freeze event is the only link between the two identities).
            # Append-only ledger order keeps the list revision-ascending.
            origin_draft_id = next(
                (
                    event.get("draft_id")
                    for event in scope_events
                    if event.get("kind") == "candidate_frozen"
                    and event.get("candidate_id") == candidate_id
                ),
                None,
            )
            workspace["events"] = [
                {
                    "event_id": event["event_id"],
                    "revision": event["revision"],
                    "kind": event["kind"],
                    "actor": event["actor"],
                    "trusted_time": event["trusted_time"],
                    "reason_code": event.get("reason_code"),
                    "candidate_id": event.get("candidate_id"),
                    "draft_id": event.get("draft_id"),
                    "manifest_id": event.get("manifest_id"),
                    "approval_binding_id": event.get("approval_binding_id"),
                    "activation_event_id": event.get("activation_event_id"),
                    "active_generation": event.get("active_generation"),
                }
                for event in scope_events
                if event.get("candidate_id") == candidate_id
                or (
                    origin_draft_id is not None
                    and event.get("draft_id") == origin_draft_id
                )
            ]
            workspace["validation_outcome"] = self._validation_outcome(state)
            workspace["activation_outcome"] = self._activation_outcome(state)
            return workspace

    @classmethod
    def _governance_actions(
        cls, role: str, hold_union: list[dict[str, Any]]
    ) -> list[str]:
        """The server-owned T09 action surface: exactly the command names
        the backend accepts for this role under the current ledger state.
        The React panel renders this list and never derives a transition
        table."""
        if role == "operator":
            actions = ["impose_hold"]
            if hold_union:
                # The compatible rollback path exists only while a Policy
                # Safety Hold is in force; without a hold there is nothing
                # to recover through.
                actions.append("propose_rollback")
            return actions
        if role == "approver":
            return ["recover_hold"] if hold_union else []
        return []

    def query_governance_workspace(
        self, principal: PolicyPrincipal
    ) -> dict[str, Any]:
        """The single atomic T09 governance workspace: one lock hold and one
        store reload builds the authoritative revision, actor role,
        server-owned actions, active release, recorded recovery anchor,
        active hold union and the append-only S09 event refs together, so a
        worker transition can never land between the reads and yield an
        inconsistent projection.  The HTTP adapter maps this snapshot into
        the closed DTO and owns no domain transition rules."""
        _validate_principal(principal)
        if principal.role not in {"admin", "approver", "operator", "auditor"}:
            raise PolicyInvalidTransition(
                "governance workspace is not available to this role"
            )
        with self._lock:
            self._store.reload()
            events = self._store.policy_governance_events
            active = self._fold_active_projection(events, principal.scope)
            hold_union = (
                copy.deepcopy(active.get("hold_union") or [])
                if active is not None
                else []
            )
            workspace: dict[str, Any] = {
                "track": "C-DEMO",
                "capability_gate": "G3",
                "scope": principal.scope,
                "governance_revision": len(events),
                "actor_role": principal.role,
                "actions": self._governance_actions(principal.role, hold_union),
                "active_release": None,
                "recovery_anchor": None,
                "holds": hold_union,
                "events": [],
                "audit_events": [],
            }
            if active is not None:
                workspace["active_release"] = {
                    "active_generation": active["active_generation"],
                    "candidate_id": active["candidate_id"],
                    "manifest_id": active["manifest_id"],
                    "manifest_digest": active["manifest_digest"],
                    "activation_event_id": active["activation_event_id"],
                    "approval_binding_id": active["approval_binding_id"],
                    "validation_bundle_id": active["validation_bundle_id"],
                    "validation_bundle_digest": active[
                        "validation_bundle_digest"
                    ],
                    "recovery_release_id": active["recovery_release_id"],
                    "activated_at": active["activated_at"],
                    "bootstrap": bool(active.get("bootstrap")),
                    "final_impact_digest": active.get("final_impact_digest"),
                    "final_impact_manifest_id": active.get(
                        "final_impact_manifest_id"
                    ),
                    "final_impact_member_count": active.get(
                        "final_impact_member_count"
                    ),
                }
                if active.get("recovery_release_id"):
                    workspace["recovery_anchor"] = {
                        "release_candidate_id": active["recovery_release_id"]
                    }
            workspace["events"] = [
                {
                    "event_id": event["event_id"],
                    "revision": event["revision"],
                    "kind": event["kind"],
                    "actor": event["actor"],
                    "trusted_time": event["trusted_time"],
                    "reason_code": event.get("reason_code"),
                    "candidate_id": event.get("candidate_id"),
                    "manifest_id": event.get("manifest_id"),
                    "activation_event_id": event.get("activation_event_id"),
                    "active_generation": event.get("active_generation"),
                    "hold_id": event.get("hold_id"),
                    "release_candidate_id": event.get("release_candidate_id"),
                    "recovery_generation": event.get("recovery_generation"),
                }
                for event in events
                if event.get("scope") == principal.scope
            ]
            if principal.role == "auditor":
                # P-3: the minimized Auditor-only read of the append-only
                # Security Audit records, under the same lock as every other
                # workspace fact.  Governance events stay the release-history
                # view; no client or HTTP adapter ever writes audit state.
                workspace["audit_events"] = [
                    {
                        "event_id": record["event_id"],
                        "action": record["action"],
                        "subject": record["subject"],
                        "role": record["role"],
                        "result": record["result"],
                        "reason_code": record.get("reason_code"),
                        "event_time": record.get("event_time"),
                        "event_sequence": record.get("event_sequence"),
                        "candidate_id": record.get("candidate_id"),
                        "hold_id": record.get("hold_id"),
                        "release_candidate_id": record.get(
                            "release_candidate_id"
                        ),
                        "rollback_candidate_id": record.get(
                            "rollback_candidate_id"
                        ),
                        "recovery_generation": record.get(
                            "recovery_generation"
                        ),
                        "governance_event_id": record.get(
                            "governance_event_id"
                        ),
                    }
                    for record in self._store.audit_events
                    if record.get("scope") == principal.scope
                ]
            return workspace

    def _validation_outcome(
        self, state: dict[str, Any]
    ) -> dict[str, Any] | None:
        """The authoritative terminal or in-flight state of this candidate's
        validation job, projected from the append-only ledger verdicts and
        the durable job row; never derived by the browser.  A candidate that
        validated and then moved on (approved/superseded/cancelled) still
        reports the validated terminal, because the ledger event is
        immutable."""
        events = self._store.policy_governance_events
        verdicts = [
            event
            for event in events
            if event.get("candidate_id") == state["candidate_id"]
            and event.get("kind") in {"validated", "rejected"}
            and event.get("validation_bundle_id")
        ]
        if verdicts:
            last = verdicts[-1]
            if last["kind"] == "validated":
                return {
                    "status": "validated",
                    "reason_code": "S08_VALIDATION_PASSED",
                }
            return {
                "status": "rejected",
                "reason_code": "S08_VALIDATION_REJECTED",
            }
        if state["status"] == "cancelled":
            # A cancelled candidate has no validation terminal unless the
            # ledger already carries a verdict (handled above).
            return None
        job = next(
            (
                item
                for item in reversed(self._store.policy_jobs)
                if item.get("kind") == "validation"
                and item.get("candidate_id") == state["candidate_id"]
            ),
            None,
        )
        if job is None:
            return None
        if job.get("status") == "diagnostic":
            return {
                "status": "failed",
                "reason_code": job.get("reason_code") or "S08_VALIDATION_INTERNAL",
            }
        return {"status": "pending", "reason_code": None}

    def _activation_outcome(
        self, state: dict[str, Any]
    ) -> dict[str, Any] | None:
        """The authoritative terminal or in-flight state of this candidate's
        activation: the append-only ``activated`` event is the terminal
        success (a later supersession does not rewrite it), a diagnostic
        worker failure is the terminal failure that keeps the prior-active
        anchor visible, and anything else is pending.  Never derived by the
        browser.  Failure carries only the registered stable reason code."""
        activated = [
            event
            for event in self._store.policy_governance_events
            if event.get("kind") == "activated"
            and event.get("candidate_id") == state["candidate_id"]
        ]
        if activated:
            last = activated[-1]
            return {
                "status": "active",
                "activation_event_id": last.get("activation_event_id"),
                "active_generation": last.get("active_generation"),
            }
        if state["status"] == "cancelled":
            # A cancelled candidate has no activation terminal unless the
            # ledger already carries an activation (handled above).
            return None
        job = next(
            (
                item
                for item in self._store.policy_jobs
                if item.get("kind") == "activation"
                and item.get("candidate_id") == state["candidate_id"]
            ),
            None,
        )
        if job is None:
            return None
        if job.get("status") == "diagnostic":
            return {
                "status": "failed",
                "reason_code": job.get("reason_code") or "S08_ACTIVATION_INTERNAL",
            }
        return {"status": "pending", "reason_code": None}

    def query_events(self, principal: PolicyPrincipal) -> dict[str, Any]:
        _validate_principal(principal)
        with self._lock:
            self._store.reload()
            events = [
                {
                    "event_id": event["event_id"],
                    "revision": event["revision"],
                    "kind": event["kind"],
                    "actor": event["actor"],
                    "trusted_time": event["trusted_time"],
                    "reason_code": event.get("reason_code"),
                    "candidate_id": event.get("candidate_id"),
                    "draft_id": event.get("draft_id"),
                    "manifest_id": event.get("manifest_id"),
                    "approval_binding_id": event.get("approval_binding_id"),
                    "activation_event_id": event.get("activation_event_id"),
                    "active_generation": event.get("active_generation"),
                }
                for event in self._store.policy_governance_events
                if event.get("scope") == principal.scope
            ]
            return {
                "track": "C-DEMO",
                "capability_gate": "G3",
                "scope": principal.scope,
                "governance_revision": len(events),
                "events": events,
            }

    def query_drafts(self, principal: PolicyPrincipal) -> dict[str, Any]:
        _validate_principal(principal)
        with self._lock:
            self._store.reload()
            drafts = [
                {
                    "draft_id": draft["draft_id"],
                    "status": draft["status"],
                    "revision": draft["revision"],
                    "source_bundle_id": draft["source_bundle_id"],
                    "source_sha256": draft["source_sha256"],
                    "mapping_ledger_id": draft["mapping_ledger_id"],
                    "mapping_ledger_digest": draft["mapping_ledger_digest"],
                    "candidate_id": draft.get("candidate_id"),
                    "bootstrap": bool(draft.get("bootstrap")),
                }
                for draft in sorted(
                    self._store.policy_drafts.values(),
                    key=lambda item: item["draft_id"],
                )
                if draft.get("scope") == principal.scope
            ]
            return {
                "track": "C-DEMO",
                "capability_gate": "G3",
                "scope": principal.scope,
                "drafts": drafts,
            }

    def resolve_evaluation_release(
        self, *, release_id: str, release_digest: str
    ) -> dict[str, Any]:
        """S12 verified read: resolve exactly one governed release by its
        public release identity.  The manifest digest, every component
        artifact digest, and the materialized checker release are re-verified
        against the Registry before any value is returned; the read never
        writes governance state."""
        if not self.audit_available or not self.storage_available:
            raise PolicyUnavailable(
                "required audit or storage trust is unavailable for resolution"
            )
        with self._lock:
            self._store.reload()
            candidates: list[dict[str, Any]] = []
            for manifest in self._store.policy_manifests:
                if manifest.get("schema_version") != MANIFEST_SCHEMA:
                    continue
                verified = self._verify_pinned_manifest(
                    self._store, manifest["manifest_id"], manifest["digest"]
                )
                checker_id = self._component_id(verified, "checker")
                artifact = self._artifact(self._store, checker_id)
                try:
                    release = TargetRelease.from_artifact(artifact)
                except (KeyError, TypeError, ValueError):
                    raise PolicyUnavailable(
                        "registry checker artifact is not materializable"
                    )
                public = release.public_manifest()
                if (
                    public["release_id"] == release_id
                    and public["digest"] == release_digest
                ):
                    candidates.append(
                        {
                            "release_id": public["release_id"],
                            "release_digest": public["digest"],
                            "checker_build": public["checker_build"],
                            "checker_artifact": artifact,
                            "target_release": release,
                            "manifest_id": verified["manifest_id"],
                            "manifest_digest": verified["digest"],
                            "components": verified["components"],
                            "limits": public["limits"],
                            "applicable_check_ids": public["applicable_check_ids"],
                            "applicable_check_count": public[
                                "applicable_check_count"
                            ],
                            "protected_baseline_digest": public["digest"],
                        }
                    )
            if len(candidates) != 1:
                raise GovernedReleaseNotFound(
                    "release identity does not resolve to exactly one governed release"
                )
            return candidates[0]

    def evaluation_governance_measurement(self) -> dict[str, Any]:
        """S12 verified read: authoritative governance-ledger facts used to
        measure deltas across an evaluation freeze and its terminal
        publication.  Purely read-only; never writes governance state."""
        with self._lock:
            self._store.reload()
            activated = [
                event
                for event in self._store.policy_governance_events
                if event.get("kind") == "activated"
            ]
            activation_digest = (
                hashlib.sha256(
                    json.dumps(
                        sorted(
                            str(event.get("event_id") or "")
                            for event in activated
                        ),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                if activated
                else None
            )
            return {
                "governance_revision": len(self._store.policy_governance_events),
                "activation_count": len(activated),
                "activation_digest": activation_digest,
                # Canonical ordered vectors over every relevant governance
                # row: a change to any event payload, manifest or artifact
                # changes the measurement even when ids and counts stay
                # constant (SP-12).
                "governance_events_vector": [
                    {
                        "event_id": str(event.get("event_id") or ""),
                        "revision": int(event.get("revision") or 0),
                        "kind": str(event.get("kind") or ""),
                        "scope": str(event.get("scope") or ""),
                        "reason_code": str(event.get("reason_code") or ""),
                        "payload_digest": hashlib.sha256(
                            json.dumps(
                                event,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        ).hexdigest(),
                    }
                    for event in sorted(
                        self._store.policy_governance_events,
                        key=lambda item: str(item.get("event_id") or ""),
                    )
                ],
                "governance_events_id_list_digest": hashlib.sha256(
                    json.dumps(
                        [
                            str(event.get("event_id") or "")
                            for event in sorted(
                                self._store.policy_governance_events,
                                key=lambda item: str(item.get("event_id") or ""),
                            )
                        ],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
                "manifests_vector": [
                    {
                        "manifest_id": str(manifest.get("manifest_id") or ""),
                        "schema_version": str(
                            manifest.get("schema_version") or ""
                        ),
                        "declared_digest": str(manifest.get("digest") or ""),
                        "content_digest": hashlib.sha256(
                            canonical_bytes(
                                json.loads(manifest["canonical_json"])
                                if isinstance(
                                    manifest.get("canonical_json"), str
                                )
                                else {}
                            )
                        ).hexdigest(),
                    }
                    for manifest in sorted(
                        self._store.policy_manifests,
                        key=lambda item: str(item.get("manifest_id") or ""),
                    )
                ],
                "manifests_id_list_digest": hashlib.sha256(
                    json.dumps(
                        [
                            str(manifest.get("manifest_id") or "")
                            for manifest in sorted(
                                self._store.policy_manifests,
                                key=lambda item: str(item.get("manifest_id") or ""),
                            )
                        ],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
                "artifacts_vector": [
                    {
                        "artifact_id": str(artifact.get("artifact_id") or ""),
                        "content_digest": hashlib.sha256(
                            canonical_bytes(
                                json.loads(artifact["canonical_json"])
                                if isinstance(
                                    artifact.get("canonical_json"), str
                                )
                                else {}
                            )
                        ).hexdigest(),
                    }
                    for artifact in sorted(
                        self._store.policy_artifacts,
                        key=lambda item: str(item.get("artifact_id") or ""),
                    )
                ],
                "artifacts_id_list_digest": hashlib.sha256(
                    json.dumps(
                        [
                            str(artifact.get("artifact_id") or "")
                            for artifact in sorted(
                                self._store.policy_artifacts,
                                key=lambda item: str(item.get("artifact_id") or ""),
                            )
                        ],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
            }

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _result(value: dict[str, Any], *, replayed: bool = False) -> dict[str, Any]:
        if "replayed" in value:
            # A mutate already declared its replay semantics (e.g. a
            # second recovery key for an already released hold); the
            # wrapper never overrides it.
            return value
        return {**value, "replayed": replayed}

    def _persist_staged(self, staged: SQLiteTargetStore) -> None:
        try:
            staged.persist()
        except StaleStoreRevision:
            self._store.reload()
            raise PolicyConflict("store revision advanced concurrently") from None

    @staticmethod
    def _owned_job(
        staged: SQLiteTargetStore, job: dict[str, Any], now: int
    ) -> dict[str, Any] | None:
        """The current job row iff the exact claim still owns it AND the
        owned lease is still live at the trusted settlement time: same
        identity, still leased by this worker/fence/attempt under the same
        lease_until, and ``lease_until > now``.  A stale worker (reclaimed
        by a newer fence, completed, restarted, or expired with no
        successor) gets ``None`` and must never touch the row or publish
        any domain fact."""
        for item in staged.policy_jobs:
            if item.get("policy_job_id") != job["policy_job_id"]:
                continue
            if (
                item.get("status") == "leased"
                and item.get("worker_id") == job.get("worker_id")
                and item.get("fence") == job.get("fence")
                and item.get("attempt_no") == job.get("attempt_no")
                and item.get("lease_until") == job.get("lease_until")
                and int(item.get("lease_until", 0)) > now
            ):
                return item
            return None
        return None

    @staticmethod
    def _attempt_record(
        job: dict[str, Any],
        *,
        status: str,
        started_at: int,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One durable attempt record.  The claim-time ``running`` record
        carries the base id; every terminal record uses a status-suffixed id
        because the attempts table is append-only, so a claim, a terminal
        outcome and a stale settlement of the same attempt never collide."""
        suffix = "" if status == "running" else f":{status}"
        record: dict[str, Any] = {
            "attempt_id": PolicyGovernanceService._stable_id(
                "policy_attempt",
                f"{job['policy_job_id']}:{job['attempt_no']}:{job['worker_id']}{suffix}",
            ),
            "policy_job_id": job["policy_job_id"],
            "kind": job["kind"],
            "candidate_id": job.get("candidate_id"),
            "fence": job["fence"],
            "attempt_no": job["attempt_no"],
            "started_at": started_at,
            "status": status,
        }
        if result is not None:
            record["result"] = result
        return record

    def _settle_stale_attempt(
        self, staged: SQLiteTargetStore, job: dict[str, Any], now: int
    ) -> dict[str, Any]:
        """Ownership was lost before this worker's terminal write: the stale
        claim settles only as its own discarded attempt; the newer lease and
        every domain fact stay untouched.  Called with the lock already held
        and ``staged`` a fresh copy of the current store."""
        staged.policy_attempts.append(
            self._attempt_record(
                job,
                status="discarded",
                started_at=now,
                result={"outcome": "discarded", "reason_code": "S08_ATTEMPT_STALE"},
            )
        )
        if not self._persist_worker(staged):
            return {
                "status": "retry",
                "kind": job["kind"],
                "candidate_id": job.get("candidate_id"),
            }
        return {
            "status": "discarded",
            "kind": job["kind"],
            "candidate_id": job.get("candidate_id"),
            "reason_code": "S08_ATTEMPT_STALE",
        }

    def _settle_discarded(
        self,
        staged: SQLiteTargetStore,
        job: dict[str, Any],
        now: int,
        settlement_now: int,
    ) -> dict[str, Any]:
        """A still-owned job whose candidate was invalidated while the
        worker computed (cancel, hold, or any state change) settles as
        complete + discarded: the stale verdict is never published, and the
        job is never lied about as diagnostic.  Re-checks ownership against
        the trusted settlement time because every terminal write must prove
        the exact claim is still live."""
        current_job = self._owned_job(staged, job, settlement_now)
        if current_job is None:
            return self._settle_stale_attempt(staged, job, now)
        current_job["status"] = "complete"
        current_job.pop("lease_until", None)
        staged.policy_attempts.append(
            self._attempt_record(
                job,
                status="discarded",
                started_at=now,
                result={"outcome": "discarded"},
            )
        )
        reservation = staged.policy_schedule_reservations.get(
            job.get("reservation_id")
        )
        if (
            isinstance(reservation, dict)
            and reservation.get("status") == "pending"
        ):
            reservation["status"] = "cancelled"
        if not self._persist_worker(staged):
            return {
                "status": "retry",
                "kind": job["kind"],
                "candidate_id": job.get("candidate_id"),
            }
        return {
            "status": "discarded",
            "kind": job["kind"],
            "candidate_id": job.get("candidate_id"),
        }

    @staticmethod
    def _worker_reason_code(kind: str, error: Exception) -> str:
        """The registered stable reason code for a worker failure: raw
        exception text and internal write points never reach the job row or
        any public outcome."""
        table = _WORKER_REASON_CODES.get(kind, _WORKER_REASON_CODES["activation"])
        return table.get(type(error).__name__, table["default"])

    def _persist_worker(self, staged: SQLiteTargetStore) -> bool:
        try:
            staged.persist()
        except StaleStoreRevision:
            self._store.reload()
            return False
        self._store = staged
        return True


def _load_rules_bytes(rules_bytes: bytes) -> Any:
    from task4_consistency.rules.loader import load_rules

    import tempfile

    _load_yaml_source(rules_bytes)
    with tempfile.TemporaryDirectory(prefix="s08-source-") as snapshot_dir:
        snapshot_path = Path(snapshot_dir) / "rules.yaml"
        snapshot_path.write_bytes(rules_bytes)
        snapshot_path.chmod(0o400)
        return load_rules(snapshot_path)


def _load_knowledge_bytes(kb_bytes: bytes) -> dict[str, Any]:
    from task4_consistency.kb.store import EntityKB

    import tempfile

    _load_json_source(kb_bytes)
    with tempfile.TemporaryDirectory(prefix="s08-source-") as snapshot_dir:
        snapshot_path = Path(snapshot_dir) / "entity_kb.json"
        snapshot_path.write_bytes(kb_bytes)
        return EntityKB(snapshot_path).to_dict()
