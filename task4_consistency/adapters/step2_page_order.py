"""Adapter: step2 page_order JSON -> Application schema placeholders (no OCR text)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from task4_consistency.models import Application, Document, FieldValue

# Map detection class names to logical field keys when possible
_CLASS_TO_FIELD = {
    "登记证书编号": "reg_cert_no",
    "车辆识别代号": "vin",
    "车辆识别代号/车架号": "vin",
    "发动机号": "engine_no",
    "号牌号码": "plate_no",
    "所有人": "owner_name",
    "姓名": "owner_name",
    "住址": "address",
    "登记日期": "reg_date",
}


def page_order_to_application(
    data: dict[str, Any],
    *,
    application_id: str | None = None,
    doc_type: str = "机动车登记证书",
) -> Application:
    sample_id = str(data.get("sample_id") or application_id or "unknown")
    fields: dict[str, FieldValue] = {}
    for page in data.get("pages") or []:
        page_order = page.get("order")
        for det in page.get("detections") or []:
            cname = det.get("class_name_cn") or det.get("class_name") or ""
            field_key = _CLASS_TO_FIELD.get(cname)
            if not field_key:
                # keep unknown as class-based placeholder
                field_key = f"det_{det.get('class_id', cname)}"
            conf = float(det.get("confidence") or 0.0)
            # No OCR text available — raw is null placeholder
            if field_key not in fields or conf > fields[field_key].confidence:
                fields[field_key] = FieldValue(
                    raw=None,
                    confidence=conf,
                    source_page=page_order,
                    field_type=None,
                )
    doc = Document(
        doc_id=sample_id,
        doc_type=doc_type,
        fields=fields,
    )
    return Application(
        application_id=sample_id,
        documents=[doc],
        meta={
            "source": "step2_page_order",
            # Round19: layout-only adapter — no OCR text; raw fields are null
            "field_source": None,
            "step2_sample_id": sample_id,
        },
    )


def load_page_order(path: str | Path) -> Application:
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return page_order_to_application(data)


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(description="Convert step2 page_order to application schema")
    p.add_argument("input", help="page_order JSON path")
    p.add_argument("-o", "--output", help="output application JSON")
    args = p.parse_args()
    app = load_page_order(args.input)
    text = json.dumps(app.to_dict(), ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()
