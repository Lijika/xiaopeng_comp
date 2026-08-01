"""Round19: external OCR intermediate import + meta force."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from task4_consistency.adapters.external_ocr_import import (
    ExternalOcrImportError,
    external_ocr_to_application,
    import_external_ocr_to_dir,
    load_external_ocr_file,
    validate_external_ocr_payload,
)
from task4_consistency.adapters.step2_page_order import page_order_to_application

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "fixtures" / "ocr_inbox" / "example.json"


def test_example_valid_and_meta_forced():
    app = load_external_ocr_file(EXAMPLE, demo_note="demo")
    assert app.meta["source"] == "external_ocr"
    assert app.meta["field_source"] == "external_ocr"
    assert app.meta["ocr_model"]
    assert app.meta["ocr_version"]
    assert app.meta["ocr_imported_at"]
    assert app.meta.get("note") == "demo"
    assert len(app.documents) >= 1


def test_missing_ocr_model_rejected():
    data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    data.pop("ocr_model")
    with pytest.raises(ExternalOcrImportError) as ei:
        validate_external_ocr_payload(data)
    assert ei.value.error == "missing_ocr_model"


def test_missing_ocr_version_rejected():
    data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    data["ocr_version"] = "  "
    with pytest.raises(ExternalOcrImportError) as ei:
        external_ocr_to_application(data)
    assert ei.value.error == "missing_ocr_version"


def test_import_writes_semi(tmp_path):
    out = import_external_ocr_to_dir(
        EXAMPLE,
        tmp_path,
        repo_root=ROOT,
        demo_note="demo",
    )
    assert out.exists()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["meta"]["field_source"] == "external_ocr"
    assert data["meta"]["source"] == "external_ocr"


def test_path_outside_repo_rejected(tmp_path):
    outsider = tmp_path / "out.json"
    outsider.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ExternalOcrImportError) as ei:
        import_external_ocr_to_dir(outsider, tmp_path / "semi", repo_root=ROOT)
    assert ei.value.error == "path_outside_repo"


def test_step2_adapter_field_source_null():
    app = page_order_to_application(
        {
            "sample_id": "S1",
            "pages": [
                {
                    "order": 1,
                    "detections": [
                        {
                            "class_name_cn": "车辆识别代号",
                            "confidence": 0.9,
                            "class_id": 1,
                        }
                    ],
                }
            ],
        }
    )
    assert app.meta.get("field_source") is None
    # raw still null — no fake OCR text
    vin = app.documents[0].fields.get("vin")
    assert vin is not None
    assert vin.raw is None
