"""Adversarial regressions (Round2.1). See docs/ATTACK_CASES.md."""

from __future__ import annotations

from pathlib import Path

from task4_consistency.evaluate import evaluate_directory
from task4_consistency.match.fuzzy import fuzzy_match
from task4_consistency.match.numeric import numeric_tolerance_match
from task4_consistency.models import Application, Document, FieldValue, Verdict
from task4_consistency.normalize.base import normalize_field
from task4_consistency.normalize.date import normalize_date
from task4_consistency.rules.engine import RuleEngine
from task4_consistency.rules.loader import load_rules

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "configs" / "rules_auto_lease.yaml"


def _eng() -> RuleEngine:
    return RuleEngine(load_rules(RULES))


def _verdict(app: Application, rule_id: str) -> Verdict:
    for c in _eng().run(app).checks:
        if c.rule_id == rule_id:
            return c.verdict
    raise AssertionError(f"rule not found: {rule_id}")


def test_a1_vin_single_char_diff_inconsistent():
    """A1: one-char VIN tamper must be caught."""
    v1, v2 = "LSVAA4182N2123456", "LSVAA4182N2123458"
    app = Application(
        "A1",
        [
            Document("a", "机动车登记证书", {"vin": FieldValue(v1, 0.99)}),
            Document("b", "交强险保单", {"vin": FieldValue(v2, 0.99)}),
            Document("c", "融资租赁合同", {"vin": FieldValue(v1, 0.99)}),
            Document("d", "发票", {"vin": FieldValue(v1, 0.99)}),
        ],
    )
    assert _verdict(app, "R_VIN_CROSS") == Verdict.INCONSISTENT


def test_a2_amount_rel_tol_finance_stricter():
    """A2: finance default rel_tol=0.0001 (Round5); abs_tol=1 still for fen."""
    ok = numeric_tolerance_match("100000", "100000.5", abs_tol=1.0, rel_tol=0.0001)
    assert ok.match is True
    # 0.1% gap no longer tolerated under 0.0001
    bad = numeric_tolerance_match("1000000", "1001000", abs_tol=1.0, rel_tol=0.0001)
    assert bad.match is False


def test_a3_name_hard_vs_near_band():
    hard = fuzzy_match("张三", "张山", 0.88, 0.12)
    assert not hard.match and not hard.uncertain
    near = fuzzy_match("欧阳修文", "欧阳修武", 0.88, 0.12)
    assert not near.match and near.uncertain


def test_a4_brand_model_skip_excluded_from_coverage():
    app = Application(
        "A4",
        [
            Document("a", "机动车登记证书", {"vin": FieldValue("LSVAA4182N2123456", 0.99)}),
            Document("b", "交强险保单", {"vin": FieldValue("LSVAA4182N2123456", 0.99)}),
            Document("c", "融资租赁合同", {"vin": FieldValue("LSVAA4182N2123456", 0.99)}),
            Document("d", "发票", {"vin": FieldValue("LSVAA4182N2123456", 0.99)}),
        ],
    )
    rep = _eng().run(app)
    by = {c.rule_id: c for c in rep.checks}
    assert by["R_BRAND_CROSS"].verdict == Verdict.SKIPPED
    assert by["R_MODEL_CROSS"].verdict == Verdict.SKIPPED
    assert rep.summary.skipped >= 2
    assert rep.summary.total == (
        rep.summary.consistent + rep.summary.inconsistent + rep.summary.uncertain
    )


def test_a5_brand_joint_venture_prefix_not_collapsed():
    """ADV-01 CLOSED: JV prefixes kept; 一汽大众 ≠ 上汽大众."""
    a = normalize_field("一汽-大众", "brand")
    b = normalize_field("上汽大众", "brand")
    assert a == "一汽大众"
    assert b == "上汽大众"
    assert a != b
    app = Application(
        "A5",
        [
            Document("a", "机动车登记证书", {"brand": FieldValue("一汽大众", 0.99)}),
            Document("b", "交强险保单", {"brand": FieldValue("上汽大众", 0.99)}),
        ],
    )
    assert _verdict(app, "R_BRAND_CROSS") == Verdict.INCONSISTENT


