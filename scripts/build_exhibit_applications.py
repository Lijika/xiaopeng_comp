#!/usr/bin/env python3
"""Build Task-4 Application JSON from registration-certificate OCR.

主办方只提供了登记证影像。本脚本读取 ``材料/ocr_out/*/…_compact.json`` 中的
真实抽字段（VIN / 发动机号 / 登记证编号 / 品牌 / 型号），再按同一辆车补齐
保单、合同、发票、身份证上任务4规则需要的字段，才能做跨单据核验展示。

补齐字段（姓名、证件号、车牌、金额、地址、日期）不是主办方原文，写入
``meta.completed_fields``。登记证上的 OCR 值写入 ``meta.ocr_fields``。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OCR_ROOT = ROOT / "材料" / "ocr_out"
OUT_DIR = ROOT / "材料" / "task4_applications"

OCR_FIELD_MAP = {
    "车辆识别代号/车架号": "vin",
    "车辆识别代号": "vin",
    "车架号": "vin",
    "发动机号": "engine_no",
    "登记证书编号": "reg_cert_no",
    "车辆品牌": "brand",
    "车辆型号": "model",
}


def _stable_int(text: str, modulo: int) -> int:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % modulo


def _id_checksum(body17: str) -> str:
    weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    mapping = "10X98765432"
    total = sum(int(digit) * weight for digit, weight in zip(body17, weights))
    return mapping[total % 11]


def _id_number(seed: str) -> str:
    day = 1 + _stable_int(seed, 28)
    seq = 123 + _stable_int(seed + ":id", 800)
    body17 = f"320102199001{day:02d}{seq:03d}"
    return body17 + _id_checksum(body17)


def _plate(seed: str, vin: str) -> str:
    tail = re.sub(r"[^A-Z0-9]", "", vin.upper())[-5:].ljust(5, "0")
    return f"苏A{tail}"


def _amount(seed: str) -> str:
    return f"{120000 + _stable_int(seed, 80) * 1000}.00"


def _fv(raw: str | None, confidence: float = 0.93, source_page: int | None = None) -> dict:
    item: dict = {"raw": raw, "confidence": confidence}
    if source_page is not None:
        item["source_page"] = source_page
    return item


def load_ocr_fields(sample_dir: Path) -> dict[str, str]:
    compact = sample_dir / f"{sample_dir.name}_compact.json"
    if not compact.is_file():
        return {}
    payload = json.loads(compact.read_text(encoding="utf-8"))
    raw_fields = payload.get("fields") if isinstance(payload, dict) else None
    if not isinstance(raw_fields, dict):
        return {}
    out: dict[str, str] = {}
    for label, body in raw_fields.items():
        key = OCR_FIELD_MAP.get(str(label))
        if key is None or not isinstance(body, dict):
            continue
        value = body.get("value")
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()
    return out


def build_application(sample_id: str, ocr: dict[str, str], *, mismatch_vin: bool) -> dict:
    vin = ocr.get("vin") or f"LSVAA4182N2{_stable_int(sample_id, 900000):06d}"
    engine = ocr.get("engine_no") or f"ENG{_stable_int(sample_id, 900000):06d}"
    cert_no = ocr.get("reg_cert_no") or f"3201{_stable_int(sample_id, 10**8):08d}"
    brand = ocr.get("brand") or "一汽大众"
    model = ocr.get("model") or "帕萨特"
    name = "张三"
    plate = _plate(sample_id, vin)
    amount = _amount(sample_id)
    address = "江苏省南京市鼓楼区中山路100号"
    id_no = _id_number(sample_id)
    date = "2024年5月18日"
    contract_vin = vin
    if mismatch_vin:
        contract_vin = vin[:-1] + ("0" if vin[-1] != "0" else "1")

    ocr_keys = sorted(k for k in ("vin", "engine_no", "reg_cert_no", "brand", "model") if k in ocr)
    completed = [
        "owner_name",
        "insured_name",
        "lessee_name",
        "id_number",
        "plate_no",
        "plate_list",
        "financed_amount",
        "invoice_amount",
        "address",
        "reg_date",
        "contract_date",
    ]
    if "vin" not in ocr:
        completed.append("vin")
    if "engine_no" not in ocr:
        completed.append("engine_no")

    return {
        "application_id": f"EXHIBIT-{sample_id}{'-BADVIN' if mismatch_vin else '-OK'}",
        "meta": {
            "field_source": "exhibit-ocr-completed",
            "step2_sample_id": sample_id,
            "ocr_source": f"材料/ocr_out/{sample_id}",
            "ocr_fields": ocr_keys,
            "completed_fields": completed,
            "note": (
                "登记证 VIN/发动机号/登记证编号/品牌/型号来自 ocr_out；"
                "保单/合同/发票/身份证由同一辆车补齐，用于跨单据核验展示。"
                "补齐字段不是主办方原文。"
            ),
        },
        "documents": [
            {
                "doc_id": "reg_cert",
                "doc_type": "机动车登记证书",
                "fields": {
                    "vin": _fv(vin, 0.94, 1),
                    "engine_no": _fv(engine, 0.93, 1),
                    "reg_cert_no": _fv(cert_no, 0.92, 1),
                    "brand": _fv(brand, 0.9, 1),
                    "model": _fv(model, 0.9, 1),
                    "owner_name": _fv(name, 0.9),
                    "plate_no": _fv(plate, 0.9),
                    "reg_date": _fv(date, 0.9),
                    "address": _fv(address, 0.88),
                },
            },
            {
                "doc_id": "policy",
                "doc_type": "交强险保单",
                "fields": {
                    "vin": _fv(vin, 0.93),
                    "engine_no": _fv(engine.replace("-", ""), 0.92),
                    "insured_name": _fv(name, 0.9),
                    "plate_no": _fv(plate.replace("·", ""), 0.9),
                    "plate_list": _fv(f"{plate.replace('·', '')}|苏A00000", 0.88),
                    "brand": _fv(brand, 0.9),
                    "model": _fv(model, 0.9),
                },
            },
            {
                "doc_id": "lease",
                "doc_type": "融资租赁合同",
                "fields": {
                    "vin": _fv(contract_vin, 0.93),
                    "lessee_name": _fv(name, 0.9),
                    "financed_amount": _fv(f"{amount}元", 0.92),
                    "reg_cert_no": _fv(cert_no, 0.9),
                    "id_number": _fv(id_no, 0.92),
                    "reg_date": _fv("2024-05-18", 0.9),
                    "contract_date": _fv("2024-05-18", 0.9),
                    "brand": _fv(brand, 0.9),
                    "model": _fv(model, 0.9),
                },
            },
            {
                "doc_id": "invoice",
                "doc_type": "发票",
                "fields": {
                    "vin": _fv(vin, 0.92),
                    "engine_no": _fv(engine, 0.9),
                    "invoice_amount": _fv(amount.split(".")[0], 0.9),
                    "brand": _fv(brand, 0.9),
                    "model": _fv(model, 0.9),
                    "buyer_name": _fv(name, 0.9),
                },
            },
            {
                "doc_id": "id_card",
                "doc_type": "身份证",
                "fields": {
                    "owner_name": _fv(name, 0.95),
                    "id_number": _fv(id_no, 0.95),
                    "address": _fv("江苏南京市鼓楼区中山路100号", 0.9),
                },
            },
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ocr-root", type=Path, default=OCR_ROOT)
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for sample_dir in sorted(p for p in args.ocr_root.iterdir() if p.is_dir()):
        ocr = load_ocr_fields(sample_dir)
        if not ocr:
            continue
        ok = build_application(sample_dir.name, ocr, mismatch_vin=False)
        ok_path = args.out_dir / f"{sample_dir.name}_ok.json"
        ok_path.write_text(json.dumps(ok, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written.append(ok_path)
        if "vin" in ocr:
            bad = build_application(sample_dir.name, ocr, mismatch_vin=True)
            bad_path = args.out_dir / f"{sample_dir.name}_vin_mismatch.json"
            bad_path.write_text(
                json.dumps(bad, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            written.append(bad_path)
    readme = args.out_dir / "README.md"
    readme.write_text(
        "\n".join(
            [
                "# 展会用任务4申请 JSON",
                "",
                "由 `scripts/build_exhibit_applications.py` 从 `材料/ocr_out` 生成。",
                "",
                "- `*_ok.json`：登记证 OCR 字段 + 同车补齐的保单/合同/发票/身份证，跨单应对齐。",
                "- `*_vin_mismatch.json`：合同 VIN 最后一位被改掉，应用于展示「不一致」。",
                "",
                "首页选择其中一个 JSON 上传即可核验。不要把这些文件说成主办方提供的保单/合同原文。",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(f"wrote {len(written)} files under {args.out_dir}")
    for path in written:
        print(path.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
