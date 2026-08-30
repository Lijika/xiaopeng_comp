"""Round13: labeled inconsistent expansion + layout meta retained."""

from __future__ import annotations

import json
from pathlib import Path

from task4_consistency.models import Application
from task4_consistency.rules.engine import RuleEngine
from task4_consistency.rules.loader import load_rules

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "fixtures" / "applications"
RULES = ROOT / "configs" / "rules_auto_lease.yaml"


def test_r13_fixtures_exist_and_labeled_inconsistent():
    files = sorted(FIX.glob("app_r13_layout_*.json"))
    assert len(files) >= 12
    eng = RuleEngine(load_rules(RULES))
    n_inc_labels = 0
    for fp in files:
        data = json.loads(fp.read_text(encoding="utf-8"))
        meta = data.get("meta") or {}
        assert meta.get("layout_sample_id"), f"{fp.name} missing layout_sample_id"
        assert meta.get("source") == "semi_real_layout"
        exp = data.get("expected_verdicts") or {}
        n_inc = sum(1 for v in exp.values() if v == "inconsistent")
        assert n_inc >= 1, f"{fp.name} needs ≥1 labeled inconsistent"
        n_inc_labels += n_inc
        report = eng.run(Application.from_dict(data))
        for c in report.checks:
            if c.rule_id in exp:
                assert c.verdict.value == exp[c.rule_id], (
                    f"{fp.name} {c.rule_id}: pred={c.verdict.value} exp={exp[c.rule_id]}"
                )
    assert n_inc_labels >= 12


def test_r13_raises_inconsistent_denominator():
    """FNR 分母 = labeled inconsistent pairs should grow past Round12 (~31)."""
    n = 0
    for fp in FIX.glob("*.json"):
        data = json.loads(fp.read_text(encoding="utf-8"))
        for v in (data.get("expected_verdicts") or {}).values():
            if v == "inconsistent":
                n += 1
    assert n >= 40, f"expected ≥40 inconsistent labels, got {n}"
