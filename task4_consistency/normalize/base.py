"""Normalizer registry and field-type routing."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from typing import Any

Normalizer = Callable[[str], str | None]

_REGISTRY: dict[str, Normalizer] = {}

# Canonical field name -> normalizer type
FIELD_TYPE_HINTS: dict[str, str] = {
    "vin": "vin",
    "vehicle_id": "vin",
    "车辆识别代号": "vin",
    "engine_no": "engine",
    "发动机号": "engine",
    "owner_name": "person",
    "lessee_name": "person",
    "insured_name": "person",
    "buyer_name": "person",
    "seller_name": "person",
    "姓名": "person",
    "id_number": "id_number",
    "id_no": "id_number",
    "身份证号": "id_number",
    "证件号": "id_number",
    "plate_no": "plate",
    "plate_number": "plate",
    "号牌号码": "plate",
    "车牌": "plate",
    "plate_list": "plate_list",
    "号牌列表": "plate_list",
    "financed_amount": "money",
    "invoice_amount": "money",
    "loan_amount": "money",
    "amount": "money",
    "金额": "money",
    "reg_date": "date",
    "issue_date": "date",
    "contract_date": "date",
    "policy_start": "date",
    "policy_end": "date",
    "日期": "date",
    "address": "address",
    "owner_address": "address",
    "地址": "address",
    "reg_cert_no": "reg_cert_no",
    "登记证编号": "reg_cert_no",
    "登记证书编号": "reg_cert_no",
    "brand": "brand",
    "vehicle_brand": "brand",
    "品牌": "brand",
    "model": "model",
    "vehicle_model": "model",
    "型号": "model",
    "车型": "model",
}


def register_normalizer(field_type: str, fn: Normalizer) -> None:
    _REGISTRY[field_type] = fn


def fullwidth_to_halfwidth(text: str) -> str:
    chars: list[str] = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            chars.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            chars.append(chr(code - 0xFEE0))
        else:
            chars.append(ch)
    return "".join(chars)


def collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", "", text)


def basic_clean(raw: str) -> str:
    text = unicodedata.normalize("NFKC", raw)
    text = fullwidth_to_halfwidth(text)
    return text.strip()


def normalize_generic(raw: str) -> str:
    return collapse_whitespace(basic_clean(raw)).upper()


_PLACEHOLDER_TOKENS = {
    "",
    "-",
    "—",
    "–",
    "/",
    "无",
    "无数据",
    "空",
    "未知",
    "暂无",
    "待填",
    "NA",
    "N/A",
    "NULL",
    "NONE",
    "NIL",
    "XXXX",
    "XXX",
    "PLACEHOLDER",
}


def is_placeholder_value(raw: str) -> bool:
    text = basic_clean(raw)
    t = re.sub(r"\s+", "", text).upper()
    return t in _PLACEHOLDER_TOKENS


def normalize_engine(raw: str) -> str | None:
    if is_placeholder_value(raw):
        return None
    text = basic_clean(raw)
    text = re.sub(r"[\s\-·•_]", "", text)
    if not text:
        return None
    return text.upper()


def normalize_reg_cert_no(raw: str) -> str | None:
    if is_placeholder_value(raw):
        return None
    text = basic_clean(raw)
    text = re.sub(r"[\s\-·•_]", "", text)
    if not text:
        return None
    return text.upper()


def normalize_brand(raw: str) -> str | None:
    """Vehicle brand normalize — KEEP JV group prefixes (一汽/上汽/…).

    ADV-01: stripping 一汽/上汽 collapsed distinct brands into 大众 → false consistent.
    Only strip trailing 牌/公司 suffixes and hyphens/spaces.
    Org aliases from KB applied after built-in cleanup.
    """
    text = basic_clean(raw)
    if not text:
        return None
    text = re.sub(r"\s+", "", text)
    # KB org aliases (full company names → short brand); keys should be NFKC-normalized
    try:
        from task4_consistency.kb.store import get_kb

        text = get_kb().resolve_org(text)
    except Exception:
        pass
    text = text.replace("-", "").replace("—", "").replace("·", "")
    # drop location parentheticals: 特斯拉(上海) → 特斯拉
    text = re.sub(r"[\(（][^)）]*[\)）]", "", text)
    for suf in ("汽车工业有限公司", "汽车有限公司", "股份有限公司", "有限公司", "汽车", "牌"):
        if text.endswith(suf) and len(text) > len(suf):
            text = text[: -len(suf)]
    if not text:
        return None
    return text.upper() if re.search(r"[A-Za-z]", text) else text


def normalize_model(raw: str) -> str | None:
    text = basic_clean(raw)
    if not text:
        return None
    text = re.sub(r"[\s\-·•_]", "", text)
    return text.upper()


def _ensure_builtins() -> None:
    if _REGISTRY:
        return
    from task4_consistency.normalize.address import normalize_address
    from task4_consistency.normalize.date import normalize_date
    from task4_consistency.normalize.id_number import normalize_id_number
    from task4_consistency.normalize.money import normalize_money
    from task4_consistency.normalize.person import normalize_person_name
    from task4_consistency.normalize.plate import normalize_plate, normalize_plate_list
    from task4_consistency.normalize.vin import normalize_vin

    register_normalizer("vin", normalize_vin)
    register_normalizer("date", normalize_date)
    register_normalizer("money", normalize_money)
    register_normalizer("address", normalize_address)
    register_normalizer("person", normalize_person_name)
    register_normalizer("plate", normalize_plate)
    register_normalizer("plate_list", normalize_plate_list)
    # id_number uses normalize_id_number_ex via normalize_field_ex
    register_normalizer("id_number", normalize_id_number)
    register_normalizer("engine", normalize_engine)
    register_normalizer("reg_cert_no", normalize_reg_cert_no)
    register_normalizer("brand", normalize_brand)
    register_normalizer("model", normalize_model)
    register_normalizer("generic", normalize_generic)


def infer_field_type(field_name: str, explicit: str | None = None) -> str:
    if explicit:
        return explicit
    if field_name in FIELD_TYPE_HINTS:
        return FIELD_TYPE_HINTS[field_name]
    lower = field_name.lower()
    for key, ftype in FIELD_TYPE_HINTS.items():
        if key.lower() == lower:
            return ftype
    return "generic"


def normalize_field_ex(
    raw: str | None,
    field_name: str = "",
    field_type: str | None = None,
    *,
    date_order: str | None = None,
    vin_fix_ioq: bool = True,
    vin_strict_check_digit: bool = False,
    expand_id15_to_18: bool = True,
) -> "NormalizeResult":
    """Normalize with OCR/ambiguity metadata."""
    from task4_consistency.normalize.result import NormalizeResult

    if raw is None:
        return NormalizeResult(value=None)
    if not isinstance(raw, str):
        raw = str(raw)
    raw = raw.strip()
    if raw == "":
        return NormalizeResult(value=None)

    _ensure_builtins()
    ftype = infer_field_type(field_name, field_type)

    try:
        if ftype == "vin":
            from task4_consistency.normalize.vin import normalize_vin_ex

            return normalize_vin_ex(
                raw,
                fix_ioq=vin_fix_ioq,
                validate=True,
                strict_check_digit=vin_strict_check_digit,
            )
        if ftype == "date":
            from task4_consistency.normalize.date import normalize_date_ex

            return normalize_date_ex(raw, date_order=date_order)
        if ftype == "money":
            from task4_consistency.normalize.money import normalize_money_ex

            return normalize_money_ex(raw)
        if ftype == "id_number":
            from task4_consistency.normalize.id_number import normalize_id_number_ex

            return normalize_id_number_ex(
                raw,
                validate=True,
                strict_checksum=True,
                expand_15_to_18=expand_id15_to_18,
            )
        # ADV-17 / ADV-11: placeholders → None + note (never fake consistent)
        if ftype in {"engine", "reg_cert_no", "id_number", "generic"} and is_placeholder_value(
            raw
        ):
            return NormalizeResult(value=None, notes=["placeholder_value"])
        fn = _REGISTRY.get(ftype) or _REGISTRY["generic"]
        result = fn(raw)
    except Exception:
        return NormalizeResult(value=None)

    if result is None:
        return NormalizeResult(value=None)
    if isinstance(result, NormalizeResult):
        return result
    text = str(result).strip()
    return NormalizeResult(value=text if text else None)


def normalize_field(
    raw: str | None,
    field_name: str = "",
    field_type: str | None = None,
) -> str | None:
    """Normalize a raw field value. Returns None if raw empty/unparseable."""
    return normalize_field_ex(raw, field_name=field_name, field_type=field_type).value


def normalize_value_for_compare(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)