def test_a6_date_dmy_policy():
    assert normalize_date("13/04/2024") == "2024-04-13"  # unambiguous DMY
    # Ambiguous both ≤12 → None (ADV-03)
    assert normalize_date("03/04/2024") is None
    assert normalize_date("01/02/2023") is None

def test_a7_plate_not_in_list_inconsistent():
    app = Application(
        "A7",
        [
            Document("r", "机动车登记证书", {"plate_no": FieldValue("苏A12345", 0.99)}),
            Document(
                "p",
                "交强险保单",
                {
                    "plate_no": FieldValue("苏A12345", 0.99),
                    "plate_list": FieldValue("苏B99999|苏C00000", 0.99),
                },
            ),
        ],
    )
    assert _verdict(app, "R_PLATE_IN_LIST") == Verdict.INCONSISTENT


def test_a8_one_low_confidence_vin_uncertain():
    v = "LSVAA4182N2123456"
    app = Application(
        "A8",
        [
            Document("a", "机动车登记证书", {"vin": FieldValue(v, 0.99)}),
            Document("b", "交强险保单", {"vin": FieldValue(v, 0.99)}),
            Document("c", "融资租赁合同", {"vin": FieldValue(v, 0.55)}),
            Document("d", "发票", {"vin": FieldValue(v, 0.99)}),
        ],
    )
    assert _verdict(app, "R_VIN_CROSS") == Verdict.UNCERTAIN


def test_a12_evaluate_zero_mismatches_and_tp_ge_15():
    m = evaluate_directory(
        ROOT / "fixtures" / "applications",
        RULES,
        ROOT / "fixtures" / "labels" / "expected_verdicts.json",
    )
    mismatches = [p for p in m.pairs if p["expected"] != p["predicted"]]
    assert mismatches == []
    assert m.true_positive >= 15
    assert m.n_inconsistent_labeled_decisive >= 15
    assert m.coverage >= 0.80
    assert m.false_positive_rate <= 0.05
    assert m.false_negative_rate <= 0.03


def test_adv13_money_sci_notation_fixed():
    """ADV-13 CLOSED: scientific notation parses full magnitude."""
    from task4_consistency.normalize.money import normalize_money

    assert normalize_money("1.28e6") == "1280000"
    app = Application(
        "ADV13",
        [
            Document("c", "融资租赁合同", {"financed_amount": FieldValue("1.28e6", 0.99)}),
            Document("i", "发票", {"invoice_amount": FieldValue("1.28", 0.99)}),
        ],
    )
    assert _verdict(app, "R_AMOUNT_TOL") == Verdict.INCONSISTENT


def test_adv14_miss_rate_gated_in_pass_thresholds():
    """ADV-14 CLOSED: miss_rate is a hard pass_thresholds gate (default ≤0.10)."""
    from task4_consistency.evaluate import EvalPair, compute_metrics

    pairs = [
        EvalPair("a", "R1", "inconsistent", "uncertain"),
        EvalPair("b", "R1", "inconsistent", "uncertain"),
        EvalPair("c", "R1", "consistent", "consistent"),
    ]
    m = compute_metrics(pairs, [1.0, 1.0, 1.0])
    assert m.false_negative_rate == 0.0
    assert m.miss_rate == 1.0
    assert any(k.startswith("miss_rate<=") for k in m.pass_thresholds)
    assert m.pass_thresholds.get("miss_rate<=0.1") is False

    # under threshold → pass
    m2 = compute_metrics(
        [
            EvalPair("a", "R1", "inconsistent", "inconsistent"),
            EvalPair("b", "R1", "consistent", "consistent"),
        ],
        [1.0, 1.0],
    )
    assert m2.miss_rate == 0.0
    assert m2.pass_thresholds.get("miss_rate<=0.1") is True
