"""Round14: audit log, --strict-vin, optional web token, new fixtures."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from task4_consistency.audit import audit_log_path, write_audit
from task4_consistency.cli import main
from task4_consistency.models import Application
from task4_consistency.normalize.vin import normalize_vin_ex
from task4_consistency.rules.engine import RuleEngine
from task4_consistency.rules.loader import load_rules
from task4_consistency.web import app as webapp

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "fixtures" / "applications"
RULES = ROOT / "configs" / "rules_auto_lease.yaml"


def test_audit_write_jsonl(tmp_path, monkeypatch):
    log = tmp_path / "audit.log"
    monkeypatch.setenv("TASK4_AUDIT_LOG", str(log))
    write_audit("rules_save", ok=True, detail={"n_rules": 3})
    write_audit("kb_add", ok=True, detail={"section": "org_aliases", "key": "A", "value": "B"})
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    a = json.loads(lines[0])
    assert a["action"] == "rules_save"
    assert a["ok"] is True
    assert "ts" in a
    assert a["detail"]["n_rules"] == 3


def test_cli_strict_vin_forces_check_digit(tmp_path):
    """Synthetic VIN fails ISO check digit → strict-vin yields normalize fail/uncertain."""
    app = {
        "application_id": "STRICT-VIN",
        "documents": [
            {
                "doc_id": "r",
                "doc_type": "机动车登记证书",
                "fields": {
                    "vin": {"raw": "LSVAA4182N5000001", "confidence": 0.99},
                    "engine_no": {"raw": "E1", "confidence": 0.99},
                    "owner_name": {"raw": "甲", "confidence": 0.99},
                    "plate_no": {"raw": "苏A1", "confidence": 0.99},
                    "reg_cert_no": {"raw": "RC", "confidence": 0.99},
                    "reg_date": {"raw": "2024-01-01", "confidence": 0.99},
                    "address": {"raw": "南京", "confidence": 0.99},
                },
            },
            {
                "doc_id": "p",
                "doc_type": "交强险保单",
                "fields": {
                    "vin": {"raw": "LSVAA4182N5000001", "confidence": 0.99},
                    "engine_no": {"raw": "E1", "confidence": 0.99},
                    "insured_name": {"raw": "甲", "confidence": 0.99},
                    "plate_no": {"raw": "苏A1", "confidence": 0.99},
                    "plate_list": {"raw": "苏A1", "confidence": 0.99},
                },
            },
            {
                "doc_id": "l",
                "doc_type": "融资租赁合同",
                "fields": {
                    "vin": {"raw": "LSVAA4182N5000001", "confidence": 0.99},
                    "lessee_name": {"raw": "甲", "confidence": 0.99},
                    "id_number": {"raw": "320102199001012016", "confidence": 0.99},
                    "financed_amount": {"raw": "10000", "confidence": 0.99},
                    "reg_cert_no": {"raw": "RC", "confidence": 0.99},
                    "reg_date": {"raw": "2024-01-01", "confidence": 0.99},
                },
            },
            {
                "doc_id": "i",
                "doc_type": "发票",
                "fields": {
                    "vin": {"raw": "LSVAA4182N5000001", "confidence": 0.99},
                    "engine_no": {"raw": "E1", "confidence": 0.99},
                    "invoice_amount": {"raw": "10000", "confidence": 0.99},
                },
            },
            {
                "doc_id": "id",
                "doc_type": "身份证",
                "fields": {
                    "owner_name": {"raw": "甲", "confidence": 0.99},
                    "id_number": {"raw": "320102199001012016", "confidence": 0.99},
                    "address": {"raw": "南京", "confidence": 0.99},
                },
            },
        ],
    }
    # sanity: loose accepts, strict rejects
    assert normalize_vin_ex("LSVAA4182N5000001", strict_check_digit=False).value is not None
    assert normalize_vin_ex("LSVAA4182N5000001", strict_check_digit=True).value is None

    p = tmp_path / "app.json"
    p.write_text(json.dumps(app, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "rep.json"
    rc = main(
        [
            "check",
            str(p),
            "-c",
            str(RULES),
            "-o",
            str(out),
            "--strict-vin",
        ]
    )
    assert rc == 0
    rep = json.loads(out.read_text(encoding="utf-8"))
    vin_check = next(c for c in rep["checks"] if c["rule_id"] == "R_VIN_CROSS")
    # all sides fail normalize → uncertain (not silent consistent)
    assert vin_check["verdict"] == "uncertain"


def test_r14_fixtures_critical_boundaries():
    files = sorted(FIX.glob("app_r14_*.json"))
    assert len(files) >= 5
    eng = RuleEngine(load_rules(RULES))
    for fp in files:
        data = json.loads(fp.read_text(encoding="utf-8"))
        assert (data.get("meta") or {}).get("round") == 14
        exp = data["expected_verdicts"]
        assert any(v == "inconsistent" for v in exp.values()), fp.name
        report = eng.run(Application.from_dict(data))
        for c in report.checks:
            if c.rule_id in exp:
                assert c.verdict.value == exp[c.rule_id], (
                    f"{fp.name} {c.rule_id}: {c.verdict.value}!={exp[c.rule_id]}"
                )


def test_web_audit_on_rules_save(tmp_path, monkeypatch):
    log = tmp_path / "a.log"
    runtime = tmp_path / "runtime_rules.yaml"
    monkeypatch.setenv("TASK4_AUDIT_LOG", str(log))
    monkeypatch.delenv("TASK4_WEB_TOKEN", raising=False)
    monkeypatch.setattr(webapp, "RUNTIME_RULES", runtime)
    client = TestClient(webapp.app)
    good = RULES.read_text(encoding="utf-8")
    r = client.put("/api/rules", json={"yaml_text": good})
    assert r.status_code == 200
    assert log.exists()
    events = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert any(e["action"] == "rules_save" and e["ok"] for e in events)


def test_web_token_auth_optional(monkeypatch):
    monkeypatch.delenv("TASK4_WEB_TOKEN", raising=False)
    client = TestClient(webapp.app)
    assert client.get("/api/fixtures").status_code == 200

    monkeypatch.setenv("TASK4_WEB_TOKEN", "secret-r14")
    # health always open
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/health").json()["auth_required"] is True
    # API protected
    r = client.get("/api/fixtures")
    assert r.status_code == 401
    r = client.get("/api/fixtures", headers={"X-Task4-Token": "wrong"})
    assert r.status_code == 401
    r = client.get("/api/fixtures", headers={"Authorization": "Bearer secret-r14"})
    assert r.status_code == 200
    # cleanup env for other tests
    monkeypatch.delenv("TASK4_WEB_TOKEN", raising=False)
