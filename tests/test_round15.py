"""Round15: close ADV-W1/W2 P0 + W4/W7/W9/K3/K9/K10 P1 guards."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from task4_consistency.kb.store import EntityKB, reload_kb
from task4_consistency.normalize.address import normalize_address
from task4_consistency.rules.loader import load_rules
from task4_consistency.web import app as webapp

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "configs" / "rules_auto_lease.yaml"


def _base_package() -> dict:
    return yaml.safe_load(RULES.read_text(encoding="utf-8"))


def test_adv_w1_poison_rules_not_persisted(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime_rules.yaml"
    monkeypatch.setattr(webapp, "RUNTIME_RULES", runtime)
    monkeypatch.delenv("TASK4_WEB_TOKEN", raising=False)
    client = TestClient(webapp.app)
    bad = {"version": 1, "field_aliases": {}, "rules": [{"type": "exact", "field": "vin"}]}
    r = client.post("/api/rules/validate", json={"content": bad})
    assert r.status_code == 400
    assert not runtime.exists()
    h = client.get("/api/health")
    assert h.status_code == 200
    assert h.json()["ok"] is True


def test_adv_w1_self_heal_quarantines_poison(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime_rules.yaml"
    runtime.write_text("rules: [{type: exact, field: vin}]\n", encoding="utf-8")  # no id
    monkeypatch.setattr(webapp, "RUNTIME_RULES", runtime)
    monkeypatch.delenv("TASK4_WEB_TOKEN", raising=False)
    client = TestClient(webapp.app)
    h = client.get("/api/health")
    assert h.status_code == 200
    assert h.json()["ok"] is True
    # poisoned runtime quarantined
    assert not runtime.exists() or runtime.with_suffix(".yaml.bad").exists()


def test_adv_w2_cannot_drop_critical_vin_rule(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime_rules.yaml"
    monkeypatch.setattr(webapp, "RUNTIME_RULES", runtime)
    monkeypatch.delenv("TASK4_WEB_TOKEN", raising=False)
    client = TestClient(webapp.app)
    data = _base_package()
    data["rules"] = [r for r in data["rules"] if r.get("id") != "R_VIN_CROSS"]
    r = client.post("/api/rules/validate", json={"content": data})
    assert r.status_code == 400
    detail = r.json()["detail"]
    msg = detail.get("message", "") if isinstance(detail, dict) else str(detail)
    assert "vin" in msg.lower() or "critical" in msg.lower() or "ADV-W2" in msg
    assert not runtime.exists()


def test_adv_w4_rel_tol_capped():
    data = _base_package()
    for r in data["rules"]:
        if r.get("id") == "R_AMOUNT_TOL":
            r["rel_tol"] = 0.5
    with pytest.raises(ValueError, match="ADV-W4|rel_tol"):
        # write temp and load
        import tempfile
        from pathlib import Path as P

        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            yaml.safe_dump(data, f, allow_unicode=True)
            p = P(f.name)
        try:
            load_rules(p)
        finally:
            p.unlink(missing_ok=True)


def test_adv_w7_reg_date_cannot_absorb_contract():
    data = _base_package()
    aliases = data.setdefault("field_aliases", {})
    reg = list(aliases.get("reg_date") or [])
    reg.append("contract_date")
    aliases["reg_date"] = reg
    import tempfile
    from pathlib import Path as P

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(data, f, allow_unicode=True)
        p = P(f.name)
    try:
        with pytest.raises(ValueError, match="ADV-W7|contract"):
            load_rules(p)
    finally:
        p.unlink(missing_ok=True)


def test_adv_w9_cannot_demote_vin_to_info_skip():
    data = _base_package()
    for r in data["rules"]:
        if r.get("id") == "R_VIN_CROSS":
            r["severity"] = "info"
            r["on_missing"] = "skip"
    import tempfile
    from pathlib import Path as P

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(data, f, allow_unicode=True)
        p = P(f.name)
    try:
        with pytest.raises(ValueError, match="ADV-W9|info|skip|critical"):
            load_rules(p)
    finally:
        p.unlink(missing_ok=True)


def test_adv_k9_address_does_not_apply_org_aliases(tmp_path):
    kb_path = tmp_path / "kb.json"
    kb_path.write_text(
        '{"version":1,"address_aliases":{},"org_aliases":{"人保":"平安"},"plate_prefixes":{}}',
        encoding="utf-8",
    )
    reload_kb(kb_path)
    a = normalize_address("南京人保大厦1号")
    b = normalize_address("南京平安大厦1号")
    assert a != b
    reload_kb(ROOT / "configs" / "kb" / "entity_kb.json")


def test_adv_k10_short_key_rejected(tmp_path):
    kb = EntityKB(tmp_path / "e.json")
    with pytest.raises(ValueError, match="short|ADV-K10"):
        kb.add_alias("address_aliases", "州", "X")


def test_adv_k3_cross_city_alias_rejected(tmp_path):
    kb = EntityKB(tmp_path / "e.json")
    with pytest.raises(ValueError, match="cities|ADV-K3"):
        kb.add_alias("address_aliases", "江苏苏州", "江苏南京")
