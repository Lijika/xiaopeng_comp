"""Stable machine-readable reason codes for CheckResult (Round7)."""

from __future__ import annotations

from task4_consistency.models import CheckResult, Verdict

# Enumerated codes — free-form strings discouraged in new code
VIN_MISMATCH = "VIN_MISMATCH"
VIN_OCR_FIX_MERGE = "VIN_OCR_FIX_MERGE"
VIN_NORMALIZE_FAIL = "VIN_NORMALIZE_FAIL"
ENGINE_MISMATCH = "ENGINE_MISMATCH"
ID_MISMATCH = "ID_MISMATCH"
ID_NORMALIZE_FAIL = "ID_NORMALIZE_FAIL"
NAME_MISMATCH = "NAME_MISMATCH"
NAME_NEAR_UNCERTAIN = "NAME_NEAR_UNCERTAIN"
PLATE_MISMATCH = "PLATE_MISMATCH"
PLATE_NOT_IN_LIST = "PLATE_NOT_IN_LIST"
AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
AMOUNT_APPROX = "AMOUNT_APPROX"
DATE_MISMATCH = "DATE_MISMATCH"
DATE_AMBIGUOUS = "DATE_AMBIGUOUS"
DATE_INCOMPLETE = "DATE_INCOMPLETE"
REG_CERT_MISMATCH = "REG_CERT_MISMATCH"
BRAND_MISMATCH = "BRAND_MISMATCH"
MODEL_MISMATCH = "MODEL_MISMATCH"
ADDRESS_MISMATCH = "ADDRESS_MISMATCH"
LOW_CONF = "LOW_CONF"
LOW_CONF_COMPARED = "LOW_CONF_COMPARED"
MISSING_DOCS = "MISSING_DOCS"
MISSING_FIELD = "MISSING_FIELD"
NORMALIZE_FAIL = "NORMALIZE_FAIL"
CONDITIONAL_REQUIRED_FAIL = "CONDITIONAL_REQUIRED_FAIL"
CONDITIONAL_SKIP = "CONDITIONAL_SKIP"
USED_CAR_NAME_TRANSFER = "USED_CAR_NAME_TRANSFER"
PLACEHOLDER_VALUE = "PLACEHOLDER_VALUE"
SKIPPED = "SKIPPED"
CONSISTENT = "CONSISTENT"
UNKNOWN = "UNKNOWN"

ALL_CODES = sorted(
    {
        VIN_MISMATCH,
        VIN_OCR_FIX_MERGE,
        VIN_NORMALIZE_FAIL,
        ENGINE_MISMATCH,
        ID_MISMATCH,
        ID_NORMALIZE_FAIL,
        NAME_MISMATCH,
        NAME_NEAR_UNCERTAIN,
        PLATE_MISMATCH,
        PLATE_NOT_IN_LIST,
        AMOUNT_MISMATCH,
        AMOUNT_APPROX,
        DATE_MISMATCH,
        DATE_AMBIGUOUS,
        DATE_INCOMPLETE,
        REG_CERT_MISMATCH,
        BRAND_MISMATCH,
        MODEL_MISMATCH,
        ADDRESS_MISMATCH,
        LOW_CONF,
        LOW_CONF_COMPARED,
        MISSING_DOCS,
        MISSING_FIELD,
        NORMALIZE_FAIL,
        CONDITIONAL_REQUIRED_FAIL,
        CONDITIONAL_SKIP,
        USED_CAR_NAME_TRANSFER,
        PLACEHOLDER_VALUE,
        SKIPPED,
        CONSISTENT,
        UNKNOWN,
    }
)


