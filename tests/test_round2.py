"""Round2 commercial enhancements."""

from pathlib import Path

from task4_consistency.match.fuzzy import adaptive_uncertain_band, fuzzy_match, fuzzy_ratio
from task4_consistency.models import Application, Document, FieldValue, Verdict
from task4_consistency.normalize.base import normalize_field
from task4_consistency.report import report_to_html
from task4_consistency.rules.engine import RuleEngine
from task4_consistency.rules.loader import load_rules
from task4_consistency.evaluate import evaluate_directory

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "configs" / "rules_auto_lease.yaml"


def test_fuzzy_band_boundary():
    # exact
    assert fuzzy_match("张三", "张三", 0.88, 0.12).match
    # hard mismatch 2-char (ratio 0.5) -> inconsistent, not uncertain
    out = fuzzy_match("张三", "张山", 0.88, 0.12)
    assert not out.match
    assert not out.uncertain
    # 4-char near: 欧阳修文 vs 欧阳修武 ~0.75 -> uncertain with adaptive band
    out2 = fuzzy_match("欧阳修文", "欧阳修武", 0.88, 0.12)
    assert not out2.match
    assert out2.uncertain
    # long near threshold
    a, b = "ABCDEFGH", "ABCDEFGG"
    r = fuzzy_ratio(a, b)
    assert 0.83 <= r < 0.88
    out3 = fuzzy_match(a, b, 0.88, 0.05, adaptive_band=False)
    assert out3.uncertain


def test_adaptive_band_short_names():
    assert adaptive_uncertain_band("张三", "李四", 0.05) >= 0.25
    assert adaptive_uncertain_band("欧阳修文", "欧阳修武", 0.05) >= 0.20
    assert adaptive_uncertain_band("abcdefghij", "abcdefghix", 0.05) == 0.05


def test_on_missing_skip_not_in_coverage():
    eng = RuleEngine(load_rules(RULES))
    # no brand/model fields -> R_BRAND_CROSS / R_MODEL_CROSS skipped
    app = Application(
        "S",
        [
            Document("r", "机动车登记证书", {"vin": FieldValue("LSVAA4182N2123456", 0.99)}),
            Document("p", "交强险保单", {"vin": FieldValue("LSVAA4182N2123456", 0.99)}),
            Document("l", "融资租赁合同", {"vin": FieldValue("LSVAA4182N2123456", 0.99)}),
            Document("i", "发票", {"vin": FieldValue("LSVAA4182N2123456", 0.99)}),
        ],
    )
    report = eng.run(app)
    by = {c.rule_id: c for c in report.checks}
    assert by["R_BRAND_CROSS"].verdict == Verdict.SKIPPED
    assert by["R_MODEL_CROSS"].verdict == Verdict.SKIPPED
    # skipped excluded from coverage denominator
    assert report.summary.skipped >= 2
    assert report.summary.total == (
        report.summary.consistent
        + report.summary.inconsistent
        + report.summary.uncertain
    )
    assert report.summary.total_including_skipped == len(report.checks)


def test_brand_model_rules():
    eng = RuleEngine(load_rules(RULES))
    app = Application(
        "B",
        [
            Document(
                "r",
                "机动车登记证书",
                {
                    "brand": FieldValue("一汽-大众牌", 0.99),
                    "model": FieldValue("Passat 380", 0.99),
                },
            ),
            Document(
                "p",
                "交强险保单",
                {
                    "brand": FieldValue("一汽大众", 0.99),
                    "model": FieldValue("PASSAT380", 0.99),
                },
            ),
        ],
    )
    by = {c.rule_id: c for c in eng.run(app).checks}
    assert by["R_BRAND_CROSS"].verdict == Verdict.CONSISTENT
    assert by["R_MODEL_CROSS"].verdict == Verdict.CONSISTENT

def test_html_report_contains_app_id():
    eng = RuleEngine(load_rules(RULES))
    app = Application.from_dict(
        {
            "application_id": "HTML-1",
            "documents": [
                {
                    "doc_id": "r",
                    "doc_type": "机动车登记证书",
                    "fields": {"vin": {"raw": "LSVAA4182N2123456", "confidence": 0.99}},
                },
                {
                    "doc_id": "p",
                    "doc_type": "交强险保单",
                    "fields": {"vin": {"raw": "LSVAA4182N2123456", "confidence": 0.99}},
                },
                {
                    "doc_id": "l",
                    "doc_type": "融资租赁合同",
                    "fields": {"vin": {"raw": "LSVAA4182N2123456", "confidence": 0.99}},
                },
                {
                    "doc_id": "i",
                    "doc_type": "发票",
                    "fields": {"vin": {"raw": "LSVAA4182N2123456", "confidence": 0.99}},
                },
            ],
        }
    )
    html = report_to_html(eng.run(app))
    assert "HTML-1" in html
    assert "R_VIN_CROSS" in html
    assert "<table>" in html


def test_ocr_fullwidth_normalize():
    assert normalize_field("苏Ａ·１２３４５", "plate_no") == "苏A12345"
    assert normalize_field("张　三", "owner_name") == "张三"
    vin = normalize_field("LSVAA 4182 N2123456", "vin")
    assert vin == "LSVAA4182N2123456"


def test_evaluate_n_inconsistent_ge_15():
    m = evaluate_directory(
        ROOT / "fixtures" / "applications",
        RULES,
        ROOT / "fixtures" / "labels" / "expected_verdicts.json",
    )
    assert m.n_inconsistent_labeled_decisive >= 15
    assert m.coverage >= 0.80
    assert m.false_positive_rate <= 0.05
    assert m.false_negative_rate <= 0.03
