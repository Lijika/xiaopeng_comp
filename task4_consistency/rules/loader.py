"""YAML rule configuration loader with basic schema validation."""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any

import yaml

_ALLOWED_TYPES = {
    "exact",
    "fuzzy",
    "numeric_tolerance",
    "list_contains",
    "conditional_required",
}
_ALLOWED_ON_MISSING = {"uncertain", "skip", "inconsistent"}
_ALLOWED_SEVERITY = {"critical", "major", "minor", "info"}

# ADV-W2: runtime package must keep critical identity coverage
_REQUIRED_CRITICAL_FIELDS = frozenset({"vin", "engine_no", "id_number"})
# ADV-W4: finance-safe upper bound on relative tolerance
_MAX_REL_TOL = 0.05  # 5%
_MAX_ABS_TOL = 1_000_000.0
# ADV-W7: reg_date must not absorb contract signing day
_REG_DATE_FORBIDDEN_ALIASES = frozenset(
    {"contract_date", "sign_date", "合同日期", "签署日期", "签订日期"}
)
_CONTRACT_DATE_FORBIDDEN_ALIASES = frozenset(
    {"reg_date", "issue_date", "登记日期", "注册日期"}
)


@dataclass
class RuleDef:
    id: str
    name: str
    type: str
    field: str | None = None
    docs: list[str] = dc_field(default_factory=list)
    on_missing: str = "uncertain"  # uncertain | skip | inconsistent
    severity: str = "major"
    # fuzzy
    threshold: float = 0.88
    uncertain_band: float = 0.05
    # numeric
    abs_tol: float = 0.0
    rel_tol: float = 0.0
    # list
    list_field: str | None = None
    item_field: str | None = None
    # conditional
    if_field_present: str | None = None
    required_field: str | None = None
    # confidence gate
    min_confidence: float = 0.0
    # docs completeness
    require_all_docs: bool | None = None
    # used-car transfer: when reg owner ≠ party names → uncertain|inconsistent|null
    transfer_name_policy: str | None = None
    transfer_old_docs: list[str] = dc_field(default_factory=list)
    transfer_new_docs: list[str] = dc_field(default_factory=list)
    # extra
    field_type: str | None = None
    extra: dict[str, Any] = dc_field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, index: int = 0) -> "RuleDef":
        if not isinstance(data, dict):
            raise ValueError(f"rules[{index}] must be a mapping")
        if "id" not in data or data["id"] in (None, ""):
            raise ValueError(f"rules[{index}] missing required key 'id'")
        if "type" not in data or data["type"] in (None, ""):
            raise ValueError(f"rules[{index}] id={data.get('id')!r} missing required key 'type'")

        rtype = str(data["type"]).lower()
        if rtype not in _ALLOWED_TYPES:
            raise ValueError(
                f"rules[{index}] id={data['id']!r} unknown type {data['type']!r}; "
                f"allowed={sorted(_ALLOWED_TYPES)}"
            )

        on_missing = str(data.get("on_missing") or "uncertain").lower()
        if on_missing not in _ALLOWED_ON_MISSING:
            raise ValueError(
                f"rules[{index}] id={data['id']!r} invalid on_missing={on_missing!r}"
            )

        severity = str(data.get("severity") or "major").lower()
        if severity not in _ALLOWED_SEVERITY:
            raise ValueError(
                f"rules[{index}] id={data['id']!r} invalid severity={severity!r}"
            )

        if rtype in {"exact", "fuzzy", "numeric_tolerance"} and not data.get("field"):
            raise ValueError(
                f"rules[{index}] id={data['id']!r} type={rtype} requires 'field'"
            )
        if rtype == "list_contains":
            if not (data.get("list_field") or data.get("field")):
                raise ValueError(
                    f"rules[{index}] id={data['id']!r} list_contains requires list_field"
                )
            if not data.get("item_field"):
                raise ValueError(
                    f"rules[{index}] id={data['id']!r} list_contains requires item_field"
                )
        if rtype == "conditional_required":
            if not data.get("if_field_present") or not data.get("required_field"):
                raise ValueError(
                    f"rules[{index}] id={data['id']!r} conditional_required requires "
                    "if_field_present and required_field"
                )

        known = {
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
        extra = {k: v for k, v in data.items() if k not in known}
        req_all = data.get("require_all_docs")
        if req_all is not None:
            req_all = bool(req_all)
        tnp = data.get("transfer_name_policy")
        if tnp is not None:
            tnp = str(tnp).lower()
            if tnp not in {"uncertain", "inconsistent", "off", "none"}:
                raise ValueError(
                    f"rules[{index}] id={data['id']!r} invalid transfer_name_policy={tnp!r}"
                )
            if tnp in {"off", "none"}:
                tnp = None

        return cls(
            id=str(data["id"]),
            name=str(data.get("name") or data["id"]),
            type=rtype,
            field=data.get("field"),
            docs=list(data.get("docs") or []),
            on_missing=on_missing,
            severity=severity,
            threshold=float(data.get("threshold", 0.88)),
            uncertain_band=float(data.get("uncertain_band", 0.05)),
            abs_tol=float(data.get("abs_tol", 0.0)),
            rel_tol=float(data.get("rel_tol", 0.0)),
            list_field=data.get("list_field"),
            item_field=data.get("item_field"),
            if_field_present=data.get("if_field_present"),
            required_field=data.get("required_field"),
            min_confidence=float(data.get("min_confidence", 0.0)),
            require_all_docs=req_all,
            transfer_name_policy=tnp,
            transfer_old_docs=list(data.get("transfer_old_docs") or []),
            transfer_new_docs=list(data.get("transfer_new_docs") or []),
            field_type=data.get("field_type"),
            extra=extra,
        )


def validate_rule_package_policy(
    rules: list[RuleDef],
    field_aliases: dict[str, list[str]],
) -> None:
    """ADV-W2/W4/W7/W9 package guards (raise ValueError)."""
    if not rules:
        raise ValueError("rules list empty — refuse empty package (ADV-W8)")

    # ADV-W7 field alias semantic collapse
    reg_al = {str(x) for x in (field_aliases.get("reg_date") or [])}
    con_al = {str(x) for x in (field_aliases.get("contract_date") or [])}
    bad_reg = reg_al & _REG_DATE_FORBIDDEN_ALIASES
    if bad_reg:
        raise ValueError(
            f"field_aliases.reg_date must not include contract-day names {sorted(bad_reg)} (ADV-W7)"
        )
    bad_con = con_al & _CONTRACT_DATE_FORBIDDEN_ALIASES
    if bad_con:
        raise ValueError(
            f"field_aliases.contract_date must not include reg-day names {sorted(bad_con)} (ADV-W7)"
        )

    covered: set[str] = set()
    for r in rules:
        # ADV-W4 tol caps
        if r.rel_tol < 0:
            raise ValueError(f"rules id={r.id!r} rel_tol must be >= 0")
        if r.rel_tol > _MAX_REL_TOL:
            raise ValueError(
                f"rules id={r.id!r} rel_tol={r.rel_tol} exceeds max {_MAX_REL_TOL} (ADV-W4)"
            )
        if r.abs_tol < 0:
            raise ValueError(f"rules id={r.id!r} abs_tol must be >= 0")
        if r.abs_tol > _MAX_ABS_TOL:
            raise ValueError(
                f"rules id={r.id!r} abs_tol={r.abs_tol} exceeds max {_MAX_ABS_TOL} (ADV-W4)"
            )

        field = (r.field or "").strip()
        # ADV-W9 / Round16 fingerprint: identity fields cannot demote
        if field in _REQUIRED_CRITICAL_FIELDS:
            from task4_consistency.rules.critical_guard import CriticalGuardError

            if r.severity == "info":
                raise CriticalGuardError(
                    "critical_semantic_tamper",
                    f"rules id={r.id!r} field={field!r} cannot use severity=info (ADV-W9)",
                )
            if r.on_missing == "skip":
                raise CriticalGuardError(
                    "critical_on_missing_skip",
                    f"rules id={r.id!r} field={field!r} cannot use on_missing=skip",
                )
            if r.severity == "critical" and r.on_missing != "skip":
                covered.add(field)

    # ADV-W2 / Round16: must keep critical coverage for VIN/engine/id
    missing = _REQUIRED_CRITICAL_FIELDS - covered
    if missing:
        from task4_consistency.rules.critical_guard import CriticalGuardError

        raise CriticalGuardError(
            "critical_rule_missing",
            f"package missing severity=critical rules for fields {sorted(missing)} "
            f"(cannot drop critical identity rules; ADV-W2)",
        )


@dataclass
class RuleConfig:
    version: str | int | None
    field_aliases: dict[str, list[str]]
    rules: list[RuleDef]
    package: str | None = None
    changelog: list[str] = dc_field(default_factory=list)
    low_confidence_threshold: float = 0.6
    # None = auto (critical rules require all docs); True/False override
    default_require_all_docs: bool | None = None
    # Date slash order when both day/month ≤12: DMY | MDY | None(ambiguous→uncertain)
    date_order: str | None = None
    # VIN I/O/Q OCR fix (default True); when fix merges different raw → engine uncertain
    vin_fix_ioq: bool = True
    # Optional ISO 3779 check digit (default off — synthetic fixtures often invalid)
    vin_strict_check_digit: bool = False
    # Expand 15-digit Chinese ID to 18 with century 19 for cross-doc link
    expand_id15_to_18: bool = True
    # ADV-05: when low conf but norms clearly mismatch on critical rules
    # true → emit inconsistent + flags=[low_conf]; false → always uncertain
    critical_low_conf_compare: bool = True
    raw: dict[str, Any] = dc_field(default_factory=dict)

    def alias_map(self) -> dict[str, str]:
        """Map any alias field name -> canonical field name."""
        mapping: dict[str, str] = {}
        for canonical, aliases in self.field_aliases.items():
            mapping[canonical] = canonical
            for a in aliases:
                mapping[a] = canonical
        return mapping

    def resolve_field_names(self, canonical: str) -> list[str]:
        """All names that map to this canonical field."""
        if canonical in self.field_aliases:
            names = list(self.field_aliases[canonical])
            if canonical not in names:
                names.insert(0, canonical)
            return names
        # reverse: if given an alias, expand
        for can, aliases in self.field_aliases.items():
            if canonical in aliases or canonical == can:
                names = list(aliases)
                if can not in names:
                    names.insert(0, can)
                return names
        return [canonical]


def load_rules(path: str | Path) -> RuleConfig:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Rule config not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Rule config must be a mapping: {path}")

    aliases = data.get("field_aliases") or {}
    if not isinstance(aliases, dict):
        raise ValueError("field_aliases must be a mapping")
    aliases = {str(k): [str(x) for x in (v or [])] for k, v in aliases.items()}

    rules_raw = data.get("rules")
    if not rules_raw:
        raise ValueError(f"Rule config has empty/missing rules: {path}")
    if not isinstance(rules_raw, list):
        raise ValueError("rules must be a list")

    rules = [RuleDef.from_dict(r, index=i) for i, r in enumerate(rules_raw)]
    ids = [r.id for r in rules]
    if len(ids) != len(set(ids)):
        raise ValueError(f"Duplicate rule ids in {path}: {ids}")

    default_req = data.get("default_require_all_docs", None)
    if default_req is not None:
        default_req = bool(default_req)

    date_order = data.get("date_order")
    if date_order is not None:
        date_order = str(date_order).upper()
        if date_order not in {"DMY", "MDY"}:
            raise ValueError("date_order must be DMY, MDY, or null")

    changelog = data.get("changelog") or []
    if isinstance(changelog, str):
        changelog = [changelog]
    if not isinstance(changelog, list):
        raise ValueError("changelog must be a list of strings")

    validate_rule_package_policy(rules, aliases)

    cfg = RuleConfig(
        version=data.get("version"),
        field_aliases=aliases,
        rules=rules,
        package=str(data["package"]) if data.get("package") is not None else None,
        changelog=[str(x) for x in changelog],
        low_confidence_threshold=float(data.get("low_confidence_threshold", 0.6)),
        default_require_all_docs=default_req,
        date_order=date_order,
        vin_fix_ioq=bool(data.get("vin_fix_ioq", True)),
        vin_strict_check_digit=bool(data.get("vin_strict_check_digit", False)),
        expand_id15_to_18=bool(data.get("expand_id15_to_18", True)),
        critical_low_conf_compare=bool(data.get("critical_low_conf_compare", True)),
        raw=data,
    )
    # ARCH Round16: critical semantic fingerprints (code authority)
    from task4_consistency.rules.critical_guard import enforce_critical_fingerprints

    enforce_critical_fingerprints(cfg)
    return cfg
