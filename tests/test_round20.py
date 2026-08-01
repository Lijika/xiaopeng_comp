"""Round20: field_source=synthetic stamped; main warning gone."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from task4_consistency.evaluate import evaluate_suite
from task4_consistency.web.app import app

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "fixtures" / "applications"
RULES = ROOT / "configs" / "rules_auto_lease.yaml"


def test_all_main_fixtures_have_field_source_synthetic():
    missing = []
    for fp in FIX.glob("*.json"):
        data = json.loads(fp.read_text(encoding="utf-8"))
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        fs = meta.get("field_source")
        if fs != "synthetic":
            missing.append((fp.name, fs))
    assert not missing, f"fixtures missing field_source=synthetic: {missing[:5]}"


def test_main_evaluate_no_field_source_warning():
    m = evaluate_suite("main", RULES)
    assert m.mode == "labeled"
    assert all(m.pass_thresholds.values())
    assert not any("missing_field_source" in w for w in m.warnings)


def test_web_evaluate_suite_query():
    client = TestClient(app)
    r = client.get("/api/evaluate/summary?suite=main")
    assert r.status_code == 200
    body = r.json()
    assert body["metrics"]["suite"] == "main"
    assert body["metrics"]["mode"] == "labeled"
    assert body["metrics"]["coverage"] >= 0.80

    r2 = client.get("/api/evaluate/summary?suite=semi")
    assert r2.status_code == 200
    assert r2.json()["metrics"]["suite"] == "semi"