def infer_reason_codes(result: CheckResult) -> list[str]:
    """Derive stable codes from rule_id / flags / message / verdict."""
    codes: list[str] = []
    rid = result.rule_id or ""
    msg = result.message or ""
    flags = set(result.flags or [])
    v = result.verdict

    if "low_conf" in flags:
        codes.append(LOW_CONF)
    if "compared_despite_low_conf" in flags:
        codes.append(LOW_CONF_COMPARED)
    if "money_approx" in flags:
        codes.append(AMOUNT_APPROX)
    if "normalize_fail" in flags:
        codes.append(NORMALIZE_FAIL)
    if "placeholder" in flags or "placeholder_value" in flags:
        codes.append(PLACEHOLDER_VALUE)

    if v == Verdict.SKIPPED:
        codes.append(SKIPPED)
        return _uniq(codes)
    if v == Verdict.CONSISTENT:
        codes.append(CONSISTENT)
        return _uniq(codes)

    if "require_all_docs" in msg or "齐套" in msg:
        codes.append(MISSING_DOCS)
    if "可用值不足" in msg or "字段缺失" in msg:
        codes.append(MISSING_FIELD)
    if "标准化" in msg or "格式校验失败" in msg:
        codes.append(NORMALIZE_FAIL)
    if "placeholder" in msg.lower() or "占位" in msg:
        codes.append(PLACEHOLDER_VALUE)
    if "OCR" in msg or "ocr_fix" in msg.lower() or "纠错" in msg:
        codes.append(VIN_OCR_FIX_MERGE if "vin" in rid.lower() or "VIN" in rid else NORMALIZE_FAIL)
    if "ambiguous" in msg.lower() or "歧义" in msg or "存疑" in msg and "日期" in msg:
        if "R_DATE" in rid or "日期" in msg:
            codes.append(DATE_AMBIGUOUS)
    if "incomplete" in msg.lower() or "year_month" in msg:
        codes.append(DATE_INCOMPLETE)
    if "约数" in msg or "money_approx" in msg:
        codes.append(AMOUNT_APPROX)

    if v == Verdict.INCONSISTENT:
        if "R_VIN" in rid:
            codes.append(VIN_MISMATCH)
        elif "R_ENGINE" in rid:
            codes.append(ENGINE_MISMATCH)
        elif "R_ID_EXACT" in rid:
            codes.append(ID_MISMATCH)
        elif "R_NAME" in rid:
            codes.append(NAME_MISMATCH)
        elif "R_PLATE_IN_LIST" in rid:
            codes.append(PLATE_NOT_IN_LIST)
        elif "R_PLATE" in rid:
            codes.append(PLATE_MISMATCH)
        elif "R_AMOUNT" in rid:
            codes.append(AMOUNT_MISMATCH)
        elif "R_DATE" in rid:
            codes.append(DATE_MISMATCH)
        elif "R_REG_CERT" in rid:
            codes.append(REG_CERT_MISMATCH)
        elif "R_BRAND" in rid:
            codes.append(BRAND_MISMATCH)
        elif "R_MODEL" in rid:
            codes.append(MODEL_MISMATCH)
        elif "R_ADDRESS" in rid:
            codes.append(ADDRESS_MISMATCH)
        elif "R_ID_REQUIRED" in rid:
            codes.append(CONDITIONAL_REQUIRED_FAIL)
        elif not codes:
            codes.append(UNKNOWN)

    if v == Verdict.UNCERTAIN:
        if "used_car_name_transfer" in flags or "过户" in msg:
            codes.append(USED_CAR_NAME_TRANSFER)
        if "R_NAME" in rid and ("接近阈值" in msg or "模糊" in msg):
            codes.append(NAME_NEAR_UNCERTAIN)
        elif "R_VIN" in rid and ("OCR" in msg or "纠错" in msg):
            codes.append(VIN_OCR_FIX_MERGE)
        elif "R_DATE" in rid:
            if "incomplete" in msg or "标准化" in msg:
                codes.append(DATE_AMBIGUOUS)
        elif "R_AMOUNT" in rid and ("约" in msg or "approx" in msg.lower()):
            codes.append(AMOUNT_APPROX)
        elif "R_ID_REQUIRED" in rid and "跳过" in msg:
            codes.append(CONDITIONAL_SKIP)
        elif not any(c not in {LOW_CONF, LOW_CONF_COMPARED, NORMALIZE_FAIL} for c in codes):
            if "低置信" in msg:
                pass  # already LOW_CONF
            elif not codes:
                codes.append(UNKNOWN)

    return _uniq(codes) or [UNKNOWN]


def _uniq(codes: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for c in codes:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out
