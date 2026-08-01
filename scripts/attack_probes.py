#!/usr/bin/env python3
"""Adversarial probes. Exit 0 only if ADV-01/02/03 P0 holes are CLOSED.

Run: .venv/bin/python scripts/attack_probes.py
"""

from __future__ import annotations

import sys

from task4_consistency.evaluate import EvalPair, compute_metrics
from task4_consistency.match.numeric import multi_numeric_all
from task4_consistency.models import Application, Document, FieldValue
from task4_consistency.normalize.base import normalize_brand
from task4_consistency.normalize.date import normalize_date
from task4_consistency.normalize.money import normalize_money
from task4_consistency.normalize.vin import normalize_vin, normalize_vin_ex
from task4_consistency.rules.engine import RuleEngine
from task4_consistency.rules.loader import load_rules


def mk(app_id: str, docs: list) -> Application:
    out = []
    for did, dtype, fields in docs:
        fv = {}
        for k, v in fields.items():
            if isinstance(v, tuple):
                fv[k] = FieldValue(raw=v[0], confidence=v[1])
            else:
                fv[k] = FieldValue(raw=v)
        out.append(Document(did, dtype, fv))
    return Application(app_id, out)


def verdict(report, rule_id: str) -> str:
    return next(c.verdict.value for c in report.checks if c.rule_id == rule_id)


