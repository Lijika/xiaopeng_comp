"""Pure target checker for the S01 controlled walking skeleton."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from task4_consistency.match.exact import all_equal
from task4_consistency.match.fuzzy import multi_fuzzy_all
from task4_consistency.match.list_ops import list_contains
from task4_consistency.match.numeric import multi_numeric_all
from task4_consistency.normalize.base import (
    basic_clean,
    infer_field_type,
    is_placeholder_value,
    normalize_engine,
    normalize_generic,
    normalize_model,
    normalize_reg_cert_no,
)
from task4_consistency.normalize.date import normalize_date_ex
from task4_consistency.normalize.id_number import normalize_id_number_ex
from task4_consistency.normalize.money import normalize_money_ex
from task4_consistency.normalize.person import normalize_person_name
from task4_consistency.normalize.plate import normalize_plate, normalize_plate_list
from task4_consistency.normalize.result import NormalizeResult
from task4_consistency.normalize.vin import normalize_vin_ex
from task4_consistency.rules.loader import RuleConfig, RuleDef


_TARGET_NORMALIZER_BUILDS = {
    "address": "s01-address/1",
    "brand": "s01-brand/1",
    "date": "normalize-date-ex/1",
    "engine": "normalize-engine/1",
    "generic": "normalize-generic/1",
    "id_number": "normalize-id-number-ex/1",
    "model": "normalize-model/1",
    "money": "normalize-money-ex/1",
    "person": "normalize-person-name/1",
    "plate": "normalize-plate/1",
    "plate_list": "normalize-plate-list/1",
    "reg_cert_no": "normalize-reg-cert-no/1",
    "vin": "normalize-vin-ex/1",
}

_C_DEMO_WAIVER_POLICY_ID = "c-demo-brand-exception/1"
_C_DEMO_WAIVER_SCOPE = "one_application_cycle_run_finding"
_C_DEMO_WAIVER_REASON = "DOCUMENTED_BRAND_VARIANCE"
_C_DEMO_WAIVER_TTL_SECONDS = 900
_PROTECTED_WAIVER_CHECKS = frozenset(
    {"R_VIN_CROSS", "R_ENGINE_CROSS", "R_ID_EXACT"}
)

_TARGET_ARTIFACT_SCHEMA = "s08-target-release/1"
_TARGET_RELEASE_IDENTITY_SCHEMA = "s01-target-release/6"
_TARGET_CHECKER_BUILD = "s01-target-checker/6"
_SEMANTIC_CATALOG_SCHEMA = "s08-semantic-catalog/1"
_NORMALIZATION_POLICY_SCHEMA = "s08-normalization-policy/1"
_COMPARISON_POLICY_SCHEMA = "s08-comparison-policy/1"
_READINESS_POLICY_SCHEMA = "s08-readiness-policy/1"
_OPERATORS_SCHEMA = "s08-operators/1"
_NORMALIZERS_SCHEMA = "s08-normalizers/1"
_INPUT_CONTRACT_SCHEMA = "s08-input-contract/1"
_LIMITS_SCHEMA = "s08-limits/1"


class ProtectedInvariantError(ValueError):
    """A protected-baseline invariant is violated across the checker and
    its comparison/waiver policy components.  Materialization fails closed;
    the validation suite classifies this as a protected failure."""

# Deterministic operators the pure checker may execute.  The governed
# manifest binds this registry so validation can refuse unknown operators.
_TARGET_OPERATORS = frozenset(
    {
        "all_equal",
        "multi_fuzzy_all",
        "multi_numeric_all",
        "list_contains",
        "pre_ocr_recheck",
    }
)


@dataclass(frozen=True)
class TargetEvidenceLink:
    document_id: str
    document_role: str
    field: str
    value_state: str
    raw_masked: str | None
    observation_id: str | None
    source_object_ref: str | None
    source_sha256: str | None
    provenance_manifest_digest: str | None
    source_page: int | str | None
    source_region: str | None
    evidence_eligible: bool
    eligibility_reason: str


@dataclass(frozen=True)
class TargetCheckResult:
    rule_id: str
    verdict: str
    severity: str
    reason_codes: tuple[str, ...]
    evidence_links: tuple[TargetEvidenceLink, ...]


@dataclass(frozen=True)
class TargetNormalizationOutcome:
    rule_id: str
    observation_id: str | None
    document_id: str
    document_role: str
    field: str
    normalized: str | None
    notes: tuple[str, ...]
    ocr_fix: bool
    pre_ocr: str | None


@dataclass(frozen=True)
class TargetSelectionOutcome:
    rule_id: str
    observation_id: str | None
    document_id: str
    document_role: str
    field: str
    selected: bool
    reason_code: str


@dataclass(frozen=True)
class TargetRunResult:
    application_id: str
    checks: tuple[TargetCheckResult, ...]
    normalization_outcomes: tuple[TargetNormalizationOutcome, ...] = ()
    selection_outcomes: tuple[TargetSelectionOutcome, ...] = ()


@dataclass(frozen=True)
class TargetRule:
    rule_id: str
    rule_type: str
    field: str | None
    document_roles: tuple[str, ...]
    on_missing: str
    severity: str
    threshold: float
    uncertain_band: float
    abs_tol: float
    rel_tol: float
    list_field: str | None
    item_field: str | None
    if_field_present: str | None
    required_field: str | None
    min_confidence: float
    require_all_docs: bool
    transfer_name_policy: str | None
    transfer_old_roles: tuple[str, ...]
    transfer_new_roles: tuple[str, ...]
    waivable: bool = False
    waiver_policy_id: str | None = None
    waiver_policy_digest: str | None = None
    waiver_reasons: tuple[str, ...] = ()
    waiver_scope: str | None = None
    waiver_ttl_seconds: int = 0

    @classmethod
    def compile(cls, source: RuleDef, default_require_all: bool | None) -> "TargetRule":
        require_all = source.require_all_docs
        if require_all is None:
            require_all = (
                default_require_all
                if default_require_all is not None
                else source.severity.lower() == "critical"
            )
        return cls(
            rule_id=source.id,
            rule_type=source.type.lower(),
            field=source.field,
            document_roles=tuple(source.docs),
            on_missing=source.on_missing.lower(),
            severity=source.severity.lower(),
            threshold=source.threshold,
            uncertain_band=source.uncertain_band,
            abs_tol=source.abs_tol,
            rel_tol=source.rel_tol,
            list_field=source.list_field,
            item_field=source.item_field,
            if_field_present=source.if_field_present,
            required_field=source.required_field,
            min_confidence=source.min_confidence,
            require_all_docs=bool(require_all),
            transfer_name_policy=source.transfer_name_policy,
            transfer_old_roles=tuple(
                source.transfer_old_docs or ("机动车登记证书",)
            ),
            transfer_new_roles=tuple(
                source.transfer_new_docs
                or ("交强险保单", "融资租赁合同", "身份证", "发票")
            ),
        )


@dataclass(frozen=True)
class TargetRelease:
    release_id: str
    release_digest: str
    checker_build: str
    rules_digest: str
    knowledge_digest: str
    normalizer_digest: str
    waiver_policy_id: str
    waiver_policy_digest: str
    knowledge: tuple[tuple[str, tuple[tuple[str, str], ...]], ...]
    aliases: tuple[tuple[str, tuple[str, ...]], ...]
    field_types: tuple[tuple[str, str], ...]
    rules: tuple[TargetRule, ...]
    low_confidence_threshold: float
    critical_low_conf_compare: bool
    date_order: str | None
    vin_fix_ioq: bool
    vin_strict_check_digit: bool
    expand_id15_to_18: bool
    limits: tuple[tuple[str, int], ...]

    @classmethod
    def compile(
        cls,
        config: RuleConfig,
        digest: str,
        *,
        knowledge: dict[str, Any],
    ) -> "TargetRelease":
        # Knowledge is always explicit: the process-global KB is not a
        # release authority and never participates in compilation.
        if not isinstance(knowledge, dict):
            raise ValueError(
                "compile requires explicit knowledge; the global KB is not a release authority"
            )
        from task4_consistency.normalize.address import _ALIAS_MAP

        sections = {
            "address_aliases": dict(knowledge.get("address_aliases") or {}),
            "builtin_address_aliases": dict(_ALIAS_MAP),
            "org_aliases": dict(knowledge.get("org_aliases") or {}),
            "plate_prefixes": dict(knowledge.get("plate_prefixes") or {}),
        }
        frozen_knowledge = tuple(
            (
                section,
                tuple(sorted((str(key), str(value)) for key, value in values.items())),
            )
            for section, values in sorted(sections.items())
        )
        knowledge_bytes = json.dumps(
            {
                "schema_version": "s01-target-knowledge/1",
                "sections": frozen_knowledge,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        knowledge_digest = hashlib.sha256(knowledge_bytes).hexdigest()
        compiled_rules = tuple(
            TargetRule.compile(rule, config.default_require_all_docs)
            for rule in config.rules
        )
        waiver_policy = {
            "schema_version": "c-demo-waiver-policy/1",
            "policy_id": _C_DEMO_WAIVER_POLICY_ID,
            "checks": [
                {
                    "rule_id": rule.rule_id,
                    "waivable": rule.rule_id == "R_BRAND_CROSS",
                    "allowed_reasons": (
                        [_C_DEMO_WAIVER_REASON]
                        if rule.rule_id == "R_BRAND_CROSS"
                        else []
                    ),
                    "scope": (
                        _C_DEMO_WAIVER_SCOPE
                        if rule.rule_id == "R_BRAND_CROSS"
                        else None
                    ),
                    "maximum_ttl_seconds": (
                        _C_DEMO_WAIVER_TTL_SECONDS
                        if rule.rule_id == "R_BRAND_CROSS"
                        else 0
                    ),
                }
                for rule in compiled_rules
            ],
        }
        waiver_bytes = json.dumps(
            waiver_policy,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        waiver_policy_digest = hashlib.sha256(waiver_bytes).hexdigest()
        compiled_rules = tuple(
            replace(
                rule,
                waivable=entry["waivable"],
                waiver_policy_id=_C_DEMO_WAIVER_POLICY_ID,
                waiver_policy_digest=waiver_policy_digest,
                waiver_reasons=tuple(entry["allowed_reasons"]),
                waiver_scope=entry["scope"],
                waiver_ttl_seconds=entry["maximum_ttl_seconds"],
            )
            for rule, entry in zip(compiled_rules, waiver_policy["checks"], strict=True)
        )
        if any(
            rule.waivable and rule.rule_id in _PROTECTED_WAIVER_CHECKS
            for rule in compiled_rules
        ):
            raise ValueError("protected-baseline checks cannot be waivable")
        governed_fields = {
            field
            for rule in compiled_rules
            for field in (
                rule.field,
                rule.list_field,
                rule.item_field,
                rule.if_field_present,
                rule.required_field,
            )
            if field
        }
        field_types = tuple(
            sorted((field, infer_field_type(field)) for field in governed_fields)
        )
        used_types = {field_type for _, field_type in field_types}
        normalizer_bytes = json.dumps(
            {
                "schema_version": "s01-target-normalizers/2",
                "field_types": field_types,
                "implementations": tuple(
                    (field_type, _TARGET_NORMALIZER_BUILDS.get(field_type, "generic/1"))
                    for field_type in sorted(used_types)
                ),
                "artifacts": _normalizer_artifact_manifest(used_types),
                "options": {
                    "date_order": config.date_order,
                    "vin_fix_ioq": config.vin_fix_ioq,
                    "vin_strict_check_digit": config.vin_strict_check_digit,
                    "expand_id15_to_18": config.expand_id15_to_18,
                },
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        normalizer_digest = hashlib.sha256(normalizer_bytes).hexdigest()
        checker_build = _TARGET_CHECKER_BUILD
        release_bytes = json.dumps(
            {
                "schema_version": "s01-target-release/6",
                "release_id": f"{config.package or 'rules'}@{config.version}",
                "rules_digest": digest,
                "knowledge_digest": knowledge_digest,
                "normalizer_digest": normalizer_digest,
                "waiver_policy_id": _C_DEMO_WAIVER_POLICY_ID,
                "waiver_policy_digest": waiver_policy_digest,
                "checker_build": checker_build,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return cls(
            release_id=f"{config.package or 'rules'}@{config.version}",
            release_digest=hashlib.sha256(release_bytes).hexdigest(),
            checker_build=checker_build,
            rules_digest=digest,
            knowledge_digest=knowledge_digest,
            normalizer_digest=normalizer_digest,
            waiver_policy_id=_C_DEMO_WAIVER_POLICY_ID,
            waiver_policy_digest=waiver_policy_digest,
            knowledge=frozen_knowledge,
            aliases=tuple(
                (canonical, tuple(names))
                for canonical, names in sorted(config.field_aliases.items())
            ),
            field_types=field_types,
            rules=compiled_rules,
            low_confidence_threshold=config.low_confidence_threshold,
            critical_low_conf_compare=config.critical_low_conf_compare,
            date_order=config.date_order,
            vin_fix_ioq=config.vin_fix_ioq,
            vin_strict_check_digit=config.vin_strict_check_digit,
            expand_id15_to_18=config.expand_id15_to_18,
            limits=(
                ("max_documents", 20),
                ("max_findings", 100),
                ("max_runtime_ms", 1000),
            ),
        )

    def public_manifest(self) -> dict[str, Any]:
        limits = dict(self.limits)
        return {
            "release_id": self.release_id,
            "digest": self.release_digest,
            "checker_build": self.checker_build,
            "rules_digest": self.rules_digest,
            "knowledge_digest": self.knowledge_digest,
            "normalizer_digest": self.normalizer_digest,
            "waiver_policy_id": self.waiver_policy_id,
            "waiver_policy_digest": self.waiver_policy_digest,
            "limits": limits,
            "applicable_check_ids": tuple(rule.rule_id for rule in self.rules),
            "applicable_check_count": len(self.rules),
        }

    def normalizer_manifest(self) -> dict[str, Any]:
        """The canonical normalization policy content.  Reproduces the exact
        bytes ``compile`` hashed so the governed artifact digest equals the
        compile-time ``normalizer_digest``."""
        used_types = {field_type for _, field_type in self.field_types}
        return {
            "schema_version": "s01-target-normalizers/2",
            "field_types": self.field_types,
            "implementations": tuple(
                (field_type, _TARGET_NORMALIZER_BUILDS.get(field_type, "generic/1"))
                for field_type in sorted(used_types)
            ),
            "artifacts": _normalizer_artifact_manifest(used_types),
            "options": {
                "date_order": self.date_order,
                "vin_fix_ioq": self.vin_fix_ioq,
                "vin_strict_check_digit": self.vin_strict_check_digit,
                "expand_id15_to_18": self.expand_id15_to_18,
            },
        }

    def waiver_policy(self) -> dict[str, Any]:
        """The canonical comparison/waiver policy content.  Reproduces the
        exact bytes ``compile`` hashed so the governed artifact digest equals
        the compile-time ``waiver_policy_digest``."""
        return {
            "schema_version": "c-demo-waiver-policy/1",
            "policy_id": self.waiver_policy_id,
            "checks": [
                {
                    "rule_id": rule.rule_id,
                    "waivable": rule.waivable,
                    "allowed_reasons": (
                        list(rule.waiver_reasons) if rule.waivable else []
                    ),
                    "scope": rule.waiver_scope if rule.waivable else None,
                    "maximum_ttl_seconds": (
                        rule.waiver_ttl_seconds if rule.waivable else 0
                    ),
                }
                for rule in self.rules
            ],
        }

    def operator_registry(self) -> dict[str, Any]:
        """The deterministic operator set this release may execute."""
        return {
            "schema_version": _OPERATORS_SCHEMA,
            "operators": sorted(_TARGET_OPERATORS),
        }

    def input_contract(self) -> dict[str, Any]:
        """The input semantic contract the checker accepts."""
        roles = sorted(
            {
                role
                for rule in self.rules
                for role in rule.document_roles
            }
        )
        return {
            "schema_version": _INPUT_CONTRACT_SCHEMA,
            "evidence_snapshot_schema": "s01-evidence-snapshot/1",
            "document_roles": roles,
        }

    def to_artifact(self) -> dict[str, Any]:
        """Canonical, content-addressed checker artifact.

        The artifact excludes ``release_digest``: ``from_artifact`` recomputes
        it from the same release identity material ``compile`` hashed, so a
        governed checker materialized from the Registry has the identical
        digest to the legacy compile-time release it replaces."""
        return {
            "schema_version": _TARGET_ARTIFACT_SCHEMA,
            "release_id": self.release_id,
            "rules_digest": self.rules_digest,
            "knowledge_digest": self.knowledge_digest,
            "normalizer_digest": self.normalizer_digest,
            "waiver_policy_id": self.waiver_policy_id,
            "waiver_policy_digest": self.waiver_policy_digest,
            "checker_build": self.checker_build,
            "knowledge": self.knowledge,
            "aliases": self.aliases,
            "field_types": self.field_types,
            "rules": tuple(_rule_to_dict(rule) for rule in self.rules),
            "low_confidence_threshold": self.low_confidence_threshold,
            "critical_low_conf_compare": self.critical_low_conf_compare,
            "date_order": self.date_order,
            "vin_fix_ioq": self.vin_fix_ioq,
            "vin_strict_check_digit": self.vin_strict_check_digit,
            "expand_id15_to_18": self.expand_id15_to_18,
            "limits": self.limits,
        }

    @classmethod
    def from_artifact(cls, artifact: dict[str, Any]) -> "TargetRelease":
        """Rebuild a checker release purely from Registry bytes.

        Recomputes the release digest, validates the schema/build/operator
        compatibility, and never touches files, network or the global KB."""
        if not isinstance(artifact, dict):
            raise ValueError("governed checker artifact must be an object")
        if artifact.get("schema_version") != _TARGET_ARTIFACT_SCHEMA:
            raise ValueError("governed checker artifact schema is not supported")
        checker_build = artifact.get("checker_build")
        if checker_build != _TARGET_CHECKER_BUILD:
            raise ValueError(
                f"governed checker build {checker_build!r} is not compatible"
            )
        identity = {
            "schema_version": _TARGET_RELEASE_IDENTITY_SCHEMA,
            "release_id": artifact.get("release_id"),
            "rules_digest": artifact.get("rules_digest"),
            "knowledge_digest": artifact.get("knowledge_digest"),
            "normalizer_digest": artifact.get("normalizer_digest"),
            "waiver_policy_id": artifact.get("waiver_policy_id"),
            "waiver_policy_digest": artifact.get("waiver_policy_digest"),
            "checker_build": checker_build,
        }
        release_digest = hashlib.sha256(
            json.dumps(
                identity, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        rules_data = artifact.get("rules")
        if not isinstance(rules_data, (list, tuple)) or not rules_data:
            raise ValueError("governed checker artifact has no compiled rules")
        rules = tuple(_rule_from_dict(item) for item in rules_data)
        knowledge_data = artifact.get("knowledge")
        aliases_data = artifact.get("aliases")
        field_types_data = artifact.get("field_types")
        limits_data = artifact.get("limits")
        if not all(
            isinstance(item, (list, tuple))
            for item in (
                knowledge_data,
                aliases_data,
                field_types_data,
                limits_data,
            )
        ):
            raise ValueError("governed checker artifact is structurally invalid")
        release = cls(
            release_id=str(artifact["release_id"]),
            release_digest=release_digest,
            checker_build=checker_build,
            rules_digest=str(artifact["rules_digest"]),
            knowledge_digest=str(artifact["knowledge_digest"]),
            normalizer_digest=str(artifact["normalizer_digest"]),
            waiver_policy_id=str(artifact["waiver_policy_id"]),
            waiver_policy_digest=str(artifact["waiver_policy_digest"]),
            knowledge=tuple(
                (str(section), tuple((str(k), str(v)) for k, v in values))
                for section, values in knowledge_data
            ),
            aliases=tuple(
                (str(canonical), tuple(str(name) for name in names))
                for canonical, names in aliases_data
            ),
            field_types=tuple(
                (str(field), str(field_type)) for field, field_type in field_types_data
            ),
            rules=rules,
            low_confidence_threshold=float(artifact["low_confidence_threshold"]),
            critical_low_conf_compare=bool(artifact["critical_low_conf_compare"]),
            date_order=artifact.get("date_order"),
            vin_fix_ioq=bool(artifact["vin_fix_ioq"]),
            vin_strict_check_digit=bool(artifact["vin_strict_check_digit"]),
            expand_id15_to_18=bool(artifact["expand_id15_to_18"]),
            limits=tuple((str(key), int(value)) for key, value in limits_data),
        )
        _verify_protected_invariants(release)
        return release

    def component_artifacts(self) -> list[dict[str, Any]]:
        """Canonical non-source components of this release, excluding the raw
        rules/KB source artifacts and the checker artifact itself.  Each entry
        is ``{"type", "content"}`` with a JSON-serializable content dict."""
        return [
            {
                "type": "semantic_catalog",
                "content": {
                    "schema_version": _SEMANTIC_CATALOG_SCHEMA,
                    "field_types": self.field_types,
                },
            },
            {
                "type": "normalization_policy",
                "content": self.normalizer_manifest(),
            },
            {"type": "comparison_policy", "content": self.waiver_policy()},
            {
                "type": "readiness_policy",
                "content": {
                    "schema_version": _READINESS_POLICY_SCHEMA,
                    "policy_id": "c-demo-readiness/1",
                },
            },
            {"type": "operators", "content": self.operator_registry()},
            {
                "type": "normalizers",
                "content": {
                    "schema_version": _NORMALIZERS_SCHEMA,
                    "normalizers": dict(sorted(_TARGET_NORMALIZER_BUILDS.items())),
                },
            },
            {"type": "input_contract", "content": self.input_contract()},
            {
                "type": "limits",
                "content": {"schema_version": _LIMITS_SCHEMA, "limits": dict(self.limits)},
            },
        ]


def _rule_to_dict(rule: TargetRule) -> dict[str, Any]:
    return {
        "rule_id": rule.rule_id,
        "rule_type": rule.rule_type,
        "field": rule.field,
        "document_roles": list(rule.document_roles),
        "on_missing": rule.on_missing,
        "severity": rule.severity,
        "threshold": rule.threshold,
        "uncertain_band": rule.uncertain_band,
        "abs_tol": rule.abs_tol,
        "rel_tol": rule.rel_tol,
        "list_field": rule.list_field,
        "item_field": rule.item_field,
        "if_field_present": rule.if_field_present,
        "required_field": rule.required_field,
        "min_confidence": rule.min_confidence,
        "require_all_docs": rule.require_all_docs,
        "transfer_name_policy": rule.transfer_name_policy,
        "transfer_old_roles": list(rule.transfer_old_roles),
        "transfer_new_roles": list(rule.transfer_new_roles),
        "waivable": rule.waivable,
        "waiver_policy_id": rule.waiver_policy_id,
        "waiver_policy_digest": rule.waiver_policy_digest,
        "waiver_reasons": list(rule.waiver_reasons),
        "waiver_scope": rule.waiver_scope,
        "waiver_ttl_seconds": rule.waiver_ttl_seconds,
    }


def _rule_from_dict(data: dict[str, Any]) -> TargetRule:
    return TargetRule(
        rule_id=str(data["rule_id"]),
        rule_type=str(data["rule_type"]),
        field=data.get("field"),
        document_roles=tuple(str(role) for role in data.get("document_roles", ())),
        on_missing=str(data["on_missing"]),
        severity=str(data["severity"]),
        threshold=float(data["threshold"]),
        uncertain_band=float(data["uncertain_band"]),
        abs_tol=float(data["abs_tol"]),
        rel_tol=float(data["rel_tol"]),
        list_field=data.get("list_field"),
        item_field=data.get("item_field"),
        if_field_present=data.get("if_field_present"),
        required_field=data.get("required_field"),
        min_confidence=float(data["min_confidence"]),
        require_all_docs=bool(data["require_all_docs"]),
        transfer_name_policy=data.get("transfer_name_policy"),
        transfer_old_roles=tuple(str(role) for role in data.get("transfer_old_roles", ())),
        transfer_new_roles=tuple(str(role) for role in data.get("transfer_new_roles", ())),
        waivable=bool(data.get("waivable", False)),
        waiver_policy_id=data.get("waiver_policy_id"),
        waiver_policy_digest=data.get("waiver_policy_digest"),
        waiver_reasons=tuple(str(reason) for reason in data.get("waiver_reasons", ())),
        waiver_scope=data.get("waiver_scope"),
        waiver_ttl_seconds=int(data.get("waiver_ttl_seconds", 0)),
    )


def _verify_protected_invariants(release: "TargetRelease") -> None:
    """Protected-baseline invariants across the checker rules and the
    comparison/waiver policy: every critical fingerprint rule must exist
    with its exact field/type/severity/require_all_docs/on_missing/docs
    semantics and must never be waivable."""
    from task4_consistency.rules.critical_guard import CRITICAL_FINGERPRINTS

    def semantic_digest(value: Any) -> str:
        return hashlib.sha256(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    knowledge = {
        "schema_version": "s01-target-knowledge/1",
        "sections": release.knowledge,
    }
    if semantic_digest(knowledge) != release.knowledge_digest:
        raise ProtectedInvariantError(
            "embedded knowledge does not match its declared digest"
        )
    if semantic_digest(release.normalizer_manifest()) != release.normalizer_digest:
        raise ProtectedInvariantError(
            "normalizer semantics do not match their declared digest"
        )
    if semantic_digest(release.waiver_policy()) != release.waiver_policy_digest:
        raise ProtectedInvariantError(
            "waiver semantics do not match their declared digest"
        )
    if any(
        rule.waiver_policy_id != release.waiver_policy_id
        or rule.waiver_policy_digest != release.waiver_policy_digest
        for rule in release.rules
    ):
        raise ProtectedInvariantError(
            "checker rules do not bind the declared waiver policy"
        )

    rule_ids = [rule.rule_id for rule in release.rules]
    if len(rule_ids) != len(set(rule_ids)):
        raise ProtectedInvariantError("checker rule identities must be unique")
    rules_by_id = {rule.rule_id: rule for rule in release.rules}
    for fingerprint in CRITICAL_FINGERPRINTS:
        rule = rules_by_id.get(fingerprint.rule_id)
        if rule is None:
            raise ProtectedInvariantError(
                f"protected rule {fingerprint.rule_id} is missing"
            )
        if rule.waivable:
            raise ProtectedInvariantError(
                f"protected rule {fingerprint.rule_id} must not be waivable"
            )
        if (
            rule.rule_type != fingerprint.type
            or rule.field != fingerprint.field
            or rule.severity != fingerprint.severity
        ):
            raise ProtectedInvariantError(
                f"{fingerprint.rule_id}: field/type/severity must be "
                f"{fingerprint.field!r}/{fingerprint.type!r}/"
                f"{fingerprint.severity!r}"
            )
        if rule.require_all_docs is not True:
            raise ProtectedInvariantError(
                f"{fingerprint.rule_id}: require_all_docs must be true"
            )
        if rule.on_missing not in fingerprint.on_missing_allowed:
            raise ProtectedInvariantError(
                f"{fingerprint.rule_id}: on_missing={rule.on_missing!r} not in "
                f"{sorted(fingerprint.on_missing_allowed)}"
            )
        if not fingerprint.docs_min.issubset(set(rule.document_roles)):
            raise ProtectedInvariantError(
                f"{fingerprint.rule_id}: docs must include "
                f"{sorted(fingerprint.docs_min)}"
            )


@dataclass(frozen=True)
class _ObservedValue:
    document_id: str
    document_role: str
    field: str
    raw: Any
    confidence: float
    normalized: str | None
    notes: tuple[str, ...]
    ocr_fix: bool
    pre_ocr: str | None
    observation_id: str | None
    source_object_ref: str | None
    source_sha256: str | None
    provenance_manifest_digest: str | None
    source_page: int | str | None
    source_region: str | None
    evidence_eligible: bool
    eligibility_reason: str


class TargetChecker:
    """Evaluate one frozen target RunSpec without I/O or current-state reads."""

    def __init__(self, release: TargetRelease) -> None:
        self._release = release
        self._aliases = {canonical: names for canonical, names in release.aliases}
        self._field_types = dict(release.field_types)
        self._knowledge = {
            section: dict(values) for section, values in release.knowledge
        }

    def run(self, run_spec: dict[str, Any]) -> TargetRunResult:
        self._validate_run_spec(run_spec)
        snapshot = run_spec["evidence_snapshot"]
        evidence = snapshot["evidence"]
        if len(evidence) > dict(self._release.limits)["max_documents"]:
            raise ValueError("frozen evidence exceeds target document limit")
        checks: list[TargetCheckResult] = []
        normalization_outcomes: list[TargetNormalizationOutcome] = []
        selection_outcomes: list[TargetSelectionOutcome] = []
        for rule in self._release.rules:
            checks.append(self._evaluate(rule, evidence))
            for value, in_scope in self._trace_rule_values(rule, evidence):
                normalization_outcomes.append(
                    TargetNormalizationOutcome(
                        rule_id=rule.rule_id,
                        observation_id=value.observation_id,
                        document_id=value.document_id,
                        document_role=value.document_role,
                        field=value.field,
                        normalized=value.normalized,
                        notes=value.notes,
                        ocr_fix=value.ocr_fix,
                        pre_ocr=value.pre_ocr,
                    )
                )
                if not in_scope:
                    selected = False
                    reason_code = "TRIGGER_ABSENT"
                elif value.raw in (None, ""):
                    selected = False
                    reason_code = "MISSING_VALUE"
                elif not value.evidence_eligible:
                    selected = False
                    reason_code = value.eligibility_reason
                elif value.normalized is None:
                    selected = False
                    reason_code = "NORMALIZE_FAIL"
                else:
                    selected = True
                    reason_code = "SELECTED_FOR_CHECK"
                selection_outcomes.append(
                    TargetSelectionOutcome(
                        rule_id=rule.rule_id,
                        observation_id=value.observation_id,
                        document_id=value.document_id,
                        document_role=value.document_role,
                        field=value.field,
                        selected=selected,
                        reason_code=reason_code,
                    )
                )
        return TargetRunResult(
            application_id=str(run_spec["application_id"]),
            checks=tuple(checks),
            normalization_outcomes=tuple(normalization_outcomes),
            selection_outcomes=tuple(selection_outcomes),
        )

    def _trace_rule_values(
        self, rule: TargetRule, evidence: list[dict[str, Any]]
    ) -> list[tuple[_ObservedValue, bool]]:
        if rule.rule_type == "conditional_required":
            condition = self._collect(
                evidence, rule.if_field_present or "", rule.document_roles
            )
            required = self._collect(
                evidence, rule.required_field or "", rule.document_roles
            )
            triggered = any(value.raw not in (None, "") for value in condition)
            return [
                *((value, True) for value in condition),
                *((value, triggered) for value in required),
            ]
        if rule.rule_type == "list_contains":
            containers = self._collect(
                evidence, rule.list_field or rule.field or "", rule.document_roles
            )
            items = self._collect(
                evidence, rule.item_field or "", rule.document_roles
            )
            return [(value, True) for value in (*containers, *items)]
        return [
            (value, True)
            for value in self._collect(evidence, rule.field or "", rule.document_roles)
        ]

    def _validate_run_spec(self, run_spec: dict[str, Any]) -> None:
        required = {
            "run_id",
            "application_id",
            "cycle",
            "lifecycle_revision",
            "evidence_snapshot_id",
            "evidence_snapshot_digest",
            "evidence_snapshot",
            "evidence_revision",
            "evidence_readiness_policy",
            "baseline_release",
            "release_id",
            "release_digest",
            "checker_build",
            "fence",
            "limits",
            "applicable_check_ids",
            "applicable_check_count",
        }
        if not isinstance(run_spec, dict):
            raise ValueError("RunSpec must be an object")
        missing = sorted(required.difference(run_spec))
        if missing:
            raise ValueError(f"RunSpec is incomplete: {', '.join(missing)}")
        for key in ("run_id", "application_id"):
            value = run_spec[key]
            if not isinstance(value, str) or not value:
                raise ValueError(f"RunSpec {key} must be a non-empty string")
        for key in (
            "cycle",
            "lifecycle_revision",
            "evidence_revision",
            "fence",
        ):
            value = run_spec[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"RunSpec {key} must be a positive integer")
        if run_spec["evidence_readiness_policy"] != "c-demo-readiness/1":
            raise ValueError("RunSpec evidence readiness policy does not match")

        manifest = self._release.public_manifest()
        expected_scalars = {
            "release_id": manifest["release_id"],
            "release_digest": manifest["digest"],
            "checker_build": manifest["checker_build"],
            "applicable_check_count": manifest["applicable_check_count"],
        }
        for key, expected in expected_scalars.items():
            if run_spec[key] != expected:
                raise ValueError(f"RunSpec {key} does not match frozen release")
        if run_spec["limits"] != manifest["limits"]:
            raise ValueError("RunSpec limits do not match frozen release")
        check_ids = run_spec["applicable_check_ids"]
        if not isinstance(check_ids, (list, tuple)) or tuple(check_ids) != tuple(
            manifest["applicable_check_ids"]
        ):
            raise ValueError("RunSpec applicable checks do not match frozen release")

        baseline = run_spec["baseline_release"]
        if not isinstance(baseline, dict):
            raise ValueError("RunSpec baseline release must be an object")
        expected_baseline = {
            "release_id": manifest["release_id"],
            "digest": manifest["digest"],
            "checker_build": manifest["checker_build"],
            "rules_digest": manifest["rules_digest"],
            "knowledge_digest": manifest["knowledge_digest"],
            "normalizer_digest": manifest["normalizer_digest"],
            "waiver_policy_id": manifest["waiver_policy_id"],
            "waiver_policy_digest": manifest["waiver_policy_digest"],
            "limits": manifest["limits"],
            "applicable_check_count": manifest["applicable_check_count"],
        }
        for key, expected in expected_baseline.items():
            if baseline.get(key) != expected:
                raise ValueError(
                    f"RunSpec baseline release {key} does not match frozen release"
                )
        baseline_check_ids = baseline.get("applicable_check_ids")
        if not isinstance(baseline_check_ids, (list, tuple)) or tuple(
            baseline_check_ids
        ) != tuple(manifest["applicable_check_ids"]):
            raise ValueError(
                "RunSpec baseline release checks do not match frozen release"
            )

        snapshot = run_spec["evidence_snapshot"]
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("schema_version") != "s01-evidence-snapshot/1"
            or not isinstance(snapshot.get("evidence"), list)
            or not snapshot["evidence"]
        ):
            raise ValueError("RunSpec evidence snapshot is invalid")
        snapshot_bytes = json.dumps(
            snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        snapshot_digest = hashlib.sha256(snapshot_bytes).hexdigest()
        if run_spec["evidence_snapshot_digest"] != snapshot_digest:
            raise ValueError("RunSpec evidence snapshot digest does not match")
        if run_spec["evidence_snapshot_id"] != f"snapshot_sha256_{snapshot_digest}":
            raise ValueError("RunSpec evidence snapshot identity does not match")

        if run_spec.get("policy_scope") is not None or run_spec.get(
            "activation_event_id"
        ) is not None:
            self._validate_governed_pin(run_spec)

    def _validate_governed_pin(self, run_spec: dict[str, Any]) -> None:
        """A governed RunSpec must pin one complete Registry release."""
        for key in (
            "policy_scope",
            "activation_event_id",
            "active_generation",
            "candidate_id",
            "manifest_id",
            "manifest_digest",
            "validation_bundle_id",
            "validation_bundle_digest",
            "approval_binding_id",
            "approval_binding_digest",
            "components",
        ):
            if key not in run_spec:
                raise ValueError(f"RunSpec governed pin is incomplete: {key}")
        if run_spec["policy_scope"] != "C-DEMO/demo":
            raise ValueError("RunSpec policy scope does not match the served scope")
        if (
            isinstance(run_spec["active_generation"], bool)
            or not isinstance(run_spec["active_generation"], int)
            or run_spec["active_generation"] < 1
        ):
            raise ValueError("RunSpec active generation is invalid")
        for key in (
            "activation_event_id",
            "candidate_id",
            "manifest_id",
            "validation_bundle_id",
            "approval_binding_id",
        ):
            if (
                not isinstance(run_spec[key], str)
                or not run_spec[key]
                or run_spec[key].strip() != run_spec[key]
            ):
                raise ValueError(f"RunSpec governed pin {key} is invalid")
        for key in ("manifest_digest", "validation_bundle_digest", "approval_binding_digest"):
            if (
                not isinstance(run_spec[key], str)
                or len(run_spec[key]) != 64
                or any(character not in "0123456789abcdef" for character in run_spec[key])
            ):
                raise ValueError(f"RunSpec governed pin {key} is invalid")
        components = run_spec["components"]
        if not isinstance(components, (list, tuple)) or not components:
            raise ValueError("RunSpec governed components are incomplete")
        types = [item.get("type") for item in components]
        if len(set(types)) != len(types):
            raise ValueError("RunSpec governed components are duplicated")
        for item in components:
            if not isinstance(item, dict) or not all(
                isinstance(item.get(field), str) and item[field]
                for field in ("type", "id", "digest")
            ):
                raise ValueError("RunSpec governed component is invalid")

    def _field_names(self, canonical: str) -> tuple[str, ...]:
        names = self._aliases.get(canonical)
        if names is None:
            return (canonical,)
        if canonical in names:
            return names
        return (canonical, *names)

    def _collect(
        self,
        evidence: list[dict[str, Any]],
        field: str,
        roles: tuple[str, ...],
    ) -> list[_ObservedValue]:
        names = self._field_names(field)
        values: list[_ObservedValue] = []
        for document in evidence:
            role = str(document["document_role"])
            if roles and role not in roles:
                continue
            fields = document["fields"]
            for selected in dict.fromkeys(name for name in names if name in fields):
                value = fields[selected]
                raw = value.get("raw")
                observation_id = value.get("observation_id")
                source_object_ref = value.get("source_object_ref")
                source_sha256 = value.get("source_sha256")
                provenance_manifest_digest = value.get("provenance_manifest_digest")
                source_page = value.get("source_page")
                source_region = value.get("source_region")
                provenance_complete = (
                    isinstance(observation_id, str)
                    and bool(observation_id)
                    and isinstance(source_object_ref, str)
                    and bool(source_object_ref)
                    and isinstance(source_sha256, str)
                    and bool(source_sha256)
                    and isinstance(provenance_manifest_digest, str)
                    and re.fullmatch(r"[0-9a-f]{64}", provenance_manifest_digest)
                    and source_page is not None
                    and isinstance(source_region, str)
                    and bool(source_region)
                )
                eligible = value.get("evidence_eligible") is True and provenance_complete
                eligibility_reason = str(
                    value.get("eligibility_reason")
                    if eligible
                    else "PROVENANCE_INELIGIBLE"
                )
                normalized = self._normalize(raw, field) if eligible else None
                values.append(
                    _ObservedValue(
                        document_id=str(document["document_id"]),
                        document_role=role,
                        field=selected,
                        raw=raw,
                        confidence=float(value.get("confidence", 1.0)),
                        normalized=normalized.value if normalized is not None else None,
                        notes=tuple(normalized.notes) if normalized is not None else (),
                        ocr_fix=normalized.ocr_fix if normalized is not None else False,
                        pre_ocr=normalized.pre_ocr if normalized is not None else None,
                        observation_id=str(observation_id) if observation_id else None,
                        source_object_ref=(
                            str(source_object_ref) if source_object_ref else None
                        ),
                        source_sha256=str(source_sha256) if source_sha256 else None,
                        provenance_manifest_digest=(
                            str(provenance_manifest_digest)
                            if provenance_manifest_digest
                            else None
                        ),
                        source_page=source_page,
                        source_region=str(source_region) if source_region else None,
                        evidence_eligible=eligible,
                        eligibility_reason=eligibility_reason,
                    )
                )
        return values

    @staticmethod
    def _apply_aliases(text: str, aliases: dict[str, str]) -> str:
        for key in sorted(aliases, key=len, reverse=True):
            if key in text:
                text = text.replace(key, aliases[key])
        return text

    def _normalize(self, raw: Any, field_name: str) -> NormalizeResult:
        if raw is None:
            return NormalizeResult(value=None)
        text_raw = str(raw).strip()
        if not text_raw:
            return NormalizeResult(value=None)
        field_type = self._field_types.get(field_name, "generic")
        if field_type == "vin":
            return normalize_vin_ex(
                text_raw,
                fix_ioq=self._release.vin_fix_ioq,
                validate=True,
                strict_check_digit=self._release.vin_strict_check_digit,
            )
        if field_type == "date":
            return normalize_date_ex(text_raw, date_order=self._release.date_order)
        if field_type == "money":
            return normalize_money_ex(text_raw)
        if field_type == "id_number":
            return normalize_id_number_ex(
                text_raw,
                validate=True,
                strict_checksum=True,
                expand_15_to_18=self._release.expand_id15_to_18,
            )
        if field_type == "brand":
            text = basic_clean(text_raw)
            text = re.sub(r"\s+", "", text)
            text = self._apply_aliases(text, self._knowledge["org_aliases"])
            text = text.replace("-", "").replace("—", "").replace("·", "")
            text = re.sub(r"[\(（][^)）]*[\)）]", "", text)
            for suffix in (
                "汽车工业有限公司",
                "汽车有限公司",
                "股份有限公司",
                "有限公司",
                "汽车",
                "牌",
            ):
                if text.endswith(suffix) and len(text) > len(suffix):
                    text = text[: -len(suffix)]
            if re.search(r"[A-Za-z]", text):
                text = text.upper()
            return NormalizeResult(value=text or None)
        if field_type == "address":
            text = basic_clean(text_raw)
            text = re.sub(r"\s+", "", text).replace("　", "").replace("中国", "")
            text = self._apply_aliases(
                text, self._knowledge["builtin_address_aliases"]
            )
            text = self._apply_aliases(text, self._knowledge["address_aliases"])
            text = text.replace("－", "-").replace("—", "-")
            return NormalizeResult(value=text or None)
        if field_type in {"engine", "reg_cert_no", "generic"} and is_placeholder_value(
            text_raw
        ):
            return NormalizeResult(value=None, notes=["placeholder_value"])
        normalizers = {
            "engine": normalize_engine,
            "generic": normalize_generic,
            "model": normalize_model,
            "person": normalize_person_name,
            "plate": normalize_plate,
            "plate_list": normalize_plate_list,
            "reg_cert_no": normalize_reg_cert_no,
        }
        normalized = normalizers.get(field_type, normalize_generic)(text_raw)
        return NormalizeResult(value=normalized)

    @staticmethod
    def _links(values: list[_ObservedValue]) -> tuple[TargetEvidenceLink, ...]:
        return tuple(
            TargetEvidenceLink(
                document_id=value.document_id,
                document_role=value.document_role,
                field=value.field,
                value_state="present" if value.raw not in (None, "") else "missing",
                raw_masked="[REDACTED]" if value.raw not in (None, "") else None,
                observation_id=value.observation_id,
                source_object_ref=value.source_object_ref,
                source_sha256=value.source_sha256,
                provenance_manifest_digest=value.provenance_manifest_digest,
                source_page=value.source_page,
                source_region=value.source_region,
                evidence_eligible=value.evidence_eligible,
                eligibility_reason=value.eligibility_reason,
            )
            for value in values
        )

    @staticmethod
    def _missing_verdict(rule: TargetRule) -> str:
        if rule.on_missing == "skip":
            return "skipped"
        if rule.on_missing == "inconsistent":
            return "inconsistent"
        return "uncertain"

    @staticmethod
    def _reason(rule_id: str, verdict: str, *, missing: bool = False) -> str:
        if verdict == "consistent":
            return "CONSISTENT"
        if verdict == "skipped":
            return "SKIPPED"
        if missing:
            return "MISSING_FIELD"
        return {
            "R_VIN_CROSS": "VIN_MISMATCH",
            "R_ENGINE_CROSS": "ENGINE_MISMATCH",
            "R_ID_EXACT": "ID_MISMATCH",
            "R_NAME_FUZZY": "NAME_MISMATCH",
            "R_PLATE_CROSS": "PLATE_MISMATCH",
            "R_AMOUNT_TOL": "AMOUNT_MISMATCH",
            "R_DATE_CROSS": "DATE_MISMATCH",
            "R_REG_CERT_CROSS": "REG_CERT_MISMATCH",
            "R_BRAND_CROSS": "BRAND_MISMATCH",
            "R_MODEL_CROSS": "MODEL_MISMATCH",
            "R_ADDRESS_FUZZY": "ADDRESS_MISMATCH",
            "R_PLATE_IN_LIST": "PLATE_NOT_IN_LIST",
            "R_ID_REQUIRED_IF_AMOUNT": "CONDITIONAL_REQUIRED_FAIL",
        }.get(rule_id, "MANDATORY_CHECK_FINDING")

    def _result(
        self,
        rule: TargetRule,
        verdict: str,
        values: list[_ObservedValue],
        *,
        missing: bool = False,
        reason: str | None = None,
        reasons: tuple[str, ...] | None = None,
    ) -> TargetCheckResult:
        return TargetCheckResult(
            rule_id=rule.rule_id,
            verdict=verdict,
            severity=rule.severity,
            reason_codes=reasons
            or (reason or self._reason(rule.rule_id, verdict, missing=missing),),
            evidence_links=self._links(values),
        )

    @staticmethod
    def _is_governed_name_transfer(
        rule: TargetRule, values: list[_ObservedValue]
    ) -> bool:
        if rule.transfer_name_policy != "uncertain":
            return False
        old_values = {
            value.normalized
            for value in values
            if value.document_role in rule.transfer_old_roles
            and value.normalized is not None
        }
        new_values = {
            value.normalized
            for value in values
            if value.document_role in rule.transfer_new_roles
            and value.normalized is not None
        }
        return len(old_values) == 1 and len(new_values) == 1 and old_values != new_values

    def _has_low_confidence(
        self, rule: TargetRule, values: list[_ObservedValue]
    ) -> bool:
        threshold = max(
            self._release.low_confidence_threshold, rule.min_confidence
        )
        return any(
            value.raw not in (None, "")
            and value.evidence_eligible
            and value.confidence < threshold
            for value in values
        )

    @staticmethod
    def _has_evidence_conflict(values: list[_ObservedValue]) -> bool:
        by_document: dict[str, set[str]] = {}
        for value in values:
            if (
                value.raw in (None, "")
                or not value.evidence_eligible
                or value.normalized is None
            ):
                continue
            by_document.setdefault(value.document_id, set()).add(value.normalized)
        return any(len(candidates) > 1 for candidates in by_document.values())

    def _evaluate(
        self, rule: TargetRule, evidence: list[dict[str, Any]]
    ) -> TargetCheckResult:
        if rule.rule_type == "conditional_required":
            return self._conditional(rule, evidence)
        if rule.rule_type == "list_contains":
            return self._list_contains(rule, evidence)
        field = rule.field or ""
        values = self._collect(evidence, field, rule.document_roles)
        if any(
            value.raw not in (None, "") and not value.evidence_eligible
            for value in values
        ):
            return self._result(
                rule, "uncertain", values, reason="PROVENANCE_INELIGIBLE"
            )
        present = [
            value
            for value in values
            if value.raw not in (None, "") and value.evidence_eligible
        ]
        if rule.require_all_docs and rule.document_roles:
            present_roles = {value.document_role for value in present}
            if any(role not in present_roles for role in rule.document_roles):
                verdict = self._missing_verdict(rule)
                return self._result(rule, verdict, values, missing=True, reason="MISSING_DOCS")
        if len(present) < 2:
            verdict = self._missing_verdict(rule)
            return self._result(rule, verdict, values, missing=True)
        if any(value.normalized is None for value in present):
            return self._result(rule, "uncertain", values, reason="NORMALIZE_FAIL")
        normalized = [value.normalized for value in present]
        if self._has_evidence_conflict(present):
            return self._result(rule, "uncertain", values, reason="EVIDENCE_CONFLICT")
        if rule.rule_type in {"exact", "numeric_tolerance"} and any(
            "money_approx" in value.notes for value in present
        ):
            return self._result(rule, "uncertain", values, reason="AMOUNT_APPROX")
        if self._has_low_confidence(rule, present):
            if (
                self._release.critical_low_conf_compare
                and rule.severity == "critical"
                and rule.rule_type == "exact"
                and not all_equal(normalized)
            ):
                return self._result(rule, "inconsistent", values)
            return self._result(rule, "uncertain", values, reason="LOW_CONF")

        if rule.rule_type == "exact":
            exact_match = all_equal(normalized)
            if exact_match and any(value.ocr_fix for value in present):
                pre_ocr = [value.pre_ocr or value.normalized or "" for value in present]
                if not all_equal(pre_ocr):
                    return self._result(
                        rule,
                        "uncertain",
                        values,
                        reason="VIN_OCR_FIX_MERGE",
                    )
            verdict = "consistent" if exact_match else "inconsistent"
            return self._result(rule, verdict, values)
        if rule.rule_type == "fuzzy":
            outcome = multi_fuzzy_all(
                normalized,
                threshold=rule.threshold,
                uncertain_band=rule.uncertain_band,
            )
            if not outcome.match and self._is_governed_name_transfer(rule, present):
                return self._result(
                    rule,
                    "uncertain",
                    values,
                    reasons=("USED_CAR_NAME_TRANSFER", "NAME_NEAR_UNCERTAIN"),
                )
            verdict = (
                "consistent"
                if outcome.match
                else "uncertain"
                if outcome.uncertain
                else "inconsistent"
            )
            reason = "NAME_NEAR_UNCERTAIN" if verdict == "uncertain" else None
            return self._result(rule, verdict, values, reason=reason)
        if rule.rule_type == "numeric_tolerance":
            outcome = multi_numeric_all(
                normalized,
                abs_tol=rule.abs_tol,
                rel_tol=rule.rel_tol,
            )
            verdict = "consistent" if outcome.match else "inconsistent"
            return self._result(rule, verdict, values)
        return self._result(rule, "uncertain", values, reason="UNKNOWN_CHECK_TYPE")

    def _conditional(
        self, rule: TargetRule, evidence: list[dict[str, Any]]
    ) -> TargetCheckResult:
        condition = self._collect(
            evidence, rule.if_field_present or "", rule.document_roles
        )
        required = self._collect(
            evidence, rule.required_field or "", rule.document_roles
        )
        values = [*condition, *required]
        if not any(value.raw not in (None, "") for value in condition):
            return self._result(rule, "consistent", values)
        if any(
            value.raw not in (None, "") and not value.evidence_eligible
            for value in values
        ):
            return self._result(
                rule, "uncertain", values, reason="PROVENANCE_INELIGIBLE"
            )
        if self._has_low_confidence(rule, values):
            return self._result(rule, "uncertain", values, reason="LOW_CONF")
        if any(
            value.raw not in (None, "") and value.normalized is not None
            for value in required
        ):
            return self._result(rule, "consistent", values)
        if any(value.raw not in (None, "") for value in required):
            return self._result(rule, "uncertain", values, reason="NORMALIZE_FAIL")
        return self._result(rule, "inconsistent", values)

    def _list_contains(
        self, rule: TargetRule, evidence: list[dict[str, Any]]
    ) -> TargetCheckResult:
        list_field = rule.list_field or rule.field or ""
        item_field = rule.item_field or ""
        containers = self._collect(evidence, list_field, rule.document_roles)
        items = self._collect(evidence, item_field, rule.document_roles)
        values = [*containers, *items]
        if any(
            value.raw not in (None, "") and not value.evidence_eligible
            for value in values
        ):
            return self._result(
                rule, "uncertain", values, reason="PROVENANCE_INELIGIBLE"
            )
        if self._has_low_confidence(rule, values):
            return self._result(rule, "uncertain", values, reason="LOW_CONF")
        present_containers = [value for value in containers if value.raw not in (None, "")]
        present_items = [value for value in items if value.raw not in (None, "")]
        if not present_containers or not present_items:
            return self._result(
                rule,
                self._missing_verdict(rule),
                values,
                missing=True,
            )
        if any(container.normalized is None for container in present_containers):
            return self._result(rule, "uncertain", values, reason="NORMALIZE_FAIL")
        if any(item.normalized is None for item in present_items):
            return self._result(rule, "uncertain", values, reason="NORMALIZE_FAIL")

        def normalize_item(raw: str) -> str | None:
            return self._normalize(raw, item_field).value

        matches = all(
            any(
                list_contains(
                    container.raw,
                    item.normalized,
                    normalize_item=normalize_item,
                ).match
                for container in present_containers
            )
            for item in present_items
        )
        return self._result(rule, "consistent" if matches else "inconsistent", values)


def _normalizer_artifact_manifest(used_types: set[str]) -> dict[str, Any]:
    implementations: dict[str, tuple[Any, ...]] = {
        "address": (basic_clean, TargetChecker._apply_aliases),
        "brand": (basic_clean, TargetChecker._apply_aliases),
        "date": (normalize_date_ex,),
        "engine": (normalize_engine, is_placeholder_value),
        "generic": (normalize_generic, is_placeholder_value),
        "id_number": (normalize_id_number_ex,),
        "model": (normalize_model,),
        "money": (normalize_money_ex,),
        "person": (normalize_person_name,),
        "plate": (normalize_plate,),
        "plate_list": (normalize_plate_list,),
        "reg_cert_no": (normalize_reg_cert_no, is_placeholder_value),
        "vin": (normalize_vin_ex,),
    }
    selected: dict[str, Any] = {
        f"{artifact.__module__}.{artifact.__qualname__}": artifact
        for artifact in (TargetChecker._normalize, infer_field_type, NormalizeResult)
    }
    for field_type in sorted(used_types):
        for artifact in implementations.get(field_type, (normalize_generic,)):
            selected[f"{artifact.__module__}.{artifact.__qualname__}"] = artifact

    modules: dict[str, str] = {}
    callables: list[tuple[str, str]] = []
    for identity, artifact in sorted(selected.items()):
        source_path = inspect.getsourcefile(artifact)
        if source_path is None:
            raise RuntimeError(f"normalizer artifact source is unavailable: {identity}")
        modules[artifact.__module__] = hashlib.sha256(
            Path(source_path).read_bytes()
        ).hexdigest()
        callable_source = inspect.getsource(artifact).encode("utf-8")
        callables.append((identity, hashlib.sha256(callable_source).hexdigest()))
    return {
        "schema_version": "s01-normalizer-artifacts/1",
        "modules": tuple(sorted(modules.items())),
        "callables": tuple(callables),
    }
