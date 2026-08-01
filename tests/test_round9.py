"""Round9 P2: underscore money, placeholders, ID15/18, VIN strict default."""

from pathlib import Path

from task4_consistency.models import Application, Document, FieldValue, Verdict
from task4_consistency.normalize.base import normalize_field_ex
from task4_consistency.normalize.id_number import id15_to_id18, normalize_id_number
from task4_consistency.normalize.money import normalize_money
from task4_consistency.normalize.vin import normalize_vin_ex
from task4_consistency.rules.engine import RuleEngine
from task4_consistency.rules.loader import load_rules
from task4_consistency.evaluate import evaluate_directory

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "configs" / "rules_auto_lease.yaml"


def test_adv16_underscore_money():
    assert normalize_money("1_280_000") == "1280000"
    eng = RuleEngine(load_rules(RULES))
    app = Application(
        "u",
        [
            Document("c", "融资租赁合同", {"financed_amount": FieldValue("1_280_000", 0.99)}),
            Document("i", "发票", {"invoice_amount": FieldValue("1", 0.99)}),
        ],
    )
    c = next(x for x in eng.run(app).checks if x.rule_id == "R_AMOUNT_TOL")
    assert c.verdict == Verdict.INCONSISTENT


def test_adv17_placeholder_engine_uncertain():
    nr = normalize_field_ex("无", field_name="engine_no")
    assert nr.value is None
    assert "placeholder_value" in nr.notes
    eng = RuleEngine(load_rules(RULES))
    app = Application(
        "p",
        [
            Document("a", "机动车登记证书", {"engine_no": FieldValue("无", 0.99)}),
            Document("b", "交强险保单", {"engine_no": FieldValue("EA888", 0.99)}),
            Document("c", "发票", {"engine_no": FieldValue("EA888", 0.99)}),
        ],
    )
    c = next(x for x in eng.run(app).checks if x.rule_id == "R_ENGINE_CROSS")
    assert c.verdict == Verdict.UNCERTAIN
    assert "PLACEHOLDER_VALUE" in c.reason_codes or "placeholder_value" in c.flags


def test_adv10_id15_18_reinforced():
    id15 = "110101900101001"
    id18 = id15_to_id18(id15)
    assert normalize_id_number(id15) == id18
    eng = RuleEngine(load_rules(RULES))
    app = Application(
        "id",
        [
            Document("c", "融资租赁合同", {"id_number": FieldValue(id15, 0.99)}),
            Document("i", "身份证", {"id_number": FieldValue(id18, 0.99)}),
        ],
    )
    c = next(x for x in eng.run(app).checks if x.rule_id == "R_ID_EXACT")
    assert c.verdict == Verdict.CONSISTENT


def test_adv15_vin_strict_default_off():
    cfg = load_rules(RULES)
    assert cfg.vin_strict_check_digit is False
    loose = normalize_vin_ex("LSVAA4182N2123456", strict_check_digit=False)
    assert loose.value == "LSVAA4182N2123456"


def test_metrics_stable():
    m = evaluate_directory(
        ROOT / "fixtures" / "applications",
        RULES,
        ROOT / "fixtures" / "labels" / "expected_verdicts.json",
    )
    assert m.coverage >= 0.80
    assert m.false_positive_rate <= 0.05
    assert m.false_negative_rate <= 0.03
    assert m.miss_rate <= 0.10