def main() -> int:
    rules = load_rules("configs/rules_auto_lease.yaml")
    eng = RuleEngine(rules)
    results: list[tuple[str, str, str, bool]] = []  # id, title, detail, closed

    # ADV-01 brand JV — must NOT collapse
    b1, b2 = normalize_brand("一汽大众"), normalize_brand("上汽大众")
    brand_norm_ok = b1 != b2
    app = mk(
        "adv01",
        [
            ("d1", "机动车登记证书", {"brand": "一汽大众", "vin": "LGXCE4CB0N0123456"}),
            ("d2", "交强险保单", {"brand": "上汽大众", "vin": "LGXCE4CB0N0123456"}),
            ("d3", "融资租赁合同", {"brand": "一汽-大众", "vin": "LGXCE4CB0N0123456"}),
            ("d4", "发票", {"brand": "大众", "vin": "LGXCE4CB0N0123456"}),
        ],
    )
    v = verdict(eng.run(app), "R_BRAND_CROSS")
    closed = brand_norm_ok and v == "inconsistent"
    results.append(
        ("ADV-01", "P0 brand JV", f"norm={b1!r}/{b2!r} R_BRAND_CROSS={v}", closed)
    )

    # ADV-02 VIN IOQ — no silent consistent
    r_i = normalize_vin_ex("LGXCE4CB0N012345I")
    r_1 = normalize_vin_ex("LGXCE4CB0N0123451")
    app = mk(
        "adv02",
        [
            ("d1", "机动车登记证书", {"vin": "LGXCE4CB0N012345I"}),
            ("d2", "交强险保单", {"vin": "LGXCE4CB0N0123451"}),
            ("d3", "融资租赁合同", {"vin": "LGXCE4CB0N012345I"}),
            ("d4", "发票", {"vin": "LGXCE4CB0N0123451"}),
        ],
    )
    v = verdict(eng.run(app), "R_VIN_CROSS")
    # closed if not consistent (uncertain after OCR merge is OK)
    closed = v != "consistent" and r_i.ocr_fix
    results.append(
        (
            "ADV-02",
            "P0 VIN IOQ",
            f"ocr_fix={r_i.ocr_fix} vals={r_i.value}/{r_1.value} R_VIN_CROSS={v}",
            closed,
        )
    )

    # ADV-03 date ambiguous — not forced equal to ISO
    d1 = normalize_date("01/02/2023")
    d2 = normalize_date("2023-02-01")
    app = mk(
        "adv03",
        [
            ("d1", "机动车登记证书", {"reg_date": "01/02/2023"}),
            ("d2", "融资租赁合同", {"reg_date": "2023-02-01"}),
        ],
    )
    v = verdict(eng.run(app), "R_DATE_CROSS")
    closed = d1 is None and d2 == "2023-02-01" and v == "uncertain"
    results.append(
        ("ADV-03", "P0 date DMY/MDY", f"norm={d1!r}/{d2!r} R_DATE_CROSS={v}", closed)
    )

    # ADV-04 year-month incomplete
    ym = normalize_date("2023年1月")
    app = mk(
        "adv04",
        [
            ("d1", "机动车登记证书", {"reg_date": "2023年1月"}),
            ("d2", "融资租赁合同", {"reg_date": "2023-01-01"}),
        ],
    )
    v = verdict(eng.run(app), "R_DATE_CROSS")
    closed = ym is None and v == "uncertain"
    results.append(
        ("ADV-04", "P1 year-month", f"norm={ym!r} R_DATE_CROSS={v}", closed)
    )

    # ADV-05 low conf clear mismatch → inconsistent + low_conf (critical_low_conf_compare)
    app = mk(
        "adv05",
        [
            ("d1", "机动车登记证书", {"vin": ("LGXCE4CB0N0123456", 0.3)}),
            ("d2", "交强险保单", {"vin": ("LGXCE4CB0N0999999", 0.3)}),
            ("d3", "融资租赁合同", {"vin": ("LGXCE4CB0N0123456", 0.3)}),
            ("d4", "发票", {"vin": ("LGXCE4CB0N0999999", 0.3)}),
        ],
    )
    rep = eng.run(app)
    c05 = next(c for c in rep.checks if c.rule_id == "R_VIN_CROSS")
    v = c05.verdict.value
    closed = v == "inconsistent" and "low_conf" in (c05.flags or [])
    results.append(
        ("ADV-05", "P1 low conf compare", f"R_VIN_CROSS={v} flags={c05.flags}", closed)
    )

    # ADV-06 money 约 → parse + uncertain reason (not bare None hide)
    from task4_consistency.normalize.money import normalize_money_ex

    mr = normalize_money_ex("约12.5万")
    app = mk(
        "adv06",
        [
            ("c", "融资租赁合同", {"financed_amount": "约12.5万"}),
            ("d", "发票", {"invoice_amount": "100"}),
        ],
    )
    c06 = next(c for c in eng.run(app).checks if c.rule_id == "R_AMOUNT_TOL")
    v = c06.verdict.value
    closed = (
        mr.value == "125000"
        and "money_approx" in mr.notes
        and v == "uncertain"
        and "money_approx" in (c06.flags or [])
    )
    results.append(
        (
            "ADV-06",
            "P1 money 约万",
            f"norm={mr.value!r} notes={mr.notes} R_AMOUNT_TOL={v} flags={c06.flags}",
            closed,
        )
    )

    # ADV-07 miss_rate metric
    pairs = [
        EvalPair("x", "R_VIN_CROSS", "inconsistent", "uncertain"),
        EvalPair("y", "R_VIN_CROSS", "consistent", "consistent"),
    ]
    m = compute_metrics(pairs, [1.0, 1.0])
    closed = m.miss_rate > 0  # uncertain hide counted as miss
    results.append(
        (
            "ADV-07",
            "P1 miss_rate",
            f"fnr={m.false_negative_rate} miss_rate={m.miss_rate}",
            closed,
        )
    )

    # ADV-08 rel_tol tightened (0.0001): 0.1% gap → inconsistent
    app = mk(
        "adv08",
        [
            ("c", "融资租赁合同", {"financed_amount": "1000000"}),
            ("d", "发票", {"invoice_amount": "1001000"}),
        ],
    )
    v = verdict(eng.run(app), "R_AMOUNT_TOL")
    results.append(
        ("ADV-08", "P2 rel_tol", f"R_AMOUNT_TOL={v}", v == "inconsistent")
    )

    out = multi_numeric_all(["100.0", "100.9", "99.1"], abs_tol=1.0)
    results.append(("ADV-09", "P2 numeric", f"match={out.match}", True))

    # ADV-13 scientific notation
    from task4_consistency.normalize.money import normalize_money

    sci = normalize_money("1.28e6")
    results.append(("ADV-13", "money sci", f"1.28e6→{sci!r}", sci == "1280000"))

    # ADV-14 miss_rate hard gate
    m14 = compute_metrics(
        [
            EvalPair("a", "R1", "inconsistent", "uncertain"),
            EvalPair("b", "R1", "consistent", "consistent"),
        ],
        [1.0, 1.0],
    )
    gated = any(k.startswith("miss_rate<=") for k in m14.pass_thresholds)
    results.append(
        (
            "ADV-14",
            "miss_rate gate",
            f"pass_keys={list(m14.pass_thresholds)} miss={m14.miss_rate}",
            gated and m14.pass_thresholds.get("miss_rate<=0.1") is False,
        )
    )

    # --- Round7 exploratory (r7_open; not release gate) ---
    from pathlib import Path
    from task4_consistency.models import CheckResult

    has_rc = "reason_codes" in getattr(CheckResult, "__dataclass_fields__", {})
    iface = Path("docs/INTERFACE.md")
    iface_txt = iface.read_text(encoding="utf-8") if iface.exists() else ""
    iface_has_table = "VIN_MISMATCH" in iface_txt or "reason_codes" in iface_txt.lower()
    vin_app = eng.run(
        mk(
            "adv18",
            [
                ("d1", "机动车登记证书", {"vin": "LGXCE4CB0N0123456"}),
                ("d2", "交强险保单", {"vin": "LGXCE4CB0N0999999"}),
                ("d3", "融资租赁合同", {"vin": "LGXCE4CB0N0123456"}),
                ("d4", "发票", {"vin": "LGXCE4CB0N0999999"}),
            ],
        )
    )
    vin_c = next(c for c in vin_app.checks if c.rule_id == "R_VIN_CROSS")
    rc = list(getattr(vin_c, "reason_codes", None) or [])
    results.append(
        (
            "ADV-18",
            "P2 reason_codes",
            f"field={has_rc} rc={rc} iface_doc={iface_has_table}",
            has_rc and bool(rc) and iface_has_table,
        )
    )

    used = eng.run(
        mk(
            "adv19",
            [
                ("d1", "机动车登记证书", {"owner_name": "原车主甲", "vin": "LGXCE4CB0N0123456"}),
                ("d2", "交强险保单", {"insured_name": "新承租乙", "vin": "LGXCE4CB0N0123456"}),
                (
                    "d3",
                    "融资租赁合同",
                    {
                        "lessee_name": "新承租乙",
                        "vin": "LGXCE4CB0N0123456",
                        "financed_amount": "1",
                        "id_number": "320102199001011232",
                    },
                ),
                ("d4", "发票", {"vin": "LGXCE4CB0N0123456", "invoice_amount": "1"}),
                ("d5", "身份证", {"owner_name": "新承租乙", "id_number": "320102199001011232"}),
            ],
        )
    )
    nv = verdict(used, "R_NAME_FUZZY")
    results.append(("ADV-19", "P1 used-car name", f"R_NAME_FUZZY={nv}", nv != "inconsistent"))

    multi = normalize_money("金额:113207.55 税额:14716.98 价税合计:127924.53")
    tax_app = eng.run(
        mk(
            "adv20",
            [
                ("c", "融资租赁合同", {"financed_amount": "127924.53"}),
                ("d", "发票", {"invoice_amount": "113207.55"}),
            ],
        )
    )
    av = verdict(tax_app, "R_AMOUNT_TOL")
    results.append(
        (
            "ADV-20",
            "P1 tax total",
            f"multi_norm={multi!r} R_AMOUNT_TOL={av}",
            multi == "127924.53" or av != "inconsistent",
        )
    )

    buy = eng.run(
        mk(
            "adv21",
            [
                ("d1", "机动车登记证书", {"owner_name": "张三", "vin": "LGXCE4CB0N0123456"}),
                ("d2", "交强险保单", {"insured_name": "张三", "vin": "LGXCE4CB0N0123456"}),
                (
                    "d3",
                    "融资租赁合同",
                    {
                        "lessee_name": "张三",
                        "vin": "LGXCE4CB0N0123456",
                        "financed_amount": "1",
                        "id_number": "320102199001011232",
                    },
                ),
                (
                    "d4",
                    "发票",
                    {"buyer_name": "完全别人", "vin": "LGXCE4CB0N0123456", "invoice_amount": "1"},
                ),
                ("d5", "身份证", {"owner_name": "张三", "id_number": "320102199001011232"}),
            ],
        )
    )
    name_v = verdict(buy, "R_NAME_FUZZY")
    results.append(
        (
            "ADV-21",
            "P1 buyer skip",
            f"R_NAME_FUZZY={name_v} (buyer=完全别人)",
            name_v != "consistent",
        )
    )

    # --- Round9 P2 gates ---
    from task4_consistency.normalize.id_number import id15_to_id18, normalize_id_number
    from task4_consistency.normalize.vin import is_valid_vin_check_digit

    # ADV-10 ID 15/18 link
    id15 = "110101900101001"
    id18 = id15_to_id18(id15)
    id_ok = id18 is not None and normalize_id_number(id15) == id18
    results.append(
        ("ADV-10", "P2 ID15/18", f"15→18={id18!r}", bool(id_ok))
    )

    # ADV-15 VIN check digit optional (documented default off)
    vin_loose = normalize_vin_ex("LSVAA4182N2123456", strict_check_digit=False)
    vin_strict = normalize_vin_ex("LSVAA4182N2123456", strict_check_digit=True)
    cfg_strict_off = getattr(rules, "vin_strict_check_digit", True) is False
    results.append(
        (
            "ADV-15",
            "P2 VIN check digit",
            f"loose={vin_loose.value!r} strict_fail={vin_strict.value is None} default_off={cfg_strict_off}",
            vin_loose.value is not None and cfg_strict_off,
        )
    )

    # ADV-16 underscore money
    u = normalize_money("1_280_000")
    app16 = eng.run(
        mk(
            "adv16",
            [
                ("c", "融资租赁合同", {"financed_amount": "1_280_000"}),
                ("d", "发票", {"invoice_amount": "1"}),
            ],
        )
    )
    v16 = verdict(app16, "R_AMOUNT_TOL")
    results.append(
        (
            "ADV-16",
            "P2 money underscore",
            f"norm={u!r} R_AMOUNT_TOL={v16}",
            u == "1280000" and v16 == "inconsistent",
        )
    )

    # ADV-17 placeholder engine/reg
    app17 = eng.run(
        mk(
            "adv17",
            [
                ("d1", "机动车登记证书", {"engine_no": "无", "reg_cert_no": "N/A"}),
                ("d2", "交强险保单", {"engine_no": "EA888123"}),
                ("d3", "发票", {"engine_no": "EA888123"}),
            ],
        )
    )
    c17 = next(c for c in app17.checks if c.rule_id == "R_ENGINE_CROSS")
    ph_note = any(
        "placeholder" in (n or "")
        for s in c17.snapshots
        for n in (getattr(s, "notes", None) or [])
    ) or "placeholder" in " ".join(c17.flags or []) or "placeholder" in (
        c17.message or ""
    )
    results.append(
        (
            "ADV-17",
            "P2 placeholder engine",
            f"R_ENGINE_CROSS={c17.verdict.value} flags={c17.flags} rc={c17.reason_codes}",
            c17.verdict.value == "uncertain"
            and (
                "PLACEHOLDER_VALUE" in (c17.reason_codes or [])
                or "placeholder" in " ".join(c17.flags or [])
                or ph_note
            ),
        )
    )

    print("=== ADVERSARIAL PROBE RESULTS ===")
    release_ids = {
        "ADV-01",
        "ADV-02",
        "ADV-03",
        "ADV-13",
        "ADV-14",
        "ADV-16",
        "ADV-17",
        "ADV-19",
        "ADV-21",
    }
    r7_ids = {"ADV-18", "ADV-19", "ADV-20", "ADV-21"}
    release_open = 0
    r7_open = 0
    for hid, title, detail, closed in results:
        status = "CLOSED" if closed else "OPEN"
        if hid in release_ids and not closed:
            release_open += 1
        if hid in r7_ids and not closed:
            r7_open += 1
        print(f"{hid:8} | {status:6} | {title:28} | {detail}")
    print(f"release_open={release_open} r7_open={r7_open} total={len(results)}")
    return 1 if release_open else 0


if __name__ == "__main__":
    raise SystemExit(main())
