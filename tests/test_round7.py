"""Round7: reason_codes, package version, concurrency, metrics html, fixtures>=40."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from task4_consistency.evaluate import evaluate_directory, metrics_to_html
from task4_consistency.models import Application, Document, FieldValue, Verdict
from task4_consistency.reason_codes import VIN_MISMATCH, infer_reason_codes
from task4_consistency.rules.engine import RuleEngine
from task4_consistency.rules.loader import load_rules

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "configs" / "rules_auto_lease.yaml"
APPS = ROOT / "fixtures" / "applications"


def test_rule_package_versioned():
    cfg = load_rules(RULES)
    assert cfg.package == "auto_lease"
    assert cfg.version
    assert cfg.changelog
    eng = RuleEngine(cfg)
    app = Application(
        "pkg",
        [
            Document("a", "机动车登记证书", {"vin": FieldValue("LSVAA4182N2123456", 0.99)}),
            Document("b", "交强险保单", {"vin": FieldValue("LSVAA4182N2123456", 0.99)}),
            Document("c", "融资租赁合同", {"vin": FieldValue("LSVAA4182N2123456", 0.99)}),
            Document("d", "发票", {"vin": FieldValue("LSVAA4182N2123456", 0.99)}),
        ],
    )
    rep = eng.run(app)
    assert rep.rule_package == "auto_lease"
    assert rep.rule_config_version == cfg.version
    assert rep.rule_changelog


def test_reason_codes_on_vin_mismatch():
    eng = RuleEngine(load_rules(RULES))
    app = Application(
        "rc",
        [
            Document("a", "机动车登记证书", {"vin": FieldValue("LSVAA4182N2123456", 0.99)}),
            Document("b", "交强险保单", {"vin": FieldValue("LSVAA4182N2123458", 0.99)}),
            Document("c", "融资租赁合同", {"vin": FieldValue("LSVAA4182N2123456", 0.99)}),
            Document("d", "发票", {"vin": FieldValue("LSVAA4182N2123456", 0.99)}),
        ],
    )
    c = next(x for x in eng.run(app).checks if x.rule_id == "R_VIN_CROSS")
    assert c.verdict == Verdict.INCONSISTENT
    assert VIN_MISMATCH in c.reason_codes
    # re-infer stable
    assert VIN_MISMATCH in infer_reason_codes(c)


def test_engine_thread_safe_stateless():
    """Engine has no shared mutable per-run state; parallel runs OK."""
    cfg = load_rules(RULES)
    eng = RuleEngine(cfg)

    def one(i: int) -> str:
        app = Application(
            f"T{i}",
            [
                Document(
                    "a",
                    "机动车登记证书",
                    {"vin": FieldValue("LSVAA4182N2123456", 0.99)},
                ),
                Document(
                    "b",
                    "交强险保单",
                    {
                        "vin": FieldValue(
                            "LSVAA4182N2123456" if i % 2 == 0 else "LSVAA4182N2123458",
                            0.99,
                        )
                    },
                ),
                Document(
                    "c",
                    "融资租赁合同",
                    {"vin": FieldValue("LSVAA4182N2123456", 0.99)},
                ),
                Document("d", "发票", {"vin": FieldValue("LSVAA4182N2123456", 0.99)}),
            ],
        )
        rep = eng.run(app)
        return next(c.verdict.value for c in rep.checks if c.rule_id == "R_VIN_CROSS")

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(one, range(32)))
    assert results.count("consistent") == 16
    assert results.count("inconsistent") == 16


def test_fixtures_ge_40():
    n = len(list(APPS.glob("*.json")))
    assert n >= 40, f"fixtures={n}"


def test_metrics_html_and_thresholds():
    m = evaluate_directory(APPS, RULES, ROOT / "fixtures" / "labels" / "expected_verdicts.json")
    html = metrics_to_html(m)
    assert "Batch Evaluate" in html or "coverage" in html
    assert m.coverage >= 0.80
    assert m.false_positive_rate <= 0.05
    assert m.false_negative_rate <= 0.03
    assert m.miss_rate <= 0.10
