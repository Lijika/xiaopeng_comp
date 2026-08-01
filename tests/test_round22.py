"""Round22: health API fields + audit JSONL path."""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi.testclient import TestClient

from task4_consistency.audit import audit_log_path, read_audit_tail, write_audit
from task4_consistency.web import app as webapp
from task4_consistency.web.app import app

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "configs" / "rules_auto_lease.yaml"


def test_health_has_rules_kb_version():
    os.environ.pop("TASK4_WEB_TOKEN", None)
    client = TestClient(app)
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body.get("rules_path")
    assert body.get("kb_ok") is True
    assert body.get("version") is not None
    assert body.get("package")
    assert "audit" in body
    assert body["audit"]["path"]


def test_audit_jsonl_write_and_tail(tmp_path, monkeypatch):
    log = tmp_path / "audit.log"
    monkeypatch.setenv("TASK4_AUDIT_LOG", str(log))
    assert write_audit("rules_save", ok=True, detail={"n_rules": 1}) is True
    assert write_audit("kb_add", ok=True, detail={"section": "org_aliases"}) is True
    assert log.is_file()
    events = read_audit_tail(10)
    assert len(events) >= 2
    assert events[-1]["action"] in {"kb_add", "rules_save"}
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert all(json.loads(x).get("action") for x in lines)


def test_rules_save_writes_audit(tmp_path, monkeypatch):
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
    actions = [json.loads(x)["action"] for x in log.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert "rules_save" in actions


def test_audit_recent_api(tmp_path, monkeypatch):
    log = tmp_path / "r.log"
    monkeypatch.setenv("TASK4_AUDIT_LOG", str(log))
    write_audit("health_probe", detail={"round": 22})
    client = TestClient(app)
    r = client.get("/api/audit/recent?limit=5")
    assert r.status_code == 200
    body = r.json()
    assert body["n"] >= 1
    assert any(e.get("action") == "health_probe" for e in body["events"])
