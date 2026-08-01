"""Round16 ARCH final: critical fingerprints + atomic rules PUT (W1/W2)."""

from __future__ import annotations

import copy
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from task4_consistency.rules.critical_guard import (
    CRITICAL_FINGERPRINTS,
    CriticalGuardError,
    enforce_critical_fingerprints,
)
from task4_consistency.rules.loader import load_rules
from task4_consistency.web import app as webapp

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "configs" / "rules_auto_lease.yaml"


def _pkg() -> dict:
    return yaml.safe_load(RULES.read_text(encoding="utf-8"))


def test_default_package_satisfies_fingerprints():
    cfg = load_rules(RULES)
    enforce_critical_fingerprints(cfg)
    assert {fp.rule_id for fp in CRITICAL_FINGERPRINTS} <= {r.id for r in cfg.rules}


def test_w1_bad_yaml_zero_touch_runtime(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime_rules.yaml"
    # seed good runtime first
    runtime.write_text(RULES.read_text(encoding="utf-8"), encoding="utf-8")
    before = runtime.read_text(encoding="utf-8")
    monkeypatch.setattr(webapp, "RUNTIME_RULES", runtime)
    monkeypatch.delenv("TASK4_WEB_TOKEN", raising=False)
    client = TestClient(webapp.app)

    r = client.put("/api/rules", json={"yaml_text": "rules: [\n  - type: exact\n"})
    assert r.status_code == 400
    # active unchanged
    assert runtime.read_text(encoding="utf-8") == before

    r = client.put(
        "/api/rules",
        json={"content": {"version": 1, "field_aliases": {}, "rules": [{"type": "exact", "field": "vin"}]}},
    )
    assert r.status_code == 400
    assert runtime.read_text(encoding="utf-8") == before


def test_w2_delete_vin_rejected(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime_rules.yaml"
    monkeypatch.setattr(webapp, "RUNTIME_RULES", runtime)
    monkeypatch.delenv("TASK4_WEB_TOKEN", raising=False)
    client = TestClient(webapp.app)
    data = _pkg()
    data["rules"] = [r for r in data["rules"] if r.get("id") != "R_VIN_CROSS"]
    r = client.put("/api/rules", json={"content": data})
    assert r.status_code == 400
    err = r.json()["detail"]["error"]
    assert err in {"critical_rule_missing", "rules_schema_invalid", "rules_policy_invalid"}
    assert not runtime.exists()


def test_w2_vin_type_fuzzy_rejected(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime_rules.yaml"
    monkeypatch.setattr(webapp, "RUNTIME_RULES", runtime)
    monkeypatch.delenv("TASK4_WEB_TOKEN", raising=False)
    client = TestClient(webapp.app)
    data = _pkg()
    for r in data["rules"]:
        if r.get("id") == "R_VIN_CROSS":
            r["type"] = "fuzzy"
            r["threshold"] = 0.5
    r = client.put("/api/rules", json={"content": data})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "critical_semantic_tamper"
    assert not runtime.exists()


def test_w2_docs_stripped_rejected(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime_rules.yaml"
    monkeypatch.setattr(webapp, "RUNTIME_RULES", runtime)
    monkeypatch.delenv("TASK4_WEB_TOKEN", raising=False)
    client = TestClient(webapp.app)
    data = _pkg()
    for r in data["rules"]:
        if r.get("id") == "R_VIN_CROSS":
            r["docs"] = []  # strip all
    r = client.put("/api/rules", json={"content": data})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "critical_docs_stripped"


def test_w2_docs_drop_reg_cert_rejected(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime_rules.yaml"
    monkeypatch.setattr(webapp, "RUNTIME_RULES", runtime)
    monkeypatch.delenv("TASK4_WEB_TOKEN", raising=False)
    client = TestClient(webapp.app)
    data = _pkg()
    for r in data["rules"]:
        if r.get("id") == "R_VIN_CROSS":
            r["docs"] = [d for d in r["docs"] if d != "机动车登记证书"]
    r = client.put("/api/rules", json={"content": data})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "critical_docs_stripped"


def test_w2_on_missing_skip_rejected(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime_rules.yaml"
    monkeypatch.setattr(webapp, "RUNTIME_RULES", runtime)
    monkeypatch.delenv("TASK4_WEB_TOKEN", raising=False)
    client = TestClient(webapp.app)
    data = _pkg()
    for r in data["rules"]:
        if r.get("id") == "R_VIN_CROSS":
            r["on_missing"] = "skip"
    r = client.put("/api/rules", json={"content": data})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "critical_on_missing_skip"


def test_w2_legal_docs_superset_and_version_ok(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime_rules.yaml"
    monkeypatch.setattr(webapp, "RUNTIME_RULES", runtime)
    monkeypatch.delenv("TASK4_WEB_TOKEN", raising=False)
    client = TestClient(webapp.app)
    data = _pkg()
    data["version"] = "1.9.0-r16"
    for r in data["rules"]:
        if r.get("id") == "R_VIN_CROSS":
            docs = list(r["docs"])
            if "抵押合同" not in docs:
                docs.append("抵押合同")
            r["docs"] = docs
        if r.get("id") == "R_AMOUNT_TOL":
            r["rel_tol"] = 0.0002  # non-critical tweak within cap
    r = client.put("/api/rules", json={"content": data})
    assert r.status_code == 200, r.text
    assert runtime.exists()
    assert r.json()["version"] == "1.9.0-r16"
    # active loads clean
    cfg = load_rules(runtime)
    enforce_critical_fingerprints(cfg)


def test_reset_uses_default_fingerprints(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime_rules.yaml"
    monkeypatch.setattr(webapp, "RUNTIME_RULES", runtime)
    monkeypatch.delenv("TASK4_WEB_TOKEN", raising=False)
    client = TestClient(webapp.app)
    # save good first
    r = client.put("/api/rules", json={"yaml_text": RULES.read_text(encoding="utf-8")})
    assert r.status_code == 200
    assert runtime.exists()
    r = client.post("/api/rules/reset")
    assert r.status_code == 200
    assert not runtime.exists()
    h = client.get("/api/health")
    assert h.status_code == 200
    # default package still fingerprint-ok
    load_rules(RULES)


def test_fingerprint_unit_missing_raises():
    cfg = load_rules(RULES)
    cfg.rules = [r for r in cfg.rules if r.id != "R_ENGINE_CROSS"]
    try:
        enforce_critical_fingerprints(cfg)
        assert False, "expected CriticalGuardError"
    except CriticalGuardError as e:
        assert e.error == "critical_rule_missing"
