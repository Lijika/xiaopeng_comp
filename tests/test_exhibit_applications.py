from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from task4_consistency.models import Application
from task4_consistency.web.app import _active_rules_path, _run_check

ROOT = Path(__file__).resolve().parents[1]


def _builder():
    spec = spec_from_file_location(
        "build_exhibit_applications",
        ROOT / "scripts" / "build_exhibit_applications.py",
    )
    module = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_ocr_compact_feeds_registration_fields():
    builder = _builder()
    ocr = builder.load_ocr_fields(ROOT / "材料" / "registration_extract" / "JFL25P02L080310-01")
    assert ocr["vin"] == "WDDUX6HB4FA197351"
    assert "engine_no" in ocr
    assert "reg_cert_no" in ocr


def test_ok_application_is_fully_consistent():
    builder = _builder()
    ocr = builder.load_ocr_fields(ROOT / "材料" / "registration_extract" / "JFL25P02L080310-01")
    payload = builder.build_application("JFL25P02L080310-01", ocr, mismatch_vin=False)
    report = _run_check(Application.from_dict(payload), _active_rules_path())
    summary = report.to_dict()["summary"]
    assert summary["inconsistent"] == 0
    assert summary["uncertain"] == 0
    assert summary["consistent"] >= 1


def test_mismatch_application_flags_vin():
    builder = _builder()
    ocr = builder.load_ocr_fields(ROOT / "材料" / "registration_extract" / "JFL25P02L080310-01")
    payload = builder.build_application("JFL25P02L080310-01", ocr, mismatch_vin=True)
    report = _run_check(Application.from_dict(payload), _active_rules_path())
    vin = next(
        item for item in report.to_dict()["checks"] if item["rule_id"] == "R_VIN_CROSS"
    )
    assert vin["verdict"] == "inconsistent"
