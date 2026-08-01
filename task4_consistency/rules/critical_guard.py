"""Critical rule semantic fingerprints (ARCH Round16 final).

Authority lives in code — not YAML flags. Save/load must enforce before any
runtime write. No break-glass in MVP.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    from task4_consistency.rules.loader import RuleConfig, RuleDef


@dataclass(frozen=True)
class CriticalFingerprint:
    rule_id: str
    field: str
    type: str
    severity: str
    require_all_docs: bool
    on_missing_allowed: frozenset[str]
    docs_min: frozenset[str]


# 终裁表 — 唯一权威
CRITICAL_FINGERPRINTS: tuple[CriticalFingerprint, ...] = (
    CriticalFingerprint(
        rule_id="R_VIN_CROSS",
        field="vin",
        type="exact",
        severity="critical",
        require_all_docs=True,
        on_missing_allowed=frozenset({"uncertain", "inconsistent"}),
        docs_min=frozenset({"机动车登记证书", "融资租赁合同"}),
    ),
    CriticalFingerprint(
        rule_id="R_ENGINE_CROSS",
        field="engine_no",
        type="exact",
        severity="critical",
        require_all_docs=True,
        on_missing_allowed=frozenset({"uncertain", "inconsistent"}),
        docs_min=frozenset({"机动车登记证书"}),
    ),
    CriticalFingerprint(
        rule_id="R_ID_EXACT",
        field="id_number",
        type="exact",
        severity="critical",
        require_all_docs=True,
        on_missing_allowed=frozenset({"uncertain", "inconsistent"}),
        docs_min=frozenset({"融资租赁合同", "身份证"}),
    ),
)

CRITICAL_RULE_IDS: frozenset[str] = frozenset(fp.rule_id for fp in CRITICAL_FINGERPRINTS)


class CriticalGuardError(ValueError):
    """Raised when critical fingerprint check fails."""

    def __init__(self, error: str, message: str):
        self.error = error
        super().__init__(message)


def _index_rules(rules: Iterable["RuleDef"]) -> dict[str, list["RuleDef"]]:
    out: dict[str, list[RuleDef]] = {}
    for r in rules:
        out.setdefault(r.id, []).append(r)
    return out


def enforce_critical_fingerprints(cfg: "RuleConfig") -> None:
    """Enforce CRITICAL_FINGERPRINTS on loaded RuleConfig. Raise CriticalGuardError."""
    by_id = _index_rules(cfg.rules)

    for fp in CRITICAL_FINGERPRINTS:
        rows = by_id.get(fp.rule_id) or []
        if not rows:
            raise CriticalGuardError(
                "critical_rule_missing",
                f"missing critical rule id={fp.rule_id!r}",
            )
        if len(rows) > 1:
            raise CriticalGuardError(
                "critical_semantic_tamper",
                f"duplicate critical rule id={fp.rule_id!r} (count={len(rows)})",
            )
        r = rows[0]

        if (r.field or "") != fp.field or (r.type or "") != fp.type or (r.severity or "") != fp.severity:
            raise CriticalGuardError(
                "critical_semantic_tamper",
                f"{fp.rule_id}: field/type/severity must be "
                f"{fp.field!r}/{fp.type!r}/{fp.severity!r}, got "
                f"{r.field!r}/{r.type!r}/{r.severity!r}",
            )

        # require_all_docs must be explicit True (None/False fail)
        if r.require_all_docs is not True:
            raise CriticalGuardError(
                "critical_semantic_tamper",
                f"{fp.rule_id}: require_all_docs must be true, got {r.require_all_docs!r}",
            )

        on_m = (r.on_missing or "uncertain").lower()
        if on_m == "skip":
            raise CriticalGuardError(
                "critical_on_missing_skip",
                f"{fp.rule_id}: on_missing=skip forbidden on critical identity rule",
            )
        if on_m not in fp.on_missing_allowed:
            raise CriticalGuardError(
                "critical_semantic_tamper",
                f"{fp.rule_id}: on_missing={on_m!r} not in {sorted(fp.on_missing_allowed)}",
            )

        docs = {str(d) for d in (r.docs or [])}
        if not fp.docs_min.issubset(docs):
            missing = sorted(fp.docs_min - docs)
            raise CriticalGuardError(
                "critical_docs_stripped",
                f"{fp.rule_id}: docs must ⊇ {sorted(fp.docs_min)}; missing {missing}",
            )


def fingerprints_as_dicts() -> list[dict[str, Any]]:
    """For docs / API meta."""
    return [
        {
            "rule_id": fp.rule_id,
            "field": fp.field,
            "type": fp.type,
            "severity": fp.severity,
            "require_all_docs": fp.require_all_docs,
            "on_missing_allowed": sorted(fp.on_missing_allowed),
            "docs_min": sorted(fp.docs_min),
        }
        for fp in CRITICAL_FINGERPRINTS
    ]
