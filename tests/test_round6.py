"""Round6: ID 15/18 link, short-name confusable, VIN check digit option."""

from pathlib import Path

from task4_consistency.match.fuzzy import fuzzy_match, single_confusable_diff
from task4_consistency.models import Application, Document, FieldValue, Verdict
from task4_consistency.normalize.id_number import (
    id15_to_id18,
    ids_link_equivalent,
    make_valid_id18,
    normalize_id_number,
)
from task4_consistency.normalize.vin import (
    is_valid_vin_check_digit,
    normalize_vin_ex,
    vin_check_digit,
)
from task4_consistency.rules.engine import RuleEngine
from task4_consistency.rules.loader import load_rules
from task4_consistency.evaluate import evaluate_directory

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "configs" / "rules_auto_lease.yaml"


def test_id15_to_18_link():
    body = "320102900101123"  # 15-digit style YY=90 → 1990
    # craft valid 15 then expand
    id15 = "320102900101123"
    assert len(id15) == 15
    id18 = id15_to_id18(id15)
    assert id18 is not None and len(id18) == 18
    assert is_valid_cn_id18_local(id18)
    assert normalize_id_number(id15) == id18
    assert ids_link_equivalent(id15, id18)


def is_valid_cn_id18_local(x: str) -> bool:
    from task4_consistency.normalize.id_number import is_valid_cn_id18

    return is_valid_cn_id18(x)


def test_id15_18_cross_doc_consistent():
    id15 = "110101900101001"
    id18 = id15_to_id18(id15)
    assert id18
    eng = RuleEngine(load_rules(RULES))
    app = Application(
        "idlink",
        [
            Document("c", "融资租赁合同", {"id_number": FieldValue(id15, 0.99)}),
            Document("i", "身份证", {"id_number": FieldValue(id18, 0.99)}),
        ],
    )
    c = next(x for x in eng.run(app).checks if x.rule_id == "R_ID_EXACT")
    assert c.verdict == Verdict.CONSISTENT


def test_short_name_confusable_uncertain():
    assert single_confusable_diff("张伟", "张玮")
    out = fuzzy_match("张伟", "张玮", 0.88, 0.12)
    assert not out.match and out.uncertain
    # hard mismatch still inconsistent
    hard = fuzzy_match("张三", "李四", 0.88, 0.12)
    assert not hard.match and not hard.uncertain


def test_name_confusable_engine():
    eng = RuleEngine(load_rules(RULES))
    app = Application(
        "nm",
        [
            Document("a", "机动车登记证书", {"owner_name": FieldValue("张伟", 0.99)}),
            Document("b", "交强险保单", {"insured_name": FieldValue("张玮", 0.99)}),
            Document("c", "融资租赁合同", {"lessee_name": FieldValue("张伟", 0.99)}),
            Document("d", "身份证", {"owner_name": FieldValue("张伟", 0.99)}),
        ],
    )
    c = next(x for x in eng.run(app).checks if x.rule_id == "R_NAME_FUZZY")
    assert c.verdict == Verdict.UNCERTAIN


def test_vin_strict_check_digit_mode():
    # synthetic LSVAA4182N2123456 — may fail check digit
    vin = "LSVAA4182N2123456"
    loose = normalize_vin_ex(vin, strict_check_digit=False)
    assert loose.value == vin
    strict = normalize_vin_ex(vin, strict_check_digit=True)
    # if check fails under strict → None
    if not is_valid_vin_check_digit(vin):
        assert strict.value is None
        assert "vin_check_digit_fail" in strict.notes
    # known algorithm: recompute
    cd = vin_check_digit(vin)
    assert cd is not None and len(cd) == 1


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
    assert m.n_inconsistent_labeled_decisive >= 15
