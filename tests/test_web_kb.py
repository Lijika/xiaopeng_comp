"""Round10: KB + web API smoke (no live server required for KB)."""

from pathlib import Path

from fastapi.testclient import TestClient

from task4_consistency.kb.store import EntityKB, get_kb, reload_kb
from task4_consistency.normalize.address import normalize_address
from task4_consistency.web.app import app

ROOT = Path(__file__).resolve().parents[1]


def test_kb_address_alias_affects_normalize(tmp_path):
    kb_path = tmp_path / "kb.json"
    kb_path.write_text(
        '{"version":1,"address_aliases":{"测试开发区":"测试区"},"org_aliases":{},"plate_prefixes":{}}',
        encoding="utf-8",
    )
    kb = reload_kb(kb_path)
    assert kb.list_section("address_aliases")["测试开发区"] == "测试区"
    # normalize uses global KB
    out = normalize_address("江苏省测试开发区中山路1号")
    assert out is not None
    assert "测试区" in out
    assert "测试开发区" not in out
    # restore default KB for other tests
    reload_kb(ROOT / "configs" / "kb" / "entity_kb.json")


def test_kb_crud(tmp_path):
    kb = EntityKB(tmp_path / "e.json")
    kb.add_alias("org_aliases", "某某融资租赁有限公司", "某某金租")
    assert kb.list_section("org_aliases")["某某融资租赁有限公司"] == "某某金租"
    assert kb.remove_alias("org_aliases", "某某融资租赁有限公司")
    assert "某某融资租赁有限公司" not in kb.list_section("org_aliases")


def test_web_api_smoke():
    client = TestClient(app)
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True

    r = client.get("/api/fixtures")
    assert r.status_code == 200
    fixtures = r.json()["fixtures"]
    assert fixtures
    name = fixtures[0]["file"]

    r = client.get(f"/api/fixtures/{name}")
    assert r.status_code == 200
    fixture_id = Path(name).stem

    r = client.post("/api/check", json={"fixture_id": fixture_id})
    assert r.status_code == 200
    body = r.json()
    assert "report" in body
    assert "checks" in body["report"]
    assert body["report"]["summary"]["total"] >= 0

    r = client.get("/api/rules")
    assert r.status_code == 200
    assert "yaml_text" in r.json()

    r = client.get("/api/kb")
    assert r.status_code == 200
    assert "address_aliases" in r.json()

    r = client.get("/")
    assert r.status_code == 200
    assert "/static/react/assets/" in r.text


def test_web_batch_and_evaluate_summary():
    client = TestClient(app)
    files = [x["file"] for x in client.get("/api/fixtures").json()["fixtures"][:3]]
    r = client.post("/api/check/batch", json={"fixture_files": files})
    assert r.status_code == 200
    body = r.json()
    assert body["n"] == 3
    assert "totals" in body
    assert len(body["results"]) == 3

    r = client.get("/api/evaluate/summary")
    assert r.status_code == 200
    m = r.json()["metrics"]
    assert m["coverage"] >= 0.80
    assert "html" in r.json()


def test_legacy_batch_and_evaluate_summary_wire_shapes_unchanged():
    """T07: the legacy /api/check/batch and /api/evaluate/summary routes stay
    wire-compatible (rollback path); the demo projections are additive."""
    client = TestClient(app)
    files = [x["file"] for x in client.get("/api/fixtures").json()["fixtures"][:2]]
    r = client.post("/api/check/batch", json={"fixture_files": files})
    assert r.status_code == 200, r.text
    assert set(r.json().keys()) == {"n", "totals", "results", "rules_path"}

    r = client.get("/api/evaluate/summary")
    assert r.status_code == 200, r.text
    assert set(r.json().keys()) == {"metrics", "html", "rules_path", "suite"}
    assert "pass_thresholds" in r.json()["metrics"]


def test_web_batch_check_max_n():
    """Round26: check/batch soft cap; no evaluate/batch API."""
    from task4_consistency.web import app as webapp

    client = TestClient(app)
    n = webapp.BATCH_CHECK_MAX_N + 1
    files = [x["file"] for x in client.get("/api/fixtures").json()["fixtures"]]
    # pad by repeating names if needed
    while len(files) < n:
        files = files + files
    r = client.post("/api/check/batch", json={"fixture_files": files[:n]})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["error"] == "batch_too_large"
    assert detail["max_n"] == webapp.BATCH_CHECK_MAX_N


