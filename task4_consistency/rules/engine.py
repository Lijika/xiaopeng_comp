"""Rule engine: normalize fields then apply configured checks."""

from __future__ import annotations

from task4_consistency.match.exact import all_equal
from task4_consistency.match.fuzzy import multi_fuzzy_all
from task4_consistency.match.list_ops import as_list, list_contains
from task4_consistency.match.numeric import multi_numeric_all
from task4_consistency.models import (
    Application,
    CheckResult,
    DiffHighlight,
    Document,
    FieldSnapshot,
    FieldValue,
    Report,
    Severity,
    Verdict,
)
from task4_consistency.normalize.base import normalize_field, normalize_field_ex
from task4_consistency.normalize.vin import vin_edit_distance
from task4_consistency.report import build_report, first_diff
from task4_consistency.rules.loader import RuleConfig, RuleDef

# type -> handler name on RuleEngine (strategy registry; ARCH_DEBATE P1)
_HANDLER_REGISTRY: dict[str, str] = {
    "exact": "_eval_cross_field",
    "fuzzy": "_eval_cross_field",
    "numeric_tolerance": "_eval_cross_field",
    "list_contains": "_eval_list_contains",
    "conditional_required": "_eval_conditional",
}


def register_rule_handler(rule_type: str, method_name: str) -> None:
    """In-process plugin registration (whitelist types only via loader)."""
    _HANDLER_REGISTRY[rule_type.lower()] = method_name


