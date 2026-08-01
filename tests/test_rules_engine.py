import json
from pathlib import Path

from task4_consistency.models import Application, Verdict
from task4_consistency.rules.engine import RuleEngine
from task4_consistency.rules.loader import load_rules

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "configs" / "rules_auto_lease.yaml"
FIXTURES = ROOT / "fixtures" / "applications"


def _run(name: str):
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    app = Application.from_dict(data)
    engine = RuleEngine(load_rules(RULES))
    report = engine.run(app)
    by_id = {c.rule_id: c for c in report.checks}
    return data, report, by_id


def test_consistent_app():
    data, report, by_id = _run("app_consistent_01.json")
    assert report.summary.inconsistent == 0
    for rid, exp in data["expected_verdicts"].items():
        assert by_id[rid].verdict.value == exp, rid


def test_inconsistent_vin():
    _, _, by_id = _run("app_inconsistent_vin.json")
    assert by_id["R_VIN_CROSS"].verdict == Verdict.INCONSISTENT
    assert by_id["R_VIN_CROSS"].diff_highlight is not None


def test_inconsistent_amount():
    _, _, by_id = _run("app_inconsistent_amount.json")
    assert by_id["R_AMOUNT_TOL"].verdict == Verdict.INCONSISTENT


def test_uncertain_low_conf():
    _, _, by_id = _run("app_uncertain_ocr_noise.json")
    assert by_id["R_VIN_CROSS"].verdict == Verdict.UNCERTAIN


def test_conditional_required():
    _, _, by_id = _run("app_missing_id_conditional.json")
    assert by_id["R_ID_REQUIRED_IF_AMOUNT"].verdict == Verdict.INCONSISTENT


def test_format_variants_still_consistent():
    data, _, by_id = _run("app_consistent_format_variants.json")
    for rid, exp in data["expected_verdicts"].items():
        assert by_id[rid].verdict.value == exp, (rid, by_id[rid].message)
