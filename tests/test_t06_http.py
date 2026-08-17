"""T06 / Issue #40: /demo/react + /api/demo/* closed synthetic demo facade.

Red-green boundary: FastAPI alone owns the fixture allow-list, file
resolution, active rules, check execution, report projection, evidence-link
targets, and the C-DEMO claim label.  The browser sends only ``fixture_id``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import HTTPException
from fastapi.testclient import TestClient

from task4_consistency.web import app as webapp

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures" / "applications"
STEP2 = ROOT / "data" / "step2"

EXPECTED_FIXTURE_IDS = [
    "app_demo_step2_ok",
    "app_demo_step2_bad_vin",
    "app_demo_step2_fmt",
]
EXPECTED_NEUTRAL_TITLES = ["演示样例 1", "演示样例 2", "演示样例 3"]
NEUTRAL_DESCRIPTION = "预置合成多单据校验样例"
BAD_VIN_SAMPLE = "JFL25P02L086208-01"

# The exact closed error contracts (detail envelope; fixed generic messages)
DEMO_ERROR_404 = {"error": "DEMO_FIXTURE_NOT_FOUND", "message": "未找到演示样例"}
DEMO_ERROR_503 = {"error": "DEMO_FIXTURE_UNAVAILABLE", "message": "演示样例暂不可用"}
DEMO_ERROR_500 = {"error": "DEMO_CHECK_FAILED", "message": "校验执行失败，请稍后重试"}


def make_client() -> TestClient:
    os.environ.pop("TASK4_WEB_TOKEN", None)
    return TestClient(webapp.app)


def _assert_closed_error(response, status: int, expected: dict) -> None:
    """The runtime body is exactly the closed nested detail envelope."""
    assert response.status_code == status, response.text
    assert response.json() == {"detail": expected}


def _demo_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_demo_fixtures_closed_allow_list():
    """GET /api/demo/fixtures: exactly the three permitted options with
    neutral code-owned copy, validated synthetic metadata only, no
    path/application/rules/expected-outcome exposure."""
    client = make_client()
    r = client.get("/api/demo/fixtures")
    assert r.status_code == 200, r.text
    payload = r.json()
    # T07 additive: the server-owned batch cap is exposed on the option list
    # so React never hard-codes a second limit.
    assert set(payload.keys()) == {"fixtures", "batch_max_n"}
    assert payload["batch_max_n"] == 50
    options = payload["fixtures"]
    assert [o["fixture_id"] for o in options] == EXPECTED_FIXTURE_IDS
    assert [o["title"] for o in options] == EXPECTED_NEUTRAL_TITLES
    for opt in options:
        assert set(opt.keys()) == {
            "fixture_id",
            "title",
            "description",
            "field_source",
            "step2_sample_id",
        }
        assert opt["field_source"] == "synthetic"
        assert opt["description"] == NEUTRAL_DESCRIPTION
        sid = opt["step2_sample_id"]
        assert (STEP2 / f"{sid}_page_order.json").is_file(), sid
        # no filename/path, raw application, rules, or expected_verdicts
        assert "path" not in opt
        assert "file" not in opt
        assert "application" not in opt
        assert "expected_verdicts" not in opt
        # neutral copy never leaks the known expected outcome or fixture ids
        assert "一致" not in opt["title"]
        assert "不一致" not in opt["title"]
        assert "expected" not in opt["title"].lower()
        assert "label" not in opt["title"].lower()


def test_demo_check_closed_response_with_bad_vin():
    """POST /api/demo/check: one server-resident fixture, C-DEMO track,
    typed report, safe server-projected Step2 evidence link."""
    client = make_client()
    r = client.post(
        "/api/demo/check", json={"fixture_id": "app_demo_step2_bad_vin"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) == {
        "track",
        "data_scope",
        "fixture_id",
        "application_id",
        "summary",
        "checks",
        "config",
        "evidence_links",
    }
    assert body["track"] == "C-DEMO"
    assert body["data_scope"] == "synthetic"
    assert body["fixture_id"] == "app_demo_step2_bad_vin"
    assert body["application_id"] == "DEMO-STEP2-JFL25P02L086208-01-BADVIN"
    assert "html" not in body
    assert "rules_path" not in body

    summary = body["summary"]
    assert set(summary.keys()) == {
        "consistent",
        "inconsistent",
        "uncertain",
        "skipped",
        "coverage",
        "total",
        "total_including_skipped",
    }
    assert summary["inconsistent"] >= 1

    checks = body["checks"]
    assert isinstance(checks, list) and checks
    vin = next(c for c in checks if c["rule_id"] == "R_VIN_CROSS")
    assert vin["verdict"] == "inconsistent"
    assert set(vin.keys()) >= {
        "rule_id",
        "name",
        "verdict",
        "severity",
        "message",
        "snapshots",
    }
    assert vin["snapshots"]
    snap = vin["snapshots"][0]
    assert set(snap.keys()) >= {"doc_id", "doc_type", "field", "raw", "normalized"}
    assert isinstance(snap["raw"], (str, type(None)))
    assert "diff_highlight" in vin and "score" in vin

    config = body["config"]
    assert set(config.keys()) == {
        "rule_config_version",
        "rule_package",
        "rule_changelog",
    }
    assert config["rule_config_version"] is not None

    links = body["evidence_links"]
    assert len(links) == 1
    link = links[0]
    assert set(link.keys()) == {"kind", "label", "sample_id", "href", "limitation"}
    assert link["kind"] == "step2_sample"
    assert link["sample_id"] == BAD_VIN_SAMPLE
    assert link["label"]
    assert link["limitation"]
    # relative same-origin path, no scheme/netloc, under /api/step2/
    assert link["href"].startswith("/api/step2/")
    assert "://" not in link["href"]
    assert not link["href"].startswith("//")
    detail = client.get(link["href"])
    assert detail.status_code == 200, detail.text
    assert detail.json()["sample_id"] == BAD_VIN_SAMPLE


def test_demo_check_ok_fixture_consistent():
    """The ok fixture still runs and yields a zero-inconsistent summary."""
    client = make_client()
    r = client.post("/api/demo/check", json={"fixture_id": "app_demo_step2_ok"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"]["inconsistent"] == 0
    assert body["summary"]["consistent"] >= 1
    assert body["application_id"] == "DEMO-STEP2-JFL25P02L080310-01-OK"


def test_demo_check_unknown_and_shape_rejected():
    """Unknown/path-like/extra/shape values fail closed; only bare fixture_id.
    The 404 is the exact closed envelope and never reflects caller input."""
    client = make_client()
    r = client.post("/api/demo/check", json={"fixture_id": "not-a-demo"})
    _assert_closed_error(r, 404, DEMO_ERROR_404)
    assert "not-a-demo" not in r.text

    # path-like or filename values are not in the allow-list -> 404, never
    # joined to a path and never reflected back
    for bad in ("../app_demo_step2_ok.json", "app_demo_step2_ok.json", "/etc/passwd"):
        r = client.post("/api/demo/check", json={"fixture_id": bad})
        _assert_closed_error(r, 404, DEMO_ERROR_404)
        assert bad not in r.text
        assert "app_demo_step2_ok" not in r.text
        assert "etc" not in r.text

    # extra field -> typed 422
    r = client.post(
        "/api/demo/check",
        json={"fixture_id": "app_demo_step2_ok", "application": {}},
    )
    assert r.status_code == 422, r.text

    # missing field -> typed 422
    r = client.post("/api/demo/check", json={})
    assert r.status_code == 422, r.text


def test_demo_check_fail_closed_missing_file(tmp_path, monkeypatch):
    """Allow-listed but missing fixture file -> exact closed 503 envelope with
    no basename/locator exposure."""
    monkeypatch.setattr(webapp, "FIXTURES", tmp_path)
    monkeypatch.setattr(webapp, "DEMO_FIXTURES", {"broken": "does_not_exist.json"})
    client = make_client()
    r = client.post("/api/demo/check", json={"fixture_id": "broken"})
    _assert_closed_error(r, 503, DEMO_ERROR_503)
    assert "does_not_exist.json" not in r.text
    assert "broken" not in r.text


def test_demo_check_fail_closed_non_synthetic(tmp_path, monkeypatch):
    """Non-synthetic meta -> exact closed 503: only field_source=synthetic."""
    fx = tmp_path / "nonsynth.json"
    fx.write_text(
        json.dumps(
            {
                "application_id": "X",
                "documents": [],
                "meta": {
                    "field_source": "real",
                    "step2_sample_id": BAD_VIN_SAMPLE,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(webapp, "FIXTURES", tmp_path)
    monkeypatch.setattr(webapp, "DEMO_FIXTURES", {"nonsynth": "nonsynth.json"})
    client = make_client()
    r = client.post("/api/demo/check", json={"fixture_id": "nonsynth"})
    _assert_closed_error(r, 503, DEMO_ERROR_503)
    assert "nonsynth" not in r.text


def test_demo_check_fail_closed_missing_step2_sample(tmp_path, monkeypatch):
    """Synthetic fixture without its verified Step2 sample -> exact closed
    503 with no sample-id/locator exposure."""
    fx = tmp_path / "nostep2.json"
    fx.write_text(
        json.dumps(
            {
                "application_id": "Y",
                "documents": [],
                "meta": {
                    "field_source": "synthetic",
                    "step2_sample_id": "NO-SUCH-SAMPLE",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(webapp, "FIXTURES", tmp_path)
    monkeypatch.setattr(webapp, "DEMO_FIXTURES", {"nostep2": "nostep2.json"})
    client = make_client()
    r = client.post("/api/demo/check", json={"fixture_id": "nostep2"})
    _assert_closed_error(r, 503, DEMO_ERROR_503)
    assert "NO-SUCH-SAMPLE" not in r.text
    assert "nostep2" not in r.text


def test_demo_check_500_generic_no_internal_detail(monkeypatch):
    """A check exception containing an internal path yields the exact closed
    500 with no exception text/path disclosure (chaining preserved for logs)."""
    def _explode(application, rules_path):
        raise RuntimeError("internal /srv/secret/rules.yaml exploded")

    monkeypatch.setattr(webapp, "_run_check", _explode)
    client = make_client()
    r = client.post("/api/demo/check", json={"fixture_id": "app_demo_step2_ok"})
    _assert_closed_error(r, 500, DEMO_ERROR_500)
    assert "/srv/secret" not in r.text
    assert "rules.yaml" not in r.text
    assert "exploded" not in r.text
    assert "internal" not in r.text


def test_demo_check_http_exception_is_generic_500(monkeypatch):
    """Check-path HTTP errors cannot bypass the fixed public 500 envelope."""
    leaked_code = "INTERNAL_CHECK_HTTP_FAILURE"
    leaked_fixture_id = "app_demo_step2_ok"
    leaked_path = "/srv/secret/rules.yaml"

    def _explode(application, rules_path):
        raise HTTPException(
            status_code=409,
            detail={
                "error": leaked_code,
                "message": f"fixture {leaked_fixture_id} failed at {leaked_path}",
            },
        )

    monkeypatch.setattr(webapp, "_run_check", _explode)
    client = make_client()
    r = client.post("/api/demo/check", json={"fixture_id": leaked_fixture_id})
    _assert_closed_error(r, 500, DEMO_ERROR_500)
    for secret in (leaked_code, leaked_fixture_id, leaked_path, "rules.yaml"):
        assert secret not in r.text


def test_demo_fixtures_503_closed_envelope(tmp_path, monkeypatch):
    """GET /api/demo/fixtures fails closed with the same registered 503
    envelope when a permitted fixture is unavailable."""
    monkeypatch.setattr(webapp, "FIXTURES", tmp_path)
    monkeypatch.setattr(webapp, "DEMO_FIXTURES", {"broken": "does_not_exist.json"})
    client = make_client()
    r = client.get("/api/demo/fixtures")
    _assert_closed_error(r, 503, DEMO_ERROR_503)
    assert "does_not_exist.json" not in r.text


def test_demo_react_shell_no_store():
    """GET /demo/react serves the built React shell with no-store."""
    client = make_client()
    r = client.get("/demo/react")
    assert r.status_code == 200, r.text
    assert r.headers.get("cache-control") == "no-store"
    assert '<div id="root"></div>' in r.text
    assert 'src="/static/react/assets/' in r.text


def test_demo_react_shell_503_when_build_missing(monkeypatch):
    """Missing/incomplete build fails closed with DEMO_REACT_UNAVAILABLE."""
    monkeypatch.setattr(webapp, "S01_REACT_INDEX", Path("/nonexistent/index.html"))
    client = make_client()
    r = client.get("/demo/react")
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "DEMO_REACT_UNAVAILABLE"


def test_legacy_surfaces_unchanged():
    """Canonical root serves the React shell while the demo APIs remain
    usable (Issue #54 cutover; the legacy root is rollback-only)."""
    client = make_client()
    r = client.get("/")
    assert r.status_code == 200
    assert "/static/react/assets/" in r.text

    f = client.get("/api/fixtures")
    assert f.status_code == 200
    names = {i["file"] for i in f.json()["fixtures"]}
    assert "app_demo_step2_ok.json" in names

    data = _demo_fixture("app_demo_step2_ok.json")
    c = client.post("/api/check", json={"application": data})
    assert c.status_code == 200, c.text
    body = c.json()
    assert set(body.keys()) == {"report", "html", "rules_path"}
    assert body["report"]["summary"]["inconsistent"] == 0

    fx = client.get("/api/fixtures/app_demo_step2_bad_vin.json")
    assert fx.status_code == 200
    assert fx.json()["application_id"] == "DEMO-STEP2-JFL25P02L086208-01-BADVIN"


# Every T06-owned DTO is closed: generated schema must carry
# additionalProperties: false (and the request stays extra=forbid).
CLOSED_DEMO_SCHEMAS = [
    "DemoFixtureOption",
    "DemoFixturesResponse",
    "DemoCheckRequest",
    "DemoSummary",
    "DemoConfigInfo",
    "DemoSnapshotItem",
    "DemoDiffHighlight",
    "DemoCheckItem",
    "DemoEvidenceLink",
    "DemoCheckResponse",
    "DemoErrorDetail",
    "DemoErrorResponse",
]


def test_openapi_demo_contract_closed():
    """The migrated seam shows concrete closed DTOs and the exact closed
    nested error envelope for every declared demo error response."""
    client = make_client()
    spec = client.get("/openapi.json").json()
    paths = spec["paths"]
    assert "/api/demo/fixtures" in paths
    assert "/api/demo/check" in paths
    assert "/demo/react" in paths

    schemas = spec["components"]["schemas"]

    post = paths["/api/demo/check"]["post"]
    req_ref = post["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    assert req_ref.endswith("/DemoCheckRequest")
    req = schemas["DemoCheckRequest"]
    assert set(req["properties"].keys()) == {"fixture_id"}
    assert req["additionalProperties"] is False
    # POST 404/500/503 all reference the closed nested error envelope
    for status in ("404", "500", "503"):
        err_ref = post["responses"][status]["content"]["application/json"][
            "schema"
        ]["$ref"]
        assert err_ref.endswith("/DemoErrorResponse"), status

    get = paths["/api/demo/fixtures"]["get"]
    assert "503" in get["responses"], "GET fixtures must declare its 503"
    get_err_ref = get["responses"]["503"]["content"]["application/json"][
        "schema"
    ]["$ref"]
    assert get_err_ref.endswith("/DemoErrorResponse")

    # the envelope itself is closed and contains only detail -> DemoErrorDetail
    err_response = schemas["DemoErrorResponse"]
    assert set(err_response["properties"].keys()) == {"detail"}
    assert err_response["additionalProperties"] is False
    err_detail = schemas["DemoErrorDetail"]
    assert set(err_detail["properties"].keys()) == {"error", "message"}
    assert err_detail["additionalProperties"] is False

    resp = schemas["DemoCheckResponse"]
    assert set(resp["properties"].keys()) == {
        "track",
        "data_scope",
        "fixture_id",
        "application_id",
        "summary",
        "checks",
        "config",
        "evidence_links",
    }
    assert resp["properties"]["track"].get("const") == "C-DEMO"
    assert resp["properties"]["data_scope"].get("const") == "synthetic"

    fixtures_schema = schemas["DemoFixturesResponse"]
    assert set(fixtures_schema["properties"].keys()) == {
        "fixtures",
        "batch_max_n",
    }
    assert fixtures_schema["properties"]["batch_max_n"]["type"] == "integer"

    # every T06-owned schema is closed, not merely "not open"
    for name in CLOSED_DEMO_SCHEMAS:
        assert name in schemas, name
        assert schemas[name]["additionalProperties"] is False, name
