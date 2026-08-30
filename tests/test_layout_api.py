"""Web 业务向：表单 content 校验路径 + layout API。

- UI `saveRulesForm` 表单内容 → POST /api/rules/validate 干跑（PUT 保存已退役，不写 runtime）
- 登记证版面样例列表/详情（无抽取文本）
"""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from task4_consistency.rules.loader import load_rules
from task4_consistency.web import app as webapp

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "configs" / "rules_auto_lease.yaml"
STEP2 = ROOT / "data" / "registration_layout"


def test_validate_rules_content_form_path(tmp_path, monkeypatch):
    """表单保存：GET content → 微调 → validate content 干跑（不再写 runtime_rules.yaml）。"""
    runtime = tmp_path / "runtime_rules.yaml"
    monkeypatch.setattr(webapp, "RUNTIME_RULES", runtime)
    monkeypatch.delenv("TASK4_WEB_TOKEN", raising=False)
    client = TestClient(webapp.app)

    g = client.get("/api/rules")
    assert g.status_code == 200
    body = g.json()
    assert "content" in body and isinstance(body["content"], dict)
    assert "yaml_text" in body
    content = body["content"]
    assert content.get("rules"), "rules list required for form save"

    # form-like edit: bump version string only (non-critical)
    content = yaml.safe_load(RULES.read_text(encoding="utf-8"))
    content["version"] = str(content.get("version", "1.9.0")) + "-form"
    content["changelog"] = list(content.get("changelog") or []) + [
        "form content path validate test"
    ]

    r = client.post("/api/rules/validate", json={"content": content})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert data["version"] == content["version"]
    assert data["n_rules"] >= 3
    # dry-run: validate never writes runtime_rules.yaml
    assert not runtime.exists()


def test_validate_rules_content_reject_non_object(tmp_path, monkeypatch):
    """非 object content：Pydantic 422 或业务 400 content_not_object；均不得写 runtime。"""
    runtime = tmp_path / "runtime_rules.yaml"
    monkeypatch.setattr(webapp, "RUNTIME_RULES", runtime)
    monkeypatch.delenv("TASK4_WEB_TOKEN", raising=False)
    client = TestClient(webapp.app)
    r = client.post("/api/rules/validate", json={"content": ["not", "object"]})
    # FastAPI/Pydantic rejects list before handler (422); handler path is 400 content_not_object
    assert r.status_code in (400, 422), r.text
    if r.status_code == 400:
        detail = r.json()["detail"]
        assert isinstance(detail, dict)
        assert detail.get("error") == "content_not_object"
    assert not runtime.exists()


def test_layout_api_list_and_detail():
    """GET /api/layout/samples + /api/layout/{sample_id}."""
    monkeypatch_token = None  # noqa: silence — use env clear via client only
    client = TestClient(webapp.app)
    # ensure no auth required in test env
    import os

    os.environ.pop("TASK4_WEB_TOKEN", None)

    r = client.get("/api/layout/samples")
    assert r.status_code == 200, r.text
    payload = r.json()
    assert "samples" in payload
    assert isinstance(payload["samples"], list)
    assert payload["samples"], "data/registration_layout should have page_order samples"
    assert "note" in payload
    sample = payload["samples"][0]
    assert sample.get("sample_id")
    assert sample.get("file", "").endswith("_page_order.json")
    assert "n_pages" in sample
    assert "linked_fixtures" in sample

    sid = sample["sample_id"]
    d = client.get(f"/api/layout/{sid}")
    assert d.status_code == 200, d.text
    detail = d.json()
    assert detail.get("sample_id") == sid or detail.get("sample_id")
    assert isinstance(detail.get("pages"), list)
    assert detail["pages"], "pages compact list"
    page0 = detail["pages"][0]
    assert "page_type" in page0 or "filename" in page0
    assert "detected_fields" in page0


def test_layout_api_invalid_and_missing():
    client = TestClient(webapp.app)
    import os

    os.environ.pop("TASK4_WEB_TOKEN", None)

    r = client.get("/api/layout/../etc/passwd")
    assert r.status_code in (400, 404)

    r = client.get("/api/layout/not-a-real-sample-id-xyz")
    assert r.status_code == 404


# --- 业务向演示：app_demo_layout_* 绑定真实 sample_id ---

FIXTURES = ROOT / "fixtures" / "applications"
DEMO_FILES = [
    "app_demo_layout_ok.json",
    "app_demo_layout_bad_vin.json",
    "app_demo_layout_fmt.json",
]


def test_demo_layout_fixtures_bound_and_labeled():
    """app_demo_layout_*：field_source=synthetic + layout_sample_id + expected_verdicts。"""
    import json

    from task4_consistency.models import Application
    from task4_consistency.rules.engine import RuleEngine

    eng = RuleEngine(load_rules(RULES))
    found = []
    for name in DEMO_FILES:
        fp = FIXTURES / name
        assert fp.is_file(), f"missing demo fixture {name}"
        data = json.loads(fp.read_text(encoding="utf-8"))
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        assert meta.get("field_source") == "synthetic", name
        sid = meta.get("layout_sample_id")
        assert sid, f"{name} missing layout_sample_id"
        assert (STEP2 / f"{sid}_page_order.json").is_file(), f"layout file for {sid}"
        exp = data.get("expected_verdicts") or {}
        assert exp, f"{name} missing expected_verdicts"

        rep = eng.run(Application.from_dict(data))
        got = {
            c.rule_id: (c.verdict.value if hasattr(c.verdict, "value") else str(c.verdict))
            for c in rep.checks
        }
        bad = {k: (exp[k], got.get(k)) for k in exp if got.get(k) != exp[k]}
        assert not bad, f"{name} verdict mismatch {bad}"
        found.append((name, sid, data.get("label")))

    assert len(found) == 3
    # bad_vin must surface VIN inconsistent
    bad_data = json.loads((FIXTURES / "app_demo_layout_bad_vin.json").read_text(encoding="utf-8"))
    assert bad_data["expected_verdicts"].get("R_VIN_CROSS") == "inconsistent"


def test_layout_api_links_demo_fixtures():
    """GET /api/layout/samples 应把 app_demo_layout_* 挂到对应 sample 的 linked_fixtures。"""
    import os

    os.environ.pop("TASK4_WEB_TOKEN", None)
    client = TestClient(webapp.app)
    r = client.get("/api/layout/samples")
    assert r.status_code == 200
    samples = {s["sample_id"]: s for s in r.json()["samples"] if s.get("sample_id")}

    import json

    for name in DEMO_FILES:
        data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        sid = data["meta"]["layout_sample_id"]
        assert sid in samples, f"{sid} not listed in /api/layout/samples"
        linked = samples[sid].get("linked_fixtures") or []
        assert name in linked, f"{name} not in linked_fixtures for {sid}: {linked}"
        # detail endpoint still works for demo-bound samples
        d = client.get(f"/api/layout/{sid}")
        assert d.status_code == 200
        assert d.json().get("pages")


def test_web_check_demo_layout_ok_via_fixture_api():
    """Web 拉 fixture 再 check：业务演示主路径。"""
    import os

    os.environ.pop("TASK4_WEB_TOKEN", None)
    client = TestClient(webapp.app)
    name = "app_demo_layout_ok.json"
    fx = client.get(f"/api/fixtures/{name}")
    assert fx.status_code == 200
    r = client.post("/api/check", json={"fixture_id": Path(name).stem})
    assert r.status_code == 200, r.text
    body = r.json()
    report = body.get("report") or body
    summary = report.get("summary") or {}
    assert summary.get("inconsistent", 0) == 0
    assert summary.get("consistent", 0) >= 1