def test_legacy_batch_cap_enforced_before_fixture_io():
    """T07 plan invariant: the legacy route rejects an over-cap batch before
    any fixture read — an over-cap batch of nonexistent names is the cap
    rejection, never a 404."""
    from task4_consistency.web import app as webapp

    client = TestClient(app)
    n = webapp.BATCH_CHECK_MAX_N + 1
    r = client.post(
        "/api/check/batch",
        json={"fixture_files": ["no_such_fixture_1.json"] * n},
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["error"] == "batch_too_large"
    assert detail["max_n"] == webapp.BATCH_CHECK_MAX_N


def test_org_alias_brand_normalize():
    from task4_consistency.normalize.base import normalize_brand
    from task4_consistency.kb.store import reload_kb

    reload_kb(ROOT / "configs" / "kb" / "entity_kb.json")
    assert normalize_brand("特斯拉（上海）有限公司") == normalize_brand("特斯拉")
    assert normalize_brand("比亚迪汽车工业有限公司") == "比亚迪"
    assert normalize_brand("理想汽车有限公司") == normalize_brand("理想")


def test_web_rules_validate_and_reject_bad_yaml(tmp_path, monkeypatch):
    """Round13: dry-run validate + invalid payloads never touch runtime."""
    import task4_consistency.web.app as webapp

    runtime = tmp_path / "runtime_rules.yaml"
    monkeypatch.setattr(webapp, "RUNTIME_RULES", runtime)
    client = TestClient(webapp.app)

    # valid dry-run
    good = (ROOT / "configs" / "rules_auto_lease.yaml").read_text(encoding="utf-8")
    r = client.post("/api/rules/validate", json={"yaml_text": good})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert r.json()["n_rules"] >= 1
    assert not runtime.exists()  # validate must not write

    # invalid YAML syntax
    r = client.post("/api/rules/validate", json={"yaml_text": "rules: [\n  - id: x\n type broken"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert isinstance(detail, dict)
    assert detail.get("error") in {"invalid_yaml", "rules_schema_invalid", "rules_save_failed"}
    assert not runtime.exists()

    # invalid schema (unknown type)
    bad = """
package: t
version: "0"
rules:
  - id: R_BAD
    name: bad
    type: not_a_real_type
    field: vin
    docs: [机动车登记证书]
"""
    r = client.post("/api/rules/validate", json={"yaml_text": bad})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "message" in detail
    assert not runtime.exists()

    # valid again after rejects: still dry-run, runtime never appears
    r = client.post("/api/rules/validate", json={"yaml_text": good})
    assert r.status_code == 200, r.text
    assert not runtime.exists()


def test_web_check_unknown_fixture_is_hidden():
    client = TestClient(app)
    r = client.post("/api/check", json={"fixture_id": "unknown-fixture"})
    assert r.status_code == 404
    assert r.json() == {"detail": {"error": "SYNTHETIC_ONLY"}}


def test_attack_web_kb_gate_exit_1_on_any_open_result(monkeypatch):
    """Issue #45 Round-1: any open RET-*/ADV-* result must fail the gate.

    Red at 8a869767 (a RET open is outside the W1/W2 gate set, so the
    command exits 0).  Preserves the per-probe output and total count.
    """
    import sys

    import scripts.attack_web_kb as attack_web_kb

    monkeypatch.setattr(sys, "argv", ["attack_web_kb.py"])
    monkeypatch.setattr(
        attack_web_kb,
        "_has_delivery",
        lambda: {"kb_pkg": False, "web_pkg": True, "run_web": False},
    )
    monkeypatch.setattr(
        attack_web_kb,
        "attack_w1_w2_local",
        lambda: [("RET-1", "retired route", "HTTP 200 (expect 405)", False)],
    )
    assert attack_web_kb.main() == 1


def test_attack_web_kb_gate_exit_0_when_all_closed(monkeypatch):
    """A nonempty all-closed result still exits 0 (regression guard)."""
    import sys

    import scripts.attack_web_kb as attack_web_kb

    monkeypatch.setattr(sys, "argv", ["attack_web_kb.py"])
    monkeypatch.setattr(
        attack_web_kb,
        "_has_delivery",
        lambda: {"kb_pkg": False, "web_pkg": True, "run_web": False},
    )
    monkeypatch.setattr(
        attack_web_kb,
        "attack_w1_w2_local",
        lambda: [
            ("ADV-W1", "bad rules reject", "HTTP 400", True),
            ("RET-1", "retired route", "HTTP 405 (expect 405)", True),
        ],
    )
    assert attack_web_kb.main() == 0


def test_rules_validate_empty_body_uses_retained_hint():
    """Issue #45 Round-1: empty-body recovery names the retained endpoint.

    Red at 8a869767 (the hint advertises the retired PUT /api/rules writer).
    """
    client = TestClient(app)
    r = client.post("/api/rules/validate", json={})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["error"] == "missing_body"
    assert detail["hint"] == 'POST /api/rules/validate body: {"yaml_text": "..."}'