class RuleEngine:
    def __init__(self, config: RuleConfig):
        self.config = config

    def run(self, application: Application) -> Report:
        app = self._normalize_application(application)
        checks: list[CheckResult] = []
        for rule in self.config.rules:
            checks.append(self._eval_rule(app, rule))
        return build_report(
            application_id=app.application_id,
            checks=checks,
            rule_config_version=self.config.version,
            rule_package=getattr(self.config, "package", None),
            rule_changelog=list(getattr(self.config, "changelog", None) or []),
        )

    def _normalize_application(self, application: Application) -> Application:
        docs: list[Document] = []
        date_order = getattr(self.config, "date_order", None)
        vin_fix_ioq = getattr(self.config, "vin_fix_ioq", True)
        vin_strict_check_digit = getattr(self.config, "vin_strict_check_digit", False)
        expand_id15 = getattr(self.config, "expand_id15_to_18", True)
        for doc in application.documents:
            fields: dict[str, FieldValue] = {}
            for name, fv in doc.fields.items():
                nr = normalize_field_ex(
                    fv.raw,
                    field_name=name,
                    field_type=fv.field_type,
                    date_order=date_order,
                    vin_fix_ioq=vin_fix_ioq,
                    vin_strict_check_digit=vin_strict_check_digit,
                    expand_id15_to_18=expand_id15,
                )
                nf = FieldValue(
                    raw=fv.raw,
                    confidence=fv.confidence,
                    source_page=fv.source_page,
                    field_type=fv.field_type,
                    normalized=nr.value,
                    ocr_fix=nr.ocr_fix,
                    pre_ocr=nr.pre_ocr,
                    notes=list(nr.notes),
                )
                fields[name] = nf
            docs.append(Document(doc_id=doc.doc_id, doc_type=doc.doc_type, fields=fields))
        return Application(
            application_id=application.application_id,
            documents=docs,
            meta=dict(application.meta),
        )

    def _resolve_field_on_doc(self, doc: Document, field: str) -> tuple[str, FieldValue] | None:
        names = self.config.resolve_field_names(field)
        for n in names:
            if n in doc.fields:
                return n, doc.fields[n]
        return None

    def _collect_field_snapshots(
        self,
        app: Application,
        field: str,
        docs_filter: list[str],
    ) -> list[tuple[Document, str, FieldValue]]:
        collected: list[tuple[Document, str, FieldValue]] = []
        by_type = app.docs_by_type()
        target_types = docs_filter or list(by_type.keys())
        for dtype in target_types:
            for doc in by_type.get(dtype, []):
                hit = self._resolve_field_on_doc(doc, field)
                if hit is not None:
                    fname, fv = hit
                    collected.append((doc, fname, fv))
        return collected

    def _should_require_all_docs(self, rule: RuleDef) -> bool:
        if rule.require_all_docs is not None:
            return bool(rule.require_all_docs)
        if self.config.default_require_all_docs is not None:
            return bool(self.config.default_require_all_docs)
        # Auto: critical rules require full docs coverage
        return rule.severity.lower() == "critical"

    def _missing_doc_types_for_field(
        self,
        app: Application,
        field: str,
        docs_filter: list[str],
    ) -> list[str]:
        """Listed doc types that are absent or lack a non-empty field value."""
        if not docs_filter:
            return []
        by_type = app.docs_by_type()
        missing: list[str] = []
        for dtype in docs_filter:
            docs = by_type.get(dtype) or []
            if not docs:
                missing.append(dtype)
                continue
            found = False
            for doc in docs:
                hit = self._resolve_field_on_doc(doc, field)
                if hit is not None and hit[1].raw not in (None, ""):
                    found = True
                    break
            if not found:
                missing.append(dtype)
        return missing

    def _missing_verdict(self, rule: RuleDef) -> Verdict:
        om = (rule.on_missing or "uncertain").lower()
        if om == "skip":
            return Verdict.SKIPPED
        if om == "inconsistent":
            return Verdict.INCONSISTENT
        return Verdict.UNCERTAIN

    def _severity(self, rule: RuleDef) -> Severity:
        try:
            return Severity(rule.severity.lower())
        except ValueError:
            return Severity.MAJOR

    def _snapshots_from(
        self, items: list[tuple[Document, str, FieldValue]]
    ) -> list[FieldSnapshot]:
        return [
            FieldSnapshot(
                doc_id=doc.doc_id,
                doc_type=doc.doc_type,
                field=fname,
                raw=fv.raw,
                normalized=fv.normalized,
                confidence=fv.confidence,
                ocr_fix=fv.ocr_fix,
                pre_ocr=fv.pre_ocr,
                notes=list(fv.notes),
            )
            for doc, fname, fv in items
        ]

    def _low_conf(self, items: list[tuple[Document, str, FieldValue]]) -> bool:
        thr = self.config.low_confidence_threshold
        for _, _, fv in items:
            if fv.confidence < thr:
                return True
        return False

    def _detect_name_transfer(
        self,
        present: list[tuple[Document, str, FieldValue]],
        rule: RuleDef,
    ) -> tuple[str, str] | None:
        """Detect clean used-car pattern: one old-owner value vs one new-party value.

        Returns (old_name, new_name) or None if not a clean transfer split.
        """
        policy = rule.transfer_name_policy
        if not policy or policy == "inconsistent":
            # still allow detection only when policy is uncertain
            if policy != "uncertain":
                return None
        old_docs = set(rule.transfer_old_docs or ["机动车登记证书"])
        new_docs = set(
            rule.transfer_new_docs
            or ["交强险保单", "融资租赁合同", "身份证", "发票"]
        )
        old_vals: list[str] = []
        new_vals: list[str] = []
        for doc, _fname, fv in present:
            if fv.normalized is None:
                continue
            if doc.doc_type in old_docs:
                old_vals.append(fv.normalized)
            if doc.doc_type in new_docs:
                new_vals.append(fv.normalized)
        if not old_vals or not new_vals:
            return None
        if len(set(old_vals)) != 1 or len(set(new_vals)) != 1:
            return None
        if old_vals[0] == new_vals[0]:
            return None
        return old_vals[0], new_vals[0]

    def _eval_rule(self, app: Application, rule: RuleDef) -> CheckResult:
        from task4_consistency.reason_codes import infer_reason_codes

        rtype = rule.type.lower()
        method_name = _HANDLER_REGISTRY.get(rtype)
        if not method_name:
            result = CheckResult(
                rule_id=rule.id,
                name=rule.name,
                verdict=Verdict.UNCERTAIN,
                severity=self._severity(rule),
                message=f"unknown rule type: {rule.type}",
                rule_type=rule.type,
            )
        else:
            handler = getattr(self, method_name)
            # cross-field handlers need rtype
            if method_name == "_eval_cross_field":
                result = handler(app, rule, rtype)
            else:
                result = handler(app, rule)
        if not result.reason_codes:
            result.reason_codes = infer_reason_codes(result)
        return result

    def _eval_cross_field(self, app: Application, rule: RuleDef, rtype: str) -> CheckResult:
        field = rule.field or ""
        items = self._collect_field_snapshots(app, field, rule.docs)
        snaps = self._snapshots_from(items)
        present = [(d, n, fv) for d, n, fv in items if fv.raw not in (None, "")]

        if self._should_require_all_docs(rule) and rule.docs:
            missing = self._missing_doc_types_for_field(app, field, rule.docs)
            if missing:
                mv = self._missing_verdict(rule)
                return CheckResult(
                    rule_id=rule.id,
                    name=rule.name,
                    verdict=mv,
                    severity=self._severity(rule),
                    message=(
                        f"字段 {field} 未在全部目标单据齐套："
                        f"missing={missing} (require_all_docs)"
                        + (" — skipped" if mv == Verdict.SKIPPED else "")
                    ),
                    snapshots=snaps,
                    rule_type=rule.type,
                )

        if len(present) < 2:
            mv = self._missing_verdict(rule)
            return CheckResult(
                rule_id=rule.id,
                name=rule.name,
                verdict=mv,
                severity=self._severity(rule),
                message=(
                    f"字段 {field} 可用值不足（{len(present)}），无法完成跨单据比对"
                    + (" — skipped" if mv == Verdict.SKIPPED else "")
                ),
                snapshots=snaps,
                rule_type=rule.type,
            )

        low_conf = self._low_conf(present) or any(
            fv.confidence < rule.min_confidence for _, _, fv in present
        )
        norms = [fv.normalized for _, _, fv in present]
        approx_notes = any(
            "money_approx" in (fv.notes or []) for _, _, fv in present
        )

        if any(n is None for n in norms):
            notes_flat = [n for _, _, fv in present for n in (fv.notes or [])]
            reason = ""
            if notes_flat:
                reason = f" reasons={notes_flat}"
            flags = ["normalize_fail"] + (["low_conf"] if low_conf else [])
            if any("placeholder" in n for n in notes_flat):
                flags.append("placeholder_value")
                reason = reason or " reasons=['placeholder_value']"
            return CheckResult(
                rule_id=rule.id,
                name=rule.name,
                verdict=Verdict.UNCERTAIN,
                severity=self._severity(rule),
                message=f"字段 {field} 标准化/格式校验失败，标记存疑{reason}",
                snapshots=snaps,
                rule_type=rule.type,
                flags=flags,
            )

        # Approximate money: never auto-consistent; explain (ADV-06)
        if approx_notes and rtype in {"exact", "numeric_tolerance"}:
            return CheckResult(
                rule_id=rule.id,
                name=rule.name,
                verdict=Verdict.UNCERTAIN,
                severity=self._severity(rule),
                message=(
                    f"字段 {field} 含约数/估算金额（money_approx），"
                    f"不可自动判定一致 values={norms}"
                ),
                snapshots=snaps,
                rule_type=rule.type,
                flags=["money_approx"] + (["low_conf"] if low_conf else []),
            )

        # ADV-05: low conf — optional still emit inconsistent on clear mismatch (critical)
        if low_conf:
            allow_compare = (
                getattr(self.config, "critical_low_conf_compare", True)
                and rule.severity.lower() == "critical"
                and rtype == "exact"
            )
            if allow_compare and not all_equal(norms):
                left, right = norms[0], next(n for n in norms[1:] if n != norms[0])
                return CheckResult(
                    rule_id=rule.id,
                    name=rule.name,
                    verdict=Verdict.INCONSISTENT,
                    severity=self._severity(rule),
                    message=(
                        f"字段 {field} 低置信度下仍检出不一致: {left} vs {right} "
                        f"(flag=low_conf)"
                    ),
                    snapshots=snaps,
                    diff_highlight=first_diff(str(left), str(right)),
                    score=0.0,
                    rule_type=rule.type,
                    flags=["low_conf", "compared_despite_low_conf"],
                )
            return CheckResult(
                rule_id=rule.id,
                name=rule.name,
                verdict=Verdict.UNCERTAIN,
                severity=self._severity(rule),
                message=f"字段 {field} 存在低置信度值，标记存疑",
                snapshots=snaps,
                rule_type=rule.type,
                flags=["low_conf"],
            )

        if rtype == "exact":
            ok = all_equal(norms)
            if ok:
                # ADV-02: OCR I/O/Q (or other) fix made values equal but raw pre-OCR differed
                # → must NOT silent-consistent
                if any(fv.ocr_fix for _, _, fv in present):
                    pre_vals = [
                        (fv.pre_ocr or fv.normalized or "")
                        for _, _, fv in present
                    ]
                    if len(set(pre_vals)) > 1:
                        return CheckResult(
                            rule_id=rule.id,
                            name=rule.name,
                            verdict=Verdict.UNCERTAIN,
                            severity=self._severity(rule),
                            message=(
                                f"{field} 标准化后一致，但存在 OCR 纠错且纠错前不一致，"
                                f"标记存疑 (ocr_fix pre={pre_vals})"
                            ),
                            snapshots=snaps,
                            score=0.5,
                            rule_type=rule.type,
                        )
                return CheckResult(
                    rule_id=rule.id,
                    name=rule.name,
                    verdict=Verdict.CONSISTENT,
                    severity=self._severity(rule),
                    message=f"{field} 跨单据完全一致",
                    snapshots=snaps,
                    score=1.0,
                    rule_type=rule.type,
                )
            left, right = norms[0], next(n for n in norms[1:] if n != norms[0])
            # VIN edit-distance 1: still a hard identity mismatch → inconsistent
            # (never promote to consistent; optional soft path only for OCR-fixed equality)
            dist = vin_edit_distance(str(left), str(right))
            msg = f"{field} 不一致: {left} vs {right}"
            if field in {"vin", "vehicle_id"} or rule.field in {"vin", "vehicle_id"}:
                msg += f" (edit_distance={dist})"
            return CheckResult(
                rule_id=rule.id,
                name=rule.name,
                verdict=Verdict.INCONSISTENT,
                severity=self._severity(rule),
                message=msg,
                snapshots=snaps,
                diff_highlight=first_diff(str(left), str(right)),
                score=0.0,
                rule_type=rule.type,
            )

        if rtype == "fuzzy":
            out = multi_fuzzy_all(
                norms,
                threshold=rule.threshold,
                uncertain_band=rule.uncertain_band,
            )
            if out.uncertain and not out.match:
                return CheckResult(
                    rule_id=rule.id,
                    name=rule.name,
                    verdict=Verdict.UNCERTAIN,
                    severity=self._severity(rule),
                    message=f"{field} 模糊匹配接近阈值（score={out.score:.3f}），存疑",
                    snapshots=snaps,
                    score=out.score,
                    rule_type=rule.type,
                )
            if out.match:
                return CheckResult(
                    rule_id=rule.id,
                    name=rule.name,
                    verdict=Verdict.CONSISTENT,
                    severity=self._severity(rule),
                    message=f"{field} 模糊一致（score={out.score:.3f}）",
                    snapshots=snaps,
                    score=out.score,
                    rule_type=rule.type,
                )
            # ADV-19: used-car transfer — reg old owner vs party new names
            xfer = self._detect_name_transfer(present, rule)
            if xfer is not None:
                old_n, new_n = xfer
                policy = (rule.transfer_name_policy or "uncertain").lower()
                if policy == "uncertain":
                    return CheckResult(
                        rule_id=rule.id,
                        name=rule.name,
                        verdict=Verdict.UNCERTAIN,
                        severity=self._severity(rule),
                        message=(
                            f"{field} 疑似二手车过户姓名：登记侧={old_n} vs 当事人侧={new_n} "
                            f"（transfer_name_policy=uncertain）"
                        ),
                        snapshots=snaps,
                        score=out.score,
                        rule_type=rule.type,
                        flags=["used_car_name_transfer"],
                        reason_codes=["USED_CAR_NAME_TRANSFER", "NAME_NEAR_UNCERTAIN"],
                    )
            return CheckResult(
                rule_id=rule.id,
                name=rule.name,
                verdict=Verdict.INCONSISTENT,
                severity=self._severity(rule),
                message=f"{field} 模糊不一致（score={out.score:.3f}）: {out.left} vs {out.right}",
                snapshots=snaps,
                diff_highlight=first_diff(str(out.left or ""), str(out.right or "")),
                score=out.score,
                rule_type=rule.type,
            )

        # numeric_tolerance
        out_n = multi_numeric_all(norms, abs_tol=rule.abs_tol, rel_tol=rule.rel_tol)
        if out_n.match:
            return CheckResult(
                rule_id=rule.id,
                name=rule.name,
                verdict=Verdict.CONSISTENT,
                severity=self._severity(rule),
                message=f"{field} 数值在容差内一致 (abs_tol={rule.abs_tol}, rel_tol={rule.rel_tol})",
                snapshots=snaps,
                score=1.0,
                rule_type=rule.type,
            )
        return CheckResult(
            rule_id=rule.id,
            name=rule.name,
            verdict=Verdict.INCONSISTENT,
            severity=self._severity(rule),
            message=(
                f"{field} 数值超出容差: {out_n.left} vs {out_n.right}"
                f" (abs_diff={out_n.abs_diff}, rel_diff={out_n.rel_diff},"
                f" abs_tol={rule.abs_tol}, rel_tol={rule.rel_tol})"
            ),
            snapshots=snaps,
            diff_highlight=DiffHighlight(
                left=str(out_n.left),
                right=str(out_n.right),
                detail=f"abs_tol={rule.abs_tol}, rel_tol={rule.rel_tol}",
            ),
            score=0.0,
            rule_type=rule.type,
        )

    def _eval_conditional(self, app: Application, rule: RuleDef) -> CheckResult:
        if_field = rule.if_field_present
        req_field = rule.required_field
        items_if = self._collect_field_snapshots(app, if_field or "", rule.docs)
        items_req = self._collect_field_snapshots(app, req_field or "", rule.docs)
        snaps = self._snapshots_from(items_if + items_req)

        if_present = any(fv.raw not in (None, "") for _, _, fv in items_if)
        if not if_present:
            return CheckResult(
                rule_id=rule.id,
                name=rule.name,
                verdict=Verdict.CONSISTENT,
                severity=self._severity(rule),
                message=f"条件字段 {if_field} 不存在，跳过必填校验",
                snapshots=snaps,
                rule_type=rule.type,
            )

        # Prefer normalized success (invalid ID format should not count as filled)
        req_present = any(
            fv.raw not in (None, "") and fv.normalized is not None for _, _, fv in items_req
        )
        if req_present:
            return CheckResult(
                rule_id=rule.id,
                name=rule.name,
                verdict=Verdict.CONSISTENT,
                severity=self._severity(rule),
                message=f"条件满足：{if_field} 存在且 {req_field} 已填且通过标准化",
                snapshots=snaps,
                rule_type=rule.type,
            )
        # raw present but normalize failed → uncertain (format issue)
        req_raw = any(fv.raw not in (None, "") for _, _, fv in items_req)
        if req_raw:
            return CheckResult(
                rule_id=rule.id,
                name=rule.name,
                verdict=Verdict.UNCERTAIN,
                severity=self._severity(rule),
                message=f"条件字段 {req_field} 有值但格式校验失败，存疑",
                snapshots=snaps,
                rule_type=rule.type,
            )
        return CheckResult(
            rule_id=rule.id,
            name=rule.name,
            verdict=Verdict.INCONSISTENT,
            severity=self._severity(rule),
            message=f"条件必填失败：存在 {if_field} 但缺少 {req_field}",
            snapshots=snaps,
            rule_type=rule.type,
        )

    def _eval_list_contains(self, app: Application, rule: RuleDef) -> CheckResult:
        list_field = rule.list_field or rule.field
        item_field = rule.item_field or rule.extra.get("item_field")
        if not list_field or not item_field:
            return CheckResult(
                rule_id=rule.id,
                name=rule.name,
                verdict=Verdict.UNCERTAIN,
                severity=self._severity(rule),
                message="list_contains 规则缺少 list_field/item_field",
                rule_type=rule.type,
            )
        list_items = self._collect_field_snapshots(app, list_field, rule.docs)
        item_items = self._collect_field_snapshots(app, str(item_field), rule.docs)
        snaps = self._snapshots_from(list_items + item_items)

        present_list = [(d, n, fv) for d, n, fv in list_items if fv.raw not in (None, "")]
        present_item = [(d, n, fv) for d, n, fv in item_items if fv.raw not in (None, "")]
        if not present_list or not present_item:
            return CheckResult(
                rule_id=rule.id,
                name=rule.name,
                verdict=self._missing_verdict(rule),
                severity=self._severity(rule),
                message="list_contains 字段缺失",
                snapshots=snaps,
                rule_type=rule.type,
            )

        if self._low_conf(present_list + present_item) or any(
            fv.confidence < rule.min_confidence for _, _, fv in present_list + present_item
        ):
            return CheckResult(
                rule_id=rule.id,
                name=rule.name,
                verdict=Verdict.UNCERTAIN,
                severity=self._severity(rule),
                message="list_contains 字段低置信度，标记存疑",
                snapshots=snaps,
                rule_type=rule.type,
            )

        # Normalize every list element with the SAME normalizer as item_field
        def _norm_item(raw: str) -> str | None:
            return normalize_field(raw, field_name=str(item_field))

        item_vals: list[str] = []
        for _, _, fv in present_item:
            n = fv.normalized if fv.normalized is not None else _norm_item(str(fv.raw))
            if n is None:
                return CheckResult(
                    rule_id=rule.id,
                    name=rule.name,
                    verdict=Verdict.UNCERTAIN,
                    severity=self._severity(rule),
                    message=f"{item_field} 标准化失败",
                    snapshots=snaps,
                    rule_type=rule.type,
                )
            item_vals.append(n)

        # Prefer raw for re-splitting; fall back to normalized joined string
        all_ok = True
        last = None
        for item in item_vals:
            ok_any = False
            for _, _, fv in present_list:
                container_raw = fv.raw
                # If already plate_list-normalized as joined string, use that too
                out = list_contains(
                    container_raw,
                    item,
                    normalize_item=_norm_item,
                )
                if not out.match and fv.normalized:
                    # also try pre-normalized joined form without re-normalizing parts twice
                    parts = as_list(fv.normalized)
                    out2 = list_contains(parts, item)
                    if out2.match:
                        out = out2
                last = out
                if out.match:
                    ok_any = True
                    break
            if not ok_any:
                all_ok = False
                break

        if all_ok:
            return CheckResult(
                rule_id=rule.id,
                name=rule.name,
                verdict=Verdict.CONSISTENT,
                severity=self._severity(rule),
                message=f"{item_field} 均包含于 {list_field}",
                snapshots=snaps,
                rule_type=rule.type,
            )
        return CheckResult(
            rule_id=rule.id,
            name=rule.name,
            verdict=Verdict.INCONSISTENT,
            severity=self._severity(rule),
            message=last.message if last else f"{item_field} 不在 {list_field} 中",
            snapshots=snaps,
            rule_type=rule.type,
        )
