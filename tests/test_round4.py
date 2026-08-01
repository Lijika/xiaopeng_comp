"""Round4 commercial polish: low_conf compare, money approx, HTML."""

from pathlib import Path

from task4_consistency.models import Application, Document, FieldValue, Verdict
from task4_consistency.normalize.money import normalize_money_ex
from task4_consistency.report import report_to_html
from task4_consistency.rules.engine import RuleEngine
from task4_consistency.rules.loader import load_rules
from task4_consistency.evaluate import evaluate_directory

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "configs" / "rules_auto_lease.yaml"


def test_adv05_low_conf_mismatch_inconsistent():
    eng = RuleEngine(load_rules(RULES))
    app = Application(
        "lc",
        [
            Document("a", "机动车登记证书", {"vin": FieldValue("LGXCE4CB0N0123456", 0.3)}),
            Document("b", "交强险保单", {"vin": FieldValue("LGXCE4CB0N0999999", 0.3)}),
            Document("c", "融资租赁合同", {"vin": FieldValue("LGXCE4CB0N0123456", 0.3)}),
            Document("d", "发票", {"vin": FieldValue("LGXCE4CB0N0999999", 0.3)}),
        ],
    )
    c = next(x for x in eng.run(app).checks if x.rule_id == "R_VIN_CROSS")
    assert c.verdict == Verdict.INCONSISTENT
    assert "low_conf" in c.flags


def test_money_approx_parses_and_uncertain():
    r = normalize_money_ex("约12.5万")
    assert r.value == "125000"
    assert "money_approx" in r.notes
    eng = RuleEngine(load_rules(RULES))
    app = Application(
        "m",
        [
            Document("c", "融资租赁合同", {"financed_amount": FieldValue("约12.5万", 0.99)}),
            Document("d", "发票", {"invoice_amount": FieldValue("125000", 0.99)}),
        ],
    )
    c = next(x for x in eng.run(app).checks if x.rule_id == "R_AMOUNT_TOL")
    assert c.verdict == Verdict.UNCERTAIN
    assert "money_approx" in c.flags
    assert "约" in c.message or "money_approx" in c.message


def test_html_has_action_list_and_flags():
    eng = RuleEngine(load_rules(RULES))
    app = Application(
        "h",
        [
            Document("a", "机动车登记证书", {"vin": FieldValue("LGXCE4CB0N0123456", 0.3)}),
            Document("b", "交强险保单", {"vin": FieldValue("LGXCE4CB0N0999999", 0.3)}),
            Document("c", "融资租赁合同", {"vin": FieldValue("LGXCE4CB0N0123456", 0.3)}),
            Document("d", "发票", {"vin": FieldValue("LGXCE4CB0N0999999", 0.3)}),
        ],
    )
    html = report_to_html(eng.run(app))
    assert "复核行动清单" in html
    assert "low_conf" in html
    assert "Critical" in html or "critical" in html.lower()


def test_metrics_no_regression():
    m = evaluate_directory(
        ROOT / "fixtures" / "applications",
        RULES,
        ROOT / "fixtures" / "labels" / "expected_verdicts.json",
    )
    assert m.coverage >= 0.80
    assert m.false_positive_rate <= 0.05
    assert m.false_negative_rate <= 0.03
    assert m.n_inconsistent_labeled_decisive >= 15
