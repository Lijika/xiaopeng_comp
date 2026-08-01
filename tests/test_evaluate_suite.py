"""Round19: evaluate --suite main|semi|all."""

from __future__ import annotations

from pathlib import Path

from task4_consistency.evaluate import evaluate_suite
from task4_consistency.rules.loader import load_rules

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "configs" / "rules_auto_lease.yaml"


def test_suite_main_threshold_pass():
    m = evaluate_suite("main", RULES)
    assert m.suite == "main"
    assert m.mode == "labeled"
    assert m.coverage >= 0.80
    assert m.false_positive_rate <= 0.05
    assert m.false_negative_rate <= 0.03
    assert m.miss_rate <= 0.10
    assert all(m.pass_thresholds.values())
    assert "Official delivery" in m.honesty_note or "main" in m.honesty_note.lower()


def test_suite_semi_smoke_empty_or_ok():
    m = evaluate_suite("semi", RULES)
    assert m.suite == "semi"
    # empty or imported demos without labels → smoke
    if m.mode == "smoke":
        assert "smoke" in m.honesty_note.lower() or "FP/FN" in m.honesty_note
        assert m.pass_thresholds.get("smoke_load_ok") is True
        # must NOT claim FPR gate as delivery
        assert not any("false_positive_rate" in k for k in m.pass_thresholds)
    else:
        # labeled semi kept separate
        assert m.mode == "labeled"
        assert "semi" in m.honesty_note.lower() or "NOT be merged" in m.honesty_note


def test_suite_all_debug_warning():
    m = evaluate_suite("all", RULES)
    assert m.suite == "all"
    assert any("debug" in w.lower() or "all" in w for w in m.warnings) or "all" in m.honesty_note
