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
VALIDATOR_BUILD = "s08-validator/2"
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


# Reproducible validator identity: the suite/build plus a digest of the
# validator module itself, so evidence produced by changed validator code
# cannot silently survive an upgrade.  Computed once at import time.
_VALIDATOR_CODE_DIGEST = raw_digest(
    (Path(__file__).resolve().parent / "s08_validate.py").read_bytes()
)


class PolicyNotFound(KeyError):
    """A governed object does not exist (existence is hidden from callers)."""


class PolicyConflict(RuntimeError):
    """Stale revision, same-key/different-fingerprint, or transition conflict."""


class PolicyInvalidTransition(RuntimeError):
    """An illegal candidate transition or actor/role/scope violation."""


class PolicyUnavailable(RuntimeError):
    """Registry/Ledger integrity cannot be proven; resolution fails closed."""


@dataclass(frozen=True)
class PolicyPrincipal:
    subject: str
    role: str  # "admin" | "approver" | "operator"
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
    if principal.role not in {"admin", "approver", "operator"}:
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
            "trusted_time": self._trusted_time(),
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
        with self._lock:
            for _ in range(3):
                self._store.reload()
                key = self._idempotency_key(principal, action, idempotency_key)
                replay = self._replay_or_conflict(self._store, key, fingerprint)
                if replay is not None:
                    return self._result(replay[1], replayed=True)
                if not self.audit_available:
                    raise PolicyInvalidTransition("audit is unavailable")
                staged = copy.deepcopy(self._store)
                result = mutate(staged, key)
                try:
                    staged.persist()
                except StaleStoreRevision:
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
                    f"{draft_id}:fork:{self._trusted_time()}",
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
            policy_job_id = self._stable_id(
                "policy_job", f"{candidate_id}:validation:{self._trusted_time()}"
            )
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
        if validation is not None and validation.get("validator_build") != VALIDATOR_BUILD:
            raise PolicyUnavailable(
                "validation bundle was produced by an outdated validator build"
            )
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
        if ledger_id:
            ledger = self._find_mapping_ledger(owner, ledger_id)
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

    def approve(
        self,
        *,
        principal: PolicyPrincipal,
        candidate_id: str,
        activation_time: int,
        recovery_release_id: str,
        idempotency_key: str,
        expected_governance_revision: int | None,
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
        fingerprint = self._fingerprint(
            "approve", candidate_id, activation_time, recovery_release_id
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
            if self._activation_hold(staged, principal.scope) is not None:
                raise PolicyInvalidTransition("activation hold is in effect")
            if activation_at < self._trusted_time():
                raise PolicyInvalidTransition("activation time is retroactive")
            binding = self._artifact(staged, approval_binding_id)
            if binding.get("schema_version") != APPROVAL_BINDING_SCHEMA:
                raise PolicyInvalidTransition("approval binding is not verifiable")
            candidate_id = str(binding["candidate_id"])
            state = self._require_candidate_state(staged, candidate_id)
            if state["status"] != "approved":
                raise PolicyInvalidTransition(
                    f"candidate {candidate_id} cannot be scheduled from {state['status']}"
                )
            if binding.get("activation_time") != activation_at:
                raise PolicyInvalidTransition(
                    "scheduled time differs from the bound trusted activation time"
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
        with self._lock:
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
                try:
                    staged.persist()
                except StaleStoreRevision:
                    self._store.reload()
                    continue
                self._store = staged
                job = copy.deepcopy(selected)
                if job["kind"] == "validation":
                    return self._run_validation_job(job, observed_now)
                return self._run_activation_job(job, observed_now)
            return {"status": "blocked", "reason_code": "POLICY_JOB_CLAIM_CONTENTION"}

    def _run_validation_job(
        self, job: dict[str, Any], now: int
    ) -> dict[str, Any]:
        candidate_id = job["candidate_id"]
        try:
            owner = copy.deepcopy(self._store)
            state = self._require_candidate_state(owner, candidate_id)
            if state["status"] != "candidate":
                raise PolicyInvalidTransition("candidate is no longer pending validation")
            bundle, outcome = self._validate_candidate(owner, state)
            self._before_write("s08.validation")
            staged = copy.deepcopy(self._store)
            staged_job = next(
                item
                for item in staged.policy_jobs
                if item["policy_job_id"] == job["policy_job_id"]
            )
            staged_job["status"] = "complete"
            staged_job.pop("lease_until", None)
            staged.policy_attempts.append(
                {
                    "attempt_id": self._stable_id(
                        "policy_attempt",
                        f"{job['policy_job_id']}:{job['attempt_no']}:{job['worker_id']}",
                    ),
                    "policy_job_id": job["policy_job_id"],
                    "kind": "validation",
                    "candidate_id": candidate_id,
                    "fence": job["fence"],
                    "attempt_no": job["attempt_no"],
                    "started_at": job["started_at"]
                    if "started_at" in job
                    else now,
                    "status": "complete",
                    "result": {
                        "validation_bundle_id": bundle["validation_bundle_id"],
                        "validation_bundle_digest": bundle["digest"],
                        "outcome": outcome,
                    },
                }
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
            RuntimeError,
        ):
            staged = copy.deepcopy(self._store)
            staged_job = next(
                (
                    item
                    for item in staged.policy_jobs
                    if item["policy_job_id"] == job["policy_job_id"]
                ),
                None,
            )
            if staged_job is not None:
                staged_job["status"] = "diagnostic"
                try:
                    staged.persist()
                    self._store = staged
                except StaleStoreRevision:
                    self._store.reload()
            return {
                "status": "failed",
                "kind": "validation",
                "candidate_id": candidate_id,
            }

    def _run_activation_job(
        self, job: dict[str, Any], now: int
    ) -> dict[str, Any]:
        candidate_id = job["candidate_id"]
        scope = job["scope"]
        try:
            owner = copy.deepcopy(self._store)
            state = self._require_candidate_state(owner, candidate_id)
            if self._activation_hold(owner, scope) is not None:
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
            if not self.audit_available:
                raise PolicyUnavailable("required audit is unavailable")
            self._before_write("s08.activation")
            staged = copy.deepcopy(self._store)
            staged_job = next(
                item
                for item in staged.policy_jobs
                if item["policy_job_id"] == job["policy_job_id"]
            )
            staged_job["status"] = "complete"
            staged_job.pop("lease_until", None)
            staged.policy_attempts.append(
                {
                    "attempt_id": self._stable_id(
                        "policy_attempt",
                        f"{job['policy_job_id']}:{job['attempt_no']}:{job['worker_id']}",
                    ),
                    "policy_job_id": job["policy_job_id"],
                    "kind": "activation",
                    "candidate_id": candidate_id,
                    "fence": job["fence"],
                    "attempt_no": job["attempt_no"],
                    "started_at": now,
                    "status": "complete",
                    "result": {
                        "approval_binding_id": job["approval_binding_id"],
                        "activation_event_id": None,
                        "active_generation": None,
                    },
                }
            )
            prior = self._fold_active_projection(
                staged.policy_governance_events, scope
            )
            generation = (
                int(prior["active_generation"]) + 1 if prior is not None else 1
            )
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
                },
            )
            activation_event["activation_event_id"] = activation_event["event_id"]
            activation_event["active_generation"] = generation
            activation_event_id = activation_event["event_id"]
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
                )
            manifest = self._manifest(staged, state["manifest_id"])
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
                "activated_at": now,
                "bootstrap": False,
                "components": manifest["components"],
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
            KeyError,
            ValueError,
            RuntimeError,
        ) as error:
            staged = copy.deepcopy(self._store)
            staged_job = next(
                (
                    item
                    for item in staged.policy_jobs
                    if item["policy_job_id"] == job["policy_job_id"]
                ),
                None,
            )
            if staged_job is not None:
                staged_job["status"] = "diagnostic"
                staged_job["terminal_reason"] = f"{type(error).__name__}: {error}"
                try:
                    staged.persist()
                    self._store = staged
                except StaleStoreRevision:
                    self._store.reload()
            return {
                "status": "failed",
                "kind": "activation",
                "candidate_id": candidate_id,
                "error": f"{type(error).__name__}: {error}",
            }

    # ------------------------------------------------------------ bootstrap

    def bootstrap_once(self) -> dict[str, Any]:
        """One-time, idempotent migration: import, validate, approve and
        activate the server-owned legacy baseline as the bootstrap release.

        Once a bootstrap activation exists, restart only reads the Registry
        and Ledger; the source files are never a runtime fallback."""
        with self._lock:
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
            if not self.audit_available:
                return {
                    "status": "blocked",
                    "reason_code": "AUDIT_UNAVAILABLE",
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
        rules_text = rules_bytes.decode("utf-8")
        rules_data = yaml.safe_load(rules_text)
        kb_data = json.loads(kb_bytes.decode("utf-8"))
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
            for field in _RULE_KNOWN_FIELDS:
                if field not in rule:
                    continue
                raw_value = rule[field]
                compiled_value = getattr(rule_def, field)
                items.append(
                    {
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
                "source_pointer": "/changelog",
                "source_digest": content_digest(("changelog", rules_data.get("changelog"))),
                "classification": "non_runtime_excluded",
                "target_ref": None,
                "importer_version": IMPORTER_VERSION,
                "reason": "documentation only; cannot affect runtime",
                "result_digest": content_digest(("changelog",)),
            }
        )
        for section in ("version", "description", "graph"):
            if section not in kb_data:
                continue
            value = kb_data[section]
            items.append(
                {
                    "source_pointer": f"/{section}",
                    "source_digest": content_digest(("kb", section, value)),
                    "classification": (
                        "non_runtime_excluded"
                        if section != "graph"
                        else "explicit_transform"
                    ),
                    "target_ref": (
                        None if section != "graph" else "checker.knowledge.aliases"
                    ),
                    "importer_version": IMPORTER_VERSION,
                    "reason": (
                        "metadata only; cannot affect runtime"
                        if section != "graph"
                        else "same_as graph projected into alias sections"
                    ),
                    "result_digest": content_digest(("kb", section, value)),
                }
            )
        for section in ("address_aliases", "org_aliases", "plate_prefixes"):
            values = kb_data.get(section)
            if not isinstance(values, dict):
                raise ValueError(f"knowledge section {section} is invalid")
            for key in sorted(values):
                value = values[key]
                items.append(
                    {
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
        hold = projection.get("activation_hold") if projection else None
        return hold if isinstance(hold, dict) else None

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
        draft_metadata = frozen.get("metadata") if frozen else None
        ledger = (
            self._find_mapping_ledger(owner, mapping_ledger_id)
            if mapping_ledger_id
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

    def _scope_validity_check(
        self, metadata: dict[str, Any] | None
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
        now = self._trusted_time()

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
        raw_outcomes = {
            "anchor": anchor_outcomes["outcomes"],
            "candidate": candidate_outcomes["outcomes"],
            "again": candidate_again["outcomes"],
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
                item["fixture"]
                for item in corpus
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
        anchor_by_id = {item["application_id"]: item for item in anchor}
        candidate_by_id = {item["application_id"]: item for item in candidate}
        compared = 0
        skipped = 0
        checks_equal = True
        selection_equal = True
        normalization_equal = True
        verdicts_equal = True
        route_equal = True
        for application_id in sorted(set(anchor_by_id) | set(candidate_by_id)):
            left = anchor_by_id.get(application_id)
            right = candidate_by_id.get(application_id)
            if left is None or right is None:
                checks_equal = False
                continue
            if "skipped" in left or "skipped" in right:
                skipped += 1
                continue
            compared += 1
            checks_equal = checks_equal and left["applicable"] == right["applicable"]
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

    @staticmethod
    def _find_mapping_ledger(
        owner: SQLiteTargetStore, ledger_id: str
    ) -> dict[str, Any] | None:
        for artifact in owner.policy_artifacts:
            if artifact.get("artifact_id") != ledger_id:
                continue
            if not artifact.get("canonical_json"):
                return None
            try:
                content = json.loads(artifact["canonical_json"])
            except (TypeError, json.JSONDecodeError):
                return None
            if content.get("schema_version") != MAPPING_LEDGER_SCHEMA:
                return None
            return content
        return None

    # ------------------------------------------------------------ resolver

    @classmethod
    def _fold_active_projection(
        cls, events: list[dict[str, Any]], scope: str
    ) -> dict[str, Any] | None:
        """Rebuild the active projection purely from append-only governance
        facts.  The highest active generation wins; a stop event adds the
        activation hold.  The mutable projection table is only a rebuildable
        cache and can never override this fold."""
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
            "components": None,
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
        owner = store if store is not None else self._store
        if store is None:
            owner.reload()
        active = self._fold_active_projection(
            owner.policy_governance_events, scope
        )
        if active is None:
            return None
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
        candidate_id = run_spec.get("candidate_id")
        validation_bundle_id = run_spec.get("validation_bundle_id")
        validation_bundle_digest = run_spec.get("validation_bundle_digest")
        approval_binding_id = run_spec.get("approval_binding_id")
        approval_binding_digest = run_spec.get("approval_binding_digest")
        if not all(
            isinstance(value, str) and value
            for value in (
                manifest_id,
                manifest_digest,
                candidate_id,
                validation_bundle_id,
                validation_bundle_digest,
                approval_binding_id,
                approval_binding_digest,
            )
        ):
            raise PolicyUnavailable("RunSpec policy pin is incomplete")
        self._store.reload()
        manifest = self._verify_pinned_manifest(
            self._store, manifest_id, manifest_digest
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
        return TargetRelease.from_artifact(checker)

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
    ) -> None:
        """Verify the validation bundle and approval binding pinned to a
        candidate: both artifacts must verify under their IDs/digests and
        reference the exact candidate manifest."""
        validation = self._artifact(owner, validation_bundle_id)
        if (
            validation_bundle_id
            != f"validation_sha256_{validation_bundle_digest}"
            or validation.get("status") != "validated"
            or validation.get("validator_build") != VALIDATOR_BUILD
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
        release_digest = run_spec.get("release_digest")
        release_id = run_spec.get("release_id")
        checker_build = run_spec.get("checker_build")
        if not all(
            isinstance(value, str) and value
            for value in (release_digest, release_id, checker_build)
        ):
            raise PolicyUnavailable("RunSpec compatibility pin is incomplete")
        self._store.reload()
        for manifest in self._store.policy_manifests:
            try:
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
                workspace["validation_bundle"] = self._artifact(
                    self._store, state["validation_bundle_id"]
                )
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

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _result(value: dict[str, Any], *, replayed: bool = False) -> dict[str, Any]:
        return {**value, "replayed": replayed}

    def _persist_staged(self, staged: SQLiteTargetStore) -> None:
        try:
            staged.persist()
        except StaleStoreRevision:
            self._store.reload()
            raise PolicyConflict("store revision advanced concurrently") from None

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

    with tempfile.TemporaryDirectory(prefix="s08-source-") as snapshot_dir:
        snapshot_path = Path(snapshot_dir) / "rules.yaml"
        snapshot_path.write_bytes(rules_bytes)
        snapshot_path.chmod(0o400)
        return load_rules(snapshot_path)


def _load_knowledge_bytes(kb_bytes: bytes) -> dict[str, Any]:
    from task4_consistency.kb.store import EntityKB

    import tempfile

    with tempfile.TemporaryDirectory(prefix="s08-source-") as snapshot_dir:
        snapshot_path = Path(snapshot_dir) / "entity_kb.json"
        snapshot_path.write_bytes(kb_bytes)
        return EntityKB(snapshot_path).to_dict()
