"""Round18: fixture expansion + demo/run_web smoke."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from fastapi.testclient import TestClient

from task4_consistency.models import Application
from task4_consistency.rules.engine import RuleEngine
from task4_consistency.rules.loader import load_rules
from task4_consistency.web.app import app

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "fixtures" / "applications"
RULES = ROOT / "configs" / "rules_auto_lease.yaml"


def test_r18_fixtures_labeled_inconsistent():
    files = sorted(FIX.glob("app_r18_*.json"))
    assert len(files) >= 5
    eng = RuleEngine(load_rules(RULES))
    n_inc = 0
    for fp in files:
        data = json.loads(fp.read_text(encoding="utf-8"))
        assert (data.get("meta") or {}).get("round") == 18
        exp = data["expected_verdicts"]
        n = sum(1 for v in exp.values() if v == "inconsistent")
        assert n >= 1, fp.name
        n_inc += n
        report = eng.run(Application.from_dict(data))
        for c in report.checks:
            if c.rule_id in exp:
                assert c.verdict.value == exp[c.rule_id], (
                    f"{fp.name} {c.rule_id}: {c.verdict.value}!={exp[c.rule_id]}"
                )
    assert n_inc >= 5


def test_demo_and_run_web_scripts_exist():
    demo = ROOT / "scripts" / "demo.sh"
    run_web = ROOT / "scripts" / "run_web.sh"
    assert demo.is_file()
    assert run_web.is_file()
    # executable bit or at least readable bash scripts
    assert demo.stat().st_mode & stat.S_IRUSR
    assert run_web.stat().st_mode & stat.S_IRUSR
    text = demo.read_text(encoding="utf-8")
    assert "task4_consistency" in text or "evaluate" in text or "pytest" in text
    rw = run_web.read_text(encoding="utf-8")
    assert "uvicorn" in rw or "web" in rw


def test_web_demo_smoke_no_token():
    os.environ.pop("TASK4_WEB_TOKEN", None)
    client = TestClient(app)
    assert client.get("/api/health").status_code == 200
    assert client.get("/").status_code == 200
    fixtures = client.get("/api/fixtures").json()["fixtures"]
    assert len(fixtures) >= 80
