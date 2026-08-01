"""Load/validate fixtures/ocr_inbox/step2_slots_*.json."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from task4_consistency.adapters.step2_slots import (
    SLOTS_SCHEMA,
    Step2SlotsError,
    list_step2_slot_files,
    load_step2_slots,
    validate_step2_slots,
)

ROOT = Path(__file__).resolve().parents[1]
INBOX = ROOT / "fixtures" / "ocr_inbox"
STEP2 = ROOT / "data" / "step2"


def test_list_and_load_all_step2_slots():
    files = list_step2_slot_files(INBOX)
    assert len(files) >= 10, f"expected ≥10 step2_slots files, got {len(files)}"
    for fp in files:
        data = load_step2_slots(fp)
        assert data["schema"] == SLOTS_SCHEMA
        assert data["sample_id"]
        assert data["slots"]
        assert data.get("n_slots") == len(data["slots"])
        # pre-OCR: all raw null
        assert all(s.get("raw") is None for s in data["slots"]), fp.name
        # bbox + field present
        for s in data["slots"]:
            assert s["field"]
            assert len(s["bbox"]) == 4
        # matching step2 page_order exists for these samples
        sid = data["sample_id"]
        assert (STEP2 / f"{sid}_page_order.json").is_file(), sid


def test_validate_rejects_bad_schema():
    with pytest.raises(Step2SlotsError) as ei:
        validate_step2_slots({"schema": "wrong", "sample_id": "x", "doc_type": "y", "slots": []})
    assert ei.value.error in {"bad_schema", "bad_slots"}


def test_validate_rejects_bad_bbox_and_raw():
    base = {
        "schema": SLOTS_SCHEMA,
        "sample_id": "X",
        "doc_type": "机动车登记证书",
        "slots": [
            {
                "field": "vin",
                "bbox": [1, 2, 3],  # bad
                "raw": None,
            }
        ],
        "n_slots": 1,
    }
    with pytest.raises(Step2SlotsError) as ei:
        validate_step2_slots(base)
    assert ei.value.error == "bad_bbox"

    base["slots"][0]["bbox"] = [1, 2, 3, 4]
    base["slots"][0]["raw"] = 123
    with pytest.raises(Step2SlotsError) as ei2:
        validate_step2_slots(base)
    assert ei2.value.error == "bad_raw"


def test_n_slots_mismatch():
    data = {
        "schema": SLOTS_SCHEMA,
        "sample_id": "X",
        "doc_type": "机动车登记证书",
        "n_slots": 2,
        "slots": [{"field": "vin", "bbox": [0, 0, 1, 1], "raw": None}],
    }
    with pytest.raises(Step2SlotsError) as ei:
        validate_step2_slots(data)
    assert ei.value.error == "n_slots_mismatch"


def test_load_missing_file(tmp_path):
    with pytest.raises(Step2SlotsError) as ei:
        load_step2_slots(tmp_path / "nope.json")
    assert ei.value.error == "file_not_found"


def test_step2_slots_have_vin_or_engine_and_zip_member():
    """业务关键：至少 vin/engine 之一；每 slot 有影像定位。"""
    files = list_step2_slot_files(INBOX)
    assert files
    for fp in files:
        data = load_step2_slots(fp)
        fields = {s["field"] for s in data["slots"]}
        assert fields & {"vin", "engine_no", "reg_cert_no"}, fp.name
        for s in data["slots"]:
            assert s.get("image_filename") or s.get("zip_member"), fp.name
            assert s.get("page_order") is not None or s.get("page_type")


def test_validate_allows_filled_raw_string():
    """OCR 填字后 raw 可为 str。"""
    data = {
        "schema": SLOTS_SCHEMA,
        "sample_id": "FILLED",
        "doc_type": "机动车登记证书",
        "n_slots": 1,
        "slots": [
            {
                "field": "vin",
                "bbox": [0, 0, 10, 10],
                "raw": "LSVAA4182N2123456",
                "image_filename": "x.png",
            }
        ],
    }
    out = validate_step2_slots(data)
    assert out["slots"][0]["raw"].startswith("LSV")


def test_validate_rejects_empty_field_and_missing_sample():
    with pytest.raises(Step2SlotsError) as ei:
        validate_step2_slots(
            {
                "schema": SLOTS_SCHEMA,
                "sample_id": "",
                "doc_type": "机动车登记证书",
                "slots": [{"field": "vin", "bbox": [0, 0, 1, 1], "raw": None}],
                "n_slots": 1,
            }
        )
    assert ei.value.error == "missing_sample_id"

    with pytest.raises(Step2SlotsError) as ei2:
        validate_step2_slots(
            {
                "schema": SLOTS_SCHEMA,
                "sample_id": "X",
                "doc_type": "机动车登记证书",
                "slots": [{"field": "  ", "bbox": [0, 0, 1, 1], "raw": None}],
                "n_slots": 1,
            }
        )
    assert ei2.value.error == "empty_field"


def test_load_invalid_json(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(Step2SlotsError) as ei:
        load_step2_slots(bad)
    assert ei.value.error == "invalid_json"


def test_api_ocr_inbox_lists_slots():
    """GET /api/ocr_inbox → step2_slots_*.json 清单（slots×10）。"""
    import os

    from fastapi.testclient import TestClient

    from task4_consistency.web import app as webapp

    os.environ.pop("TASK4_WEB_TOKEN", None)
    client = TestClient(webapp.app)
    r = client.get("/api/ocr_inbox")
    assert r.status_code == 200, r.text
    body = r.json()
    items = body.get("items") or []
    assert len(items) >= 10
    assert all(i.get("file", "").startswith("step2_slots_") for i in items if "error" not in i)
    assert any((i.get("n_slots") or 0) >= 1 for i in items)
    assert "STEP2_TO_TASK4" in (body.get("note") or "") or "OCR" in (body.get("note") or "")


def test_mgr_fixtures_exist_and_engine_ok():
    """Manager 下发 fixtures：app_mgr_* 可加载且 expected 对齐。"""
    from task4_consistency.models import Application
    from task4_consistency.rules.engine import RuleEngine
    from task4_consistency.rules.loader import load_rules

    eng = RuleEngine(load_rules(ROOT / "configs" / "rules_auto_lease.yaml"))
    files = sorted((ROOT / "fixtures" / "applications").glob("app_mgr_*.json"))
    assert len(files) >= 5, f"mgr fixtures expected ≥5, got {len(files)}"
    for fp in files:
        data = json.loads(fp.read_text(encoding="utf-8"))
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        assert meta.get("field_source") == "synthetic" or "field_source" in meta or data.get(
            "expected_verdicts"
        )
        exp = data.get("expected_verdicts") or {}
        if not exp:
            continue
        rep = eng.run(Application.from_dict(data))
        got = {
            c.rule_id: (c.verdict.value if hasattr(c.verdict, "value") else str(c.verdict))
            for c in rep.checks
        }
        bad = {k: (exp[k], got.get(k)) for k in exp if got.get(k) != exp[k]}
        assert not bad, f"{fp.name} {bad}"
