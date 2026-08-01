from pathlib import Path

from task4_consistency.evaluate import evaluate_directory

ROOT = Path(__file__).resolve().parents[1]


def test_evaluate_thresholds():
    metrics = evaluate_directory(
        ROOT / "fixtures" / "applications",
        ROOT / "configs" / "rules_auto_lease.yaml",
        ROOT / "fixtures" / "labels" / "expected_verdicts.json",
    )
    assert metrics.coverage >= 0.80
    assert metrics.false_positive_rate <= 0.05
    assert metrics.false_negative_rate <= 0.03
    assert metrics.false_positive == 0
    assert metrics.false_negative == 0
