"""Round3: close ADV-01/02/03 + miss_rate + handler registry."""

from pathlib import Path

from task4_consistency.evaluate import EvalPair, compute_metrics, evaluate_directory
from task4_consistency.models import Application, Document, FieldValue, Verdict
from task4_consistency.normalize.base import normalize_brand, normalize_field
from task4_consistency.normalize.date import normalize_date
from task4_consistency.normalize.vin import normalize_vin_ex
from task4_consistency.rules.engine import RuleEngine, _HANDLER_REGISTRY
from task4_consistency.rules.loader import load_rules

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "configs" / "rules_auto_lease.yaml"


def test_adv01_brand_jv_inconsistent():
    assert normalize_brand("一汽大众") != normalize_brand("上汽大众")
    eng = RuleEngine(load_rules(RULES))
    app = Application(
        "adv01",
        [
            Document("a", "机动车登记证书", {"brand": FieldValue("一汽大众", 0.99)}),
            Document("b", "交强险保单", {"brand": FieldValue("上汽大众", 0.99)}),
        ],
    )
    by = {c.rule_id: c for c in eng.run(app).checks}
    assert by["R_BRAND_CROSS"].verdict == Verdict.INCONSISTENT


def test_adv02_vin_ioq_uncertain_not_silent_consistent():
    a = normalize_vin_ex("LGXCE4CB0N012345I")
    b = normalize_vin_ex("LGXCE4CB0N0123451")
    assert a.value == b.value
    assert a.ocr_fix is True
    eng = RuleEngine(load_rules(RULES))
    app = Application(
        "adv02",
        [
            Document("a", "机动车登记证书", {"vin": FieldValue("LGXCE4CB0N012345I", 0.99)}),
            Document("b", "交强险保单", {"vin": FieldValue("LGXCE4CB0N0123451", 0.99)}),
            Document("c", "融资租赁合同", {"vin": FieldValue("LGXCE4CB0N012345I", 0.99)}),
            Document("d", "发票", {"vin": FieldValue("LGXCE4CB0N0123451", 0.99)}),
        ],
    )
    by = {c.rule_id: c for c in eng.run(app).checks}
    assert by["R_VIN_CROSS"].verdict == Verdict.UNCERTAIN
    assert "ocr" in by["R_VIN_CROSS"].message.lower() or "OCR" in by["R_VIN_CROSS"].message


def test_adv03_ambiguous_date_uncertain():
    assert normalize_date("01/02/2023") is None
    eng = RuleEngine(load_rules(RULES))
    app = Application(
        "adv03",
        [
            Document("a", "机动车登记证书", {"reg_date": FieldValue("01/02/2023", 0.99)}),
            Document("b", "融资租赁合同", {"reg_date": FieldValue("2023-02-01", 0.99)}),
        ],
    )
    by = {c.rule_id: c for c in eng.run(app).checks}
    assert by["R_DATE_CROSS"].verdict == Verdict.UNCERTAIN


def test_miss_rate_counts_uncertain_hide():
    pairs = [
        EvalPair("x", "R1", "inconsistent", "uncertain"),
        EvalPair("y", "R1", "inconsistent", "consistent"),
        EvalPair("z", "R1", "inconsistent", "inconsistent"),
        EvalPair("w", "R1", "consistent", "consistent"),
    ]
    m = compute_metrics(pairs, [1.0] * 4)
    assert m.n_expected_inconsistent == 3
    assert m.n_missed_inconsistent == 2  # uncertain + consistent
    assert abs(m.miss_rate - round(2 / 3, 4)) < 1e-9
    assert m.false_negative == 1  # only decisive consistent miss


def test_handler_registry_has_core_types():
    for t in ("exact", "fuzzy", "numeric_tolerance", "list_contains", "conditional_required"):
        assert t in _HANDLER_REGISTRY


def test_atk_fixtures_in_evaluate():
    m = evaluate_directory(
        ROOT / "fixtures" / "applications",
        RULES,
        ROOT / "fixtures" / "labels" / "expected_verdicts.json",
    )
    apps = set(m.per_application)
    assert "APP-ATK-BRAND-JV" in apps
    assert "APP-ATK-VIN-IOQ" in apps
    assert "APP-ATK-DATE-AMBIG" in apps
    assert m.coverage >= 0.80
    assert m.false_positive_rate <= 0.05
    assert m.false_negative_rate <= 0.03
    assert m.n_inconsistent_labeled_decisive >= 15
