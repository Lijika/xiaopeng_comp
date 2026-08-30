"""T07 / Issue #41: bounded synchronous demo batch check + read-only
fixed-main evaluation-summary projection under /api/demo/*.

Red-green boundary: FastAPI alone owns the batch count cap (enforced before
any fixture I/O), the fixture allow-list, check execution, ordered per-item
terminal outcomes, the enclosing completed/partial/failed outcome, the
fixed-main evaluation computation, and the C-DEV-REG / UNVERIFIED claim
labels.  The browser sends only ``fixture_ids`` and never derives PASS.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from task4_consistency.models import Application
from task4_consistency.web import app as webapp

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures" / "applications"

FIXTURE_OK = "app_demo_layout_ok"
FIXTURE_BAD_VIN = "app_demo_layout_bad_vin"
FIXTURE_FMT = "app_demo_layout_fmt"

BATCH_CAP = 50
BATCH_ITEM_FAILED_MSG = "条目校验失败，请稍后重试"
BATCH_TOO_LARGE_MSG = f"批量校验数量超过服务端上限 {BATCH_CAP}"
EVAL_UNAVAILABLE_MSG = "评估摘要暂不可用"

# The exact closed error contracts (detail envelope; fixed generic messages)
DEMO_ERROR_404 = {"error": "DEMO_FIXTURE_NOT_FOUND", "message": "未找到演示样例"}
DEMO_ERROR_400_CAP = {"error": "DEMO_BATCH_TOO_LARGE", "message": BATCH_TOO_LARGE_MSG}
DEMO_ERROR_503_EVAL = {"error": "DEMO_EVALUATION_UNAVAILABLE", "message": EVAL_UNAVAILABLE_MSG}


def make_client() -> TestClient:
    os.environ.pop("TASK4_WEB_TOKEN", None)
    return TestClient(webapp.app)


def _assert_closed_error(response, status: int, expected: dict) -> None:
    """The runtime body is exactly the closed nested detail envelope."""
    assert response.status_code == status, response.text
    assert response.json() == {"detail": expected}


def _demo_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_demo_fixtures_expose_server_owned_batch_cap():
    """GET /api/demo/fixtures exposes the server-owned batch cap so React
    never hard-codes a second limit."""
    client = make_client()
    r = client.get("/api/demo/fixtures")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["batch_max_n"] == BATCH_CAP


def test_demo_batch_check_typed_ordered_response():
    """POST /api/demo/check/batch with two permitted ids: one ordered result
    per requested id, explicit terminal outcomes, totals from completed items
    only, and no legacy/HTML/internal fields."""
    client = make_client()
    r = client.post(
        "/api/demo/check/batch",
        json={"fixture_ids": [FIXTURE_OK, FIXTURE_BAD_VIN]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) == {
        "track",
        "data_scope",
        "requested",
        "completed",
        "failed",
        "outcome",
        "totals",
        "results",
    }
    assert body["track"] == "C-DEMO"
    assert body["data_scope"] == "synthetic"
    assert body["requested"] == 2
    assert body["completed"] == 2
    assert body["failed"] == 0
    assert body["outcome"] == "completed"
    assert "html" not in body
    assert "rules_path" not in body
    assert "checks" not in body

    totals = body["totals"]
    assert set(totals.keys()) == {"consistent", "inconsistent", "uncertain", "skipped"}
    assert totals["inconsistent"] >= 1  # the bad_vin item is decisive
    assert totals["consistent"] >= 1

    results = body["results"]
    assert len(results) == 2
    # strict request order, one result per requested id
    assert [item["fixture_id"] for item in results] == [FIXTURE_OK, FIXTURE_BAD_VIN]
    for item in results:
        assert set(item.keys()) == {
            "fixture_id",
            "outcome",
            "application_id",
            "summary",
            "issues",
            "error",
        }
        assert item["outcome"] == "completed"
        assert item["application_id"]
        assert item["error"] is None
        summary = item["summary"]
        assert set(summary.keys()) == {
            "consistent",
            "inconsistent",
            "uncertain",
            "skipped",
            "coverage",
            "total",
            "total_including_skipped",
        }
        assert isinstance(item["issues"], list)

    bad_vin = results[1]
    assert bad_vin["summary"]["inconsistent"] == 1
    vin_issue = next(i for i in bad_vin["issues"] if i["rule_id"] == "R_VIN_CROSS")
    assert set(vin_issue.keys()) == {"rule_id", "verdict", "message", "reason_codes"}
    assert vin_issue["verdict"] == "inconsistent"


def test_demo_batch_check_cap_enforced_before_io(monkeypatch):
    """Over-cap requests are rejected before any fixture I/O: even ids that
    are not on the allow-list hit the cap rejection first, proving no fixture
    read/validation precedes the cap check.  The cap number is server-owned."""
    original_load = webapp._load_demo_fixture
    reads: list[str] = []

    def _spy_load(fixture_id: str) -> dict:
        reads.append(fixture_id)
        return original_load(fixture_id)

    monkeypatch.setattr(webapp, "_load_demo_fixture", _spy_load)
    client = make_client()
    # 51 unknown ids: cap check must win over the allow-list 404
    r = client.post(
        "/api/demo/check/batch",
        json={"fixture_ids": ["not-a-demo"] * (BATCH_CAP + 1)},
    )
    _assert_closed_error(r, 400, DEMO_ERROR_400_CAP)
    assert "not-a-demo" not in r.text

    # 51 permitted ids: same rejection
    r = client.post(
        "/api/demo/check/batch",
        json={"fixture_ids": [FIXTURE_OK] * (BATCH_CAP + 1)},
    )
    _assert_closed_error(r, 400, DEMO_ERROR_400_CAP)
    # no fixture loader call for either over-cap request
    assert reads == []

    # exactly the cap is permitted (and does load the allowed fixture)
    r = client.post(
        "/api/demo/check/batch",
        json={"fixture_ids": [FIXTURE_OK] * BATCH_CAP},
    )
    assert r.status_code == 200, r.text
    assert r.json()["requested"] == BATCH_CAP
    assert len(reads) == BATCH_CAP
    assert set(reads) == {FIXTURE_OK}


def test_demo_batch_check_empty_and_shape_rejected():
    """Empty/missing/extra/non-list request shapes fail closed before work
    with the exact fixed 422 envelope; only a bare fixture_ids list is
    accepted and no submitted value is reflected."""
    client = make_client()
    for payload in (
        {"fixture_ids": "not-a-list"},
        {"fixture_ids": [FIXTURE_OK], "application": {}},
    ):
        r = client.post("/api/demo/check/batch", json=payload)
        assert r.status_code == 422, (payload, r.text)
        assert r.json() == {
            "detail": {"error": "DEMO_BATCH_INVALID", "message": "批量校验请求无效"}
        }
        for value in ("not-a-list", "application", FIXTURE_OK):
            if value in str(payload):
                assert value not in r.text, (payload, r.text)


def test_request_validation_handler_is_shared_with_s14():
    """The app keeps one validation handler so route-specific contracts do
    not get replaced by a later registration for another slice."""
    assert (
        webapp.app.exception_handlers[RequestValidationError]
        is webapp._sanitized_validation_handler
    )


def test_demo_batch_check_unknown_id_closed_404():
    """An unknown fixture id fails with the exact closed 404 envelope before
    any check runs, and the caller value is never reflected back."""
    client = make_client()
    r = client.post(
        "/api/demo/check/batch", json={"fixture_ids": ["not-a-demo", FIXTURE_OK]}
    )
    _assert_closed_error(r, 404, DEMO_ERROR_404)
    assert "not-a-demo" not in r.text


def test_demo_batch_check_per_item_failure_generic(monkeypatch):
    """A permitted fixture that fails to load is a bounded per-item failure:
    enclosing outcome partial, totals exclude failed items, and the item
    carries only the fixed generic message — never the loader detail."""
    original = webapp._load_demo_fixture

    def _break_fmt(fixture_id):
        if fixture_id == FIXTURE_FMT:
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "DEMO_FIXTURE_UNAVAILABLE",
                    "message": f"internal {FIXTURE_FMT}.json failed to load",
                },
            )
        return original(fixture_id)

    monkeypatch.setattr(webapp, "_load_demo_fixture", _break_fmt)
    client = make_client()
    r = client.post(
        "/api/demo/check/batch",
        json={"fixture_ids": [FIXTURE_OK, FIXTURE_FMT, FIXTURE_BAD_VIN]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["requested"] == 3
    assert body["completed"] == 2
    assert body["failed"] == 1
    assert body["outcome"] == "partial"
    assert body["totals"]["inconsistent"] == 1  # only the two completed items
    assert body["totals"]["consistent"] >= 1

    results = body["results"]
    assert [item["fixture_id"] for item in results] == [
        FIXTURE_OK,
        FIXTURE_FMT,
        FIXTURE_BAD_VIN,
    ]
    failed_item = results[1]
    assert failed_item["outcome"] == "failed"
    assert failed_item["application_id"] is None
    assert failed_item["summary"] is None
    assert failed_item["issues"] == []
    assert failed_item["error"] == BATCH_ITEM_FAILED_MSG
    # the loader detail never crosses the API (the fixture_id echo is the
    # required per-request item identifier, not a loader leak)
    assert "failed to load" not in r.text


def test_demo_batch_check_all_fail_enclosing_failed(monkeypatch):
    """Every item failed -> enclosing outcome failed, completed == 0."""
    def _explode(fixture_id):
        raise RuntimeError("internal /srv/secret/rules.yaml exploded")

    monkeypatch.setattr(webapp, "_load_demo_fixture", _explode)
    client = make_client()
    r = client.post(
        "/api/demo/check/batch", json={"fixture_ids": [FIXTURE_OK, FIXTURE_BAD_VIN]}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["completed"] == 0
    assert body["failed"] == 2
    assert body["outcome"] == "failed"
    assert body["totals"] == {
        "consistent": 0,
        "inconsistent": 0,
        "uncertain": 0,
        "skipped": 0,
    }
    assert "/srv/secret" not in r.text
    assert "rules.yaml" not in r.text


def test_demo_batch_check_check_failure_generic(monkeypatch):
    """An engine/check exception is a bounded generic per-item failure with
    no exception text or internal path disclosure."""
    original_engine = webapp._engine

    def _exploding_engine():
        engine = original_engine()

        def _explode(application):
            raise RuntimeError("internal /srv/secret/engine crashed")

        engine.run = _explode
        return engine

    monkeypatch.setattr(webapp, "_engine", _exploding_engine)
    client = make_client()
    r = client.post(
        "/api/demo/check/batch", json={"fixture_ids": [FIXTURE_OK, FIXTURE_BAD_VIN]}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "failed"
    assert body["failed"] == 2
    for item in body["results"]:
        assert item["outcome"] == "failed"
        assert item["error"] == BATCH_ITEM_FAILED_MSG
    assert "/srv/secret" not in r.text
    assert "engine crashed" not in r.text


def test_demo_batch_check_late_projection_failure_zero_totals(monkeypatch):
    """A late item-projection failure (summary valid, issue projection
    raising) yields an explicit failed item with all-zero completed-only
    totals: counts are committed only after the whole item succeeds."""
    original_engine = webapp._engine

    class _LateExplosion:
        def __init__(self, report):
            self._report = report

        @property
        def application_id(self):
            return self._report.application_id

        @property
        def summary(self):
            return self._report.summary

        @property
        def checks(self):
            raise RuntimeError("late projection exploded")

    def _wrapped_engine():
        engine = original_engine()
        original_run = engine.run

        def run(application):
            return _LateExplosion(original_run(application))

        engine.run = run
        return engine

    monkeypatch.setattr(webapp, "_engine", _wrapped_engine)
    client = make_client()
    r = client.post("/api/demo/check/batch", json={"fixture_ids": [FIXTURE_OK]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["requested"] == 1
    assert body["completed"] == 0
    assert body["failed"] == 1
    assert body["outcome"] == "failed"
    assert body["totals"] == {
        "consistent": 0,
        "inconsistent": 0,
        "uncertain": 0,
        "skipped": 0,
    }
    item = body["results"][0]
    assert item["outcome"] == "failed"
    assert item["error"] == BATCH_ITEM_FAILED_MSG
    assert "exploded" not in r.text


def test_demo_batch_check_single_engine_snapshot(monkeypatch):
    """One synchronous batch request observes exactly one rules/engine
    snapshot: ``_engine()`` is called once and every permitted item runs on
    that same engine."""
    original_engine = webapp._engine
    engine_calls: list[str] = []
    run_ids: list[str] = []

    class _SentinelEngine:
        def __init__(self):
            self._inner = original_engine()

        def run(self, application):
            run_ids.append(application.application_id)
            return self._inner.run(application)

    def _counting_engine():
        engine_calls.append("engine")
        return _SentinelEngine()

    monkeypatch.setattr(webapp, "_engine", _counting_engine)
    client = make_client()
    r = client.post(
        "/api/demo/check/batch",
        json={"fixture_ids": [FIXTURE_OK, FIXTURE_BAD_VIN]},
    )
    assert r.status_code == 200, r.text
    assert len(engine_calls) == 1
    assert len(run_ids) == 2
    assert r.json()["outcome"] == "completed"


def test_demo_batch_check_engine_construction_failure_generic(monkeypatch):
    """Engine construction failure is a bounded, closed terminal result for
    every requested ID: HTTP 200 with explicit failed items and zero
    completed-only totals, completed before any fixture I/O, with no
    internal exception/path/detail crossing the boundary."""
    engine_calls: list[str] = []
    original_load = webapp._load_demo_fixture
    fixture_reads: list[str] = []

    def _broken_engine():
        engine_calls.append("engine")
        raise RuntimeError("internal /srv/secret/rules.yaml construction failed")

    def _spy_load(fixture_id: str) -> dict:
        fixture_reads.append(fixture_id)
        return original_load(fixture_id)

    monkeypatch.setattr(webapp, "_engine", _broken_engine)
    monkeypatch.setattr(webapp, "_load_demo_fixture", _spy_load)
    client = make_client()
    r = client.post(
        "/api/demo/check/batch",
        json={"fixture_ids": [FIXTURE_OK, FIXTURE_BAD_VIN]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # one accepted request attempts exactly one engine/rules snapshot, and
    # the closed failure response is produced before any fixture I/O
    assert len(engine_calls) == 1
    assert fixture_reads == []
    assert body["requested"] == 2
    assert body["completed"] == 0
    assert body["failed"] == 2
    assert body["outcome"] == "failed"
    assert body["totals"] == {
        "consistent": 0,
        "inconsistent": 0,
        "uncertain": 0,
        "skipped": 0,
    }
    results = body["results"]
    assert len(results) == 2
    # request order preserved; no requested item omitted or implied success
    assert [item["fixture_id"] for item in results] == [
        FIXTURE_OK,
        FIXTURE_BAD_VIN,
    ]
    for item in results:
        assert item["outcome"] == "failed"
        assert item["application_id"] is None
        assert item["summary"] is None
        assert item["issues"] == []
        assert item["error"] == BATCH_ITEM_FAILED_MSG
    # no internal exception, path, rule detail, or caller-unrelated data
    assert "/srv/secret" not in r.text
    assert "rules.yaml" not in r.text
    assert "construction failed" not in r.text


def test_demo_evaluate_summary_available_typed():
    """GET /api/demo/evaluate/summary: read-only fixed-main projection with
    server-owned C-DEV-REG / UNVERIFIED claim labels; only summary counts,
    rates, warnings, and the honesty note cross the API."""
    client = make_client()
    r = client.get("/api/demo/evaluate/summary")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body.keys()) == {
        "summary_state",
        "suite",
        "claim",
        "performance_gap",
        "scope",
        "counts",
        "rates",
        "warnings",
        "honesty_note",
    }
    assert body["summary_state"] == "available"
    assert body["suite"] == "main"
    assert body["claim"] == "C-DEV-REG"
    assert body["performance_gap"] == "UNVERIFIED"
    assert body["scope"]
    assert body["honesty_note"]

    counts = body["counts"]
    assert set(counts.keys()) == {
        "n_apps_loaded",
        "n_check_ok",
        "n_check_fail",
        "total_pairs",
        "decisive_pairs",
        "true_positive",
        "true_negative",
        "false_positive",
        "false_negative",
        "uncertain_when_labeled",
        "n_inconsistent_labeled_decisive",
        "n_expected_inconsistent",
        "n_missed_inconsistent",
    }
    assert counts["n_apps_loaded"] >= 100
    assert counts["total_pairs"] >= 100
    assert counts["decisive_pairs"] >= 100

    rates = body["rates"]
    assert set(rates.keys()) == {
        "coverage",
        "false_positive_rate",
        "false_negative_rate",
        "accuracy",
        "miss_rate",
        "uncertain_rate",
        "mean_app_coverage",
    }
    assert 0.0 <= rates["coverage"] <= 1.0
    assert rates["false_positive_rate"] == 0.0
    assert rates["false_negative_rate"] == 0.0

    # the closed projection carries no legacy html/threshold/pair payload
    assert "html" not in body
    assert "pass_thresholds" not in body
    assert "pairs" not in body
    assert "per_application" not in body
    assert "rules_path" not in body
    assert "PASS" not in r.text


def test_demo_evaluate_summary_empty_state(tmp_path, monkeypatch):
    """A smoke/empty fixed-main computation yields summary_state=empty with
    nullable counts/rates — never zero-valued success."""
    from task4_consistency.evaluate import evaluate_paths, load_rules

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    rules = ROOT / "configs" / "rules_auto_lease.yaml"
    smoke = evaluate_paths([empty_dir], load_rules(rules), suite="main")

    monkeypatch.setattr(
        "task4_consistency.evaluate.evaluate_suite",
        lambda suite, rules_path: smoke,
    )
    client = make_client()
    r = client.get("/api/demo/evaluate/summary")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary_state"] == "empty"
    assert body["claim"] == "C-DEV-REG"
    assert body["performance_gap"] == "UNVERIFIED"
    assert body["counts"] is None
    assert body["rates"] is None
    assert isinstance(body["warnings"], list)
    assert body["honesty_note"]
    # an empty summary must not claim zero success
    assert "0.0" not in json.dumps(body["rates"] if body["rates"] else {})


def test_demo_evaluate_summary_unavailable_503(monkeypatch):
    """Unavailable evaluation is a distinct closed 503 with no internal
    detail."""
    def _explode(suite, rules_path):
        raise RuntimeError("internal /srv/secret/evaluate.py crashed")

    monkeypatch.setattr("task4_consistency.evaluate.evaluate_suite", _explode)
    client = make_client()
    r = client.get("/api/demo/evaluate/summary")
    _assert_closed_error(r, 503, DEMO_ERROR_503_EVAL)
    assert "/srv/secret" not in r.text
    assert "evaluate.py" not in r.text


# Every T07-owned DTO is closed: generated schema must carry
# additionalProperties: false (and the request stays extra=forbid).
CLOSED_T07_SCHEMAS = [
    "DemoBatchRequest",
    "DemoBatchCheckResponse",
    "DemoBatchTotals",
    "DemoBatchIssue",
    "DemoBatchItem",
    "DemoEvaluationSummaryResponse",
    "DemoEvalCounts",
    "DemoEvalRates",
]


def test_openapi_t07_contract_closed():
    """The T07 seams show concrete closed DTOs and the exact closed nested
    error envelope on every declared error response."""
    client = make_client()
    spec = client.get("/openapi.json").json()
    paths = spec["paths"]
    assert "/api/demo/check/batch" in paths
    assert "/api/demo/evaluate/summary" in paths

    schemas = spec["components"]["schemas"]

    post = paths["/api/demo/check/batch"]["post"]
    req_ref = post["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    assert req_ref.endswith("/DemoBatchRequest")
    req = schemas["DemoBatchRequest"]
    assert set(req["properties"].keys()) == {"fixture_ids", "applications"}
    assert req["additionalProperties"] is False
    assert req["properties"]["fixture_ids"]["items"]["type"] == "string"
    # B4: invalid request shapes use the closed typed error contract, not the
    # generic HTTPValidationError shape.
    assert "422" in post["responses"]
    for status in ("400", "404", "422", "503"):
        err_ref = post["responses"][status]["content"]["application/json"][
            "schema"
        ]["$ref"]
        assert err_ref.endswith("/DemoErrorResponse"), status

    get = paths["/api/demo/evaluate/summary"]["get"]
    assert "503" in get["responses"]
    get_err_ref = get["responses"]["503"]["content"]["application/json"][
        "schema"
    ]["$ref"]
    assert get_err_ref.endswith("/DemoErrorResponse")

    resp = schemas["DemoBatchCheckResponse"]
    assert set(resp["properties"].keys()) == {
        "track",
        "data_scope",
        "requested",
        "completed",
        "failed",
        "outcome",
        "totals",
        "results",
    }
    assert resp["properties"]["track"].get("const") == "C-DEMO"
    data_scope = resp["properties"]["data_scope"]
    assert "synthetic" in (data_scope.get("enum") or [data_scope.get("const")])
    assert "uploaded" in (data_scope.get("enum") or [])
    assert "completed" in resp["properties"]["outcome"]["enum"]
    assert "partial" in resp["properties"]["outcome"]["enum"]
    assert "failed" in resp["properties"]["outcome"]["enum"]

    item = schemas["DemoBatchItem"]
    assert set(item["properties"].keys()) == {
        "fixture_id",
        "outcome",
        "application_id",
        "summary",
        "issues",
        "error",
    }
    assert "completed" in item["properties"]["outcome"]["enum"]
    assert "failed" in item["properties"]["outcome"]["enum"]

    summary = schemas["DemoEvaluationSummaryResponse"]
    assert set(summary["properties"].keys()) == {
        "summary_state",
        "suite",
        "claim",
        "performance_gap",
        "scope",
        "counts",
        "rates",
        "warnings",
        "honesty_note",
    }
    assert summary["properties"]["claim"].get("const") == "C-DEV-REG"
    assert summary["properties"]["performance_gap"].get("const") == "UNVERIFIED"
    assert summary["properties"]["suite"].get("const") == "main"
    assert "available" in summary["properties"]["summary_state"]["enum"]
    assert "empty" in summary["properties"]["summary_state"]["enum"]

    fixtures_schema = schemas["DemoFixturesResponse"]
    assert set(fixtures_schema["properties"].keys()) == {
        "fixtures",
        "batch_max_n",
    }
    assert fixtures_schema["properties"]["batch_max_n"]["type"] == "integer"

    for name in CLOSED_T07_SCHEMAS:
        assert name in schemas, name
        assert schemas[name]["additionalProperties"] is False, name


def test_startup_without_s12_worker_configuration_keeps_demo_routes_serving(
    monkeypatch,
) -> None:
    """Shared application construction with the Ticket #28 R2 required S12
    worker configuration: when TASK4_S12_WORKER_SUBJECT is absent the S12
    plane stays closed while the S07/S11 demo routes keep serving."""
    import os

    monkeypatch.delenv("TASK4_S12_WORKER_SUBJECT", raising=False)
    monkeypatch.delenv("TASK4_S12_STATE_PATH", raising=False)
    monkeypatch.delenv("TASK4_S12_CREDENTIAL", raising=False)
    monkeypatch.delenv("TASK4_S12_SUBJECT", raising=False)
    monkeypatch.setattr(webapp, "S12_WORKER_SUBJECT", "")
    monkeypatch.setattr(webapp, "S12_SERVICE", None)
    assert webapp.S12_WORKER_SUBJECT == ""
    assert webapp._s12_evaluation_service() is None
    client = make_client()
    assert client.get("/api/demo/fixtures").status_code == 200
    response = client.post(
        "/api/demo/check/batch",
        json={"fixture_ids": [FIXTURE_OK]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["completed"] == 1
    assert response.json()["outcome"] == "completed"
