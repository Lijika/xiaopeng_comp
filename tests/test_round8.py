"""Round8: used-car name transfer + buyer_name inclusion."""

from pathlib import Path

from task4_consistency.models import Application, Document, FieldValue, Verdict
from task4_consistency.normalize.base import normalize_engine
from task4_consistency.normalize.money import normalize_money
from task4_consistency.rules.engine import RuleEngine
from task4_consistency.rules.loader import load_rules
from task4_consistency.evaluate import evaluate_directory

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "configs" / "rules_auto_lease.yaml"


def test_adv19_used_car_name_uncertain():
    eng = RuleEngine(load_rules(RULES))
    app = Application(
        "adv19",
        [
            Document(
                "d1",
                "机动车登记证书",
                {
                    "owner_name": FieldValue("原车主甲", 0.99),
                    "vin": FieldValue("LGXCE4CB0N0123456", 0.99),
                },
            ),
            Document(
                "d2",
                "交强险保单",
                {
                    "insured_name": FieldValue("新承租乙", 0.99),
                    "vin": FieldValue("LGXCE4CB0N0123456", 0.99),
                },
            ),
            Document(
                "d3",
                "融资租赁合同",
                {
                    "lessee_name": FieldValue("新承租乙", 0.99),
                    "vin": FieldValue("LGXCE4CB0N0123456", 0.99),
                    "financed_amount": FieldValue("1", 0.99),
                    "id_number": FieldValue("320102199001011232", 0.99),
                },
            ),
            Document(
                "d4",
                "发票",
                {
                    "vin": FieldValue("LGXCE4CB0N0123456", 0.99),
                    "invoice_amount": FieldValue("1", 0.99),
                },
            ),
            Document(
                "d5",
                "身份证",
                {
                    "owner_name": FieldValue("新承租乙", 0.99),
                    "id_number": FieldValue("320102199001011232", 0.99),
                },
            ),
        ],
    )
    c = next(x for x in eng.run(app).checks if x.rule_id == "R_NAME_FUZZY")
    assert c.verdict == Verdict.UNCERTAIN
    assert "used_car_name_transfer" in c.flags
    assert "USED_CAR_NAME_TRANSFER" in c.reason_codes


def test_adv21_buyer_not_silent_consistent():
    eng = RuleEngine(load_rules(RULES))
    app = Application(
        "adv21",
        [
            Document(
                "d1",
                "机动车登记证书",
                {"owner_name": FieldValue("张三", 0.99), "vin": FieldValue("LGXCE4CB0N0123456", 0.99)},
            ),
            Document(
                "d2",
                "交强险保单",
                {
                    "insured_name": FieldValue("张三", 0.99),
                    "vin": FieldValue("LGXCE4CB0N0123456", 0.99),
                },
            ),
            Document(
                "d3",
                "融资租赁合同",
                {
                    "lessee_name": FieldValue("张三", 0.99),
                    "vin": FieldValue("LGXCE4CB0N0123456", 0.99),
                    "financed_amount": FieldValue("1", 0.99),
                    "id_number": FieldValue("320102199001011232", 0.99),
                },
            ),
            Document(
                "d4",
                "发票",
                {
                    "buyer_name": FieldValue("完全别人", 0.99),
                    "vin": FieldValue("LGXCE4CB0N0123456", 0.99),
                    "invoice_amount": FieldValue("1", 0.99),
                },
            ),
            Document(
                "d5",
                "身份证",
                {
                    "owner_name": FieldValue("张三", 0.99),
                    "id_number": FieldValue("320102199001011232", 0.99),
                },
            ),
        ],
    )
    c = next(x for x in eng.run(app).checks if x.rule_id == "R_NAME_FUZZY")
    assert c.verdict != Verdict.CONSISTENT


def test_money_underscore_and_placeholder_engine():
    assert normalize_money("1_280_000") == "1280000"
    assert normalize_engine("无") is None
    assert normalize_engine("N/A") is None


def test_metrics_no_regression():
    m = evaluate_directory(
        ROOT / "fixtures" / "applications",
        RULES,
        ROOT / "fixtures" / "labels" / "expected_verdicts.json",
    )
    assert m.coverage >= 0.80
    assert m.false_positive_rate <= 0.05
    assert m.false_negative_rate <= 0.03
    assert m.miss_rate <= 0.10
