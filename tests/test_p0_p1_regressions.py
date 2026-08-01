"""Round1 regressions for REVIEW P0/P1 findings."""

from pathlib import Path

from task4_consistency.models import Application, Document, FieldValue
from task4_consistency.normalize.date import normalize_date
from task4_consistency.normalize.id_number import is_valid_cn_id18, normalize_id_number
from task4_consistency.normalize.money import normalize_money
from task4_consistency.normalize.plate import normalize_plate, normalize_plate_list
from task4_consistency.normalize.vin import is_valid_vin, normalize_vin
from task4_consistency.rules.engine import RuleEngine
from task4_consistency.rules.loader import load_rules

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "configs" / "rules_auto_lease.yaml"


def test_p0_plate_list_dot_vs_plate_no():
    """plate_list keeps · under generic path historically; must not false-positive."""
    eng = RuleEngine(load_rules(RULES))
    app = Application(
        "X",
        [
            Document("r", "机动车登记证书", {"plate_no": FieldValue("苏A·12345", 0.99)}),
            Document(
                "p",
                "交强险保单",
                {
                    "plate_no": FieldValue("苏A12345", 0.99),
                    "plate_list": FieldValue("苏A·12345", 0.99),
                },
            ),
        ],
    )
    by = {c.rule_id: c for c in eng.run(app).checks}
    assert by["R_PLATE_IN_LIST"].verdict.value == "consistent"


def test_p0_date_dmy_not_none():
    # Ambiguous 01/02/2023 → None (ADV-03); unambiguous day>12 still works
    assert normalize_date("01/02/2023") is None
    assert normalize_date("13/02/2023") == "2023-02-13"
    assert normalize_date("2023/01/02") == "2023-01-02"
    assert normalize_date("20230102") == "2023-01-02"
    assert normalize_date("2023.2.1") == "2023-02-01"
    assert normalize_date("2023年2月1日") == "2023-02-01"


def test_p1_money_mixed_cn_units():
    assert normalize_money("12万8千") == "128000"
    assert normalize_money("12.8万") == "128000"
    assert normalize_money("十二万八千") == "128000"
    assert normalize_money("1百万") == "1000000"


def test_p1_vin_validate_rejects_short():
    assert normalize_vin("ABC123") is None
    assert not is_valid_vin("ABC123")
    ok = normalize_vin("LSVAA4182N2123456")
    assert ok == "LSVAA4182N2123456"
    assert is_valid_vin(ok)


def test_p1_id_checksum_validate():
    assert normalize_id_number("32010219900101123X") is None or is_valid_cn_id18(
        normalize_id_number("32010219900101123X") or ""
    )
    # clearly wrong length
    assert normalize_id_number("12345") is None
    # valid synthetic
    from task4_consistency.normalize.id_number import make_valid_id18

    vid = make_valid_id18("32010219900101123")
    assert normalize_id_number(vid) == vid
    assert is_valid_cn_id18(vid)


def test_p1_require_all_docs_missing_vin():
    eng = RuleEngine(load_rules(RULES))
    app = Application(
        "Y",
        [
            Document("r", "机动车登记证书", {"vin": FieldValue("LSVAA4182N2123456", 0.99)}),
            Document("p", "交强险保单", {"vin": FieldValue("LSVAA4182N2123456", 0.99)}),
            Document("l", "融资租赁合同", {"financed_amount": FieldValue("1元", 0.99)}),
            # invoice missing entirely
        ],
    )
    by = {c.rule_id: c for c in eng.run(app).checks}
    assert by["R_VIN_CROSS"].verdict.value == "uncertain"
    assert "require_all_docs" in by["R_VIN_CROSS"].message or "齐套" in by["R_VIN_CROSS"].message


def test_p1_reg_date_not_aliased_to_contract_date():
    eng = RuleEngine(load_rules(RULES))
    app = Application(
        "Z",
        [
            Document(
                "r",
                "机动车登记证书",
                {"reg_date": FieldValue("2024-01-15", 0.99)},
            ),
            Document(
                "l",
                "融资租赁合同",
                {
                    "contract_date": FieldValue("2024-03-20", 0.99),
                    # no reg_date — should be missing/uncertain, NOT inconsistent vs contract
                },
            ),
        ],
    )
    by = {c.rule_id: c for c in eng.run(app).checks}
    assert by["R_DATE_CROSS"].verdict.value != "inconsistent"


def test_plate_list_normalizer():
    assert normalize_plate("苏A·12345") == "苏A12345"
    assert normalize_plate_list("苏A·12345|苏B·1") == "苏A12345|苏B1"


def test_loader_schema_rejects_bad_rule(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 1\nrules:\n  - name: x\n    type: exact\n", encoding="utf-8")
    import pytest
    from task4_consistency.rules.loader import load_rules

    with pytest.raises(ValueError, match="id"):
        load_rules(bad)
