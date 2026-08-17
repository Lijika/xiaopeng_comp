"""T10 release contracts (Issue #44/#54): contracted catalog gate, installed
package provenance/content/cache/security, and three-stage rollback with
fact preservation.

Group 1 -- contracted legacy entry catalog gate (Issue #54): one assertion
over the public ``scan_legacy_contract`` result (zero canonical source
edges, exact declared owners, frozen rollback occurrences, no direct-store
drift, no uncataloged legacy route).  The former test-local scanners and
allowlists were retired when the public replacement passed; the parameterized
public attack matrix lives in tests/test_t54_legacy_catalog.py.

Group 2 -- installed package provenance/content/cache/security: gated on
the ``TASK4_T10_INSTALLED_ROOT`` environment variable (set by the release
harness, which installs the wheel with ``pip --no-deps --target`` and puts
the installed site first on ``PYTHONPATH``).  Without the variable the
group skips.  Integration dependency: the harness (Lane C) must provide
TASK4_T10_INSTALLED_ROOT and the installed-first import environment, and
must neutralize ``[tool.pytest.ini_options] pythonpath = ["."]`` in
pyproject.toml (e.g. ``-o pythonpath=``): that option prepends the repo
root to ``sys.path`` ahead of ``PYTHONPATH`` and would shadow the
installed package for the provenance assertions.

Group 3 -- three-stage rollback rehearsal over one temporary SQLite
authority across three uvicorn starts: complete build -> accepted backend
fact -> partial build (missing hashed asset) -> explicit 503 on the
canonical and alias routes -> restored build -> shell and hashed assets 200
again, with server-returned authority revision, fact DTO, current route,
and history all unchanged.  Issue #54 cut over the canonical routes to the
qualified React build, so a partial build fails closed everywhere; the
deployment-only rollback rehearsal over the prior wheel lives in the
installed release harness.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

from tests.test_s01_http import (
    UvicornLoopback,
    auditor_auth_headers,
    demo_auth_headers,
    headers,
    submit,
)
from tests.test_s07_http import _environment
from tests.test_t01_http import _create_t01_app_environment
from task4_consistency.controlled.s01 import (
    ControlledScenarioService,
    ControlledScenarioTestDriver,
)
from task4_consistency.web.legacy_catalog import scan_legacy_contract

ROOT = Path(__file__).resolve().parents[1]

T10_APP_FACTORY = "tests.test_t10_release:create_t10_test_app"


def create_t10_test_app() -> Any:
    """T01-style test app that also enables the ``_test`` driver endpoints.

    Mirrors ``tests.test_t01_http.create_t01_test_app`` (state path from
    ``TASK4_S01_TEST_STATE_PATH``, optional React dir override from
    ``TASK4_S01_TEST_REACT_DIR``) and additionally wires
    ``S01_TEST_DRIVER`` so the rollback rehearsal can explicitly process
    and project the accepted fact over HTTP.
    """
    import task4_consistency.web.app as web

    web.S01_BACKGROUND_ENABLED = False
    web.S01_REQUIRE_CONFIGURED_STARTUP = False
    web.S01_SERVICE = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=Path(os.environ["TASK4_S01_TEST_STATE_PATH"]),
        recovery_verifier=None,
        worker_identity="t10-http-worker",
    )
    web.S01_TEST_DRIVER = ControlledScenarioTestDriver(web.S01_SERVICE)
    react_dir = os.environ.get("TASK4_S01_TEST_REACT_DIR", "").strip()
    if react_dir:
        web.S01_REACT_INDEX = Path(react_dir).resolve() / "index.html"
    return web.app

# --- Group 1: contracted legacy entry catalog gate (Issue #54) -------------
#
# The production gate is one catalog completeness/source-edge assertion over
# the public scanner (task4_consistency/web/legacy_catalog.py).  The former
# test-local scanners, allowlists and parser ownership were retired after the
# public replacement passed; the parameterized public attack matrix lives in
# tests/test_t54_legacy_catalog.py::TestPublicScanAttackMatrix (method flip,
# concatenation, new React callers, duplicate allowed-file calls, inline
# template calls, read-only diagnostics and the KB reload family).


def test_legacy_mutation_callers_are_frozen_to_exact_production_owners() -> None:
    """The production source tree satisfies the complete contracted catalog
    gate: exact declared owners, frozen rollback occurrences, zero canonical
    source edges, zero direct-store drift and no uncataloged legacy route."""
    report = scan_legacy_contract(ROOT)
    assert report.completeness_ok, (
        f"catalog completeness failed: "
        f"missing_owners={[m for m in report.missing_owners]} "
        f"uncataloged_routes={[r for r in report.uncataloged_routes]} "
        f"occurrence_mismatches={[m for m in report.occurrence_mismatches]}"
    )
    assert report.zero_canonical_source_edges, (
        "canonical legacy source edges must be zero: "
        + repr(
            [
                (edge.entry_id, edge.path, edge.family, edge.method, edge.occurrences)
                for edge in report.canonical_source_edges
            ]
        )
    )
    assert report.direct_store_mismatches == (), (
        "direct-store mutation callers changed: "
        + repr(
            [
                (m.path, m.token, m.observed, m.expected)
                for m in report.direct_store_mismatches
            ]
        )
    )
    assert report.ok


# --- Group 2: retired installed-package seams (Issue #54) -------------------
#
# The four installed-package tests of the fixed base were retired with
# replacement evidence at an equal-or-higher seam (retirement inventory
# rows R2/R3/R4 in /tmp/codex/ticket-54-evidence/retirement-inventory/):
# the release harness's installed provenance probe (step 7) plus the
# canonical T01 shell/cache/missing-build contracts run against the
# installed package (step 8) and the sealed observation bundle manifest
# (current/prior wheel SHA256) retain every assertion: import provenance,
# installed React shell + closed hashed references, JS/CSS presence,
# legacy static/templates, no sourcemaps, 200/no-store, direct index
# policy, immutable cache, and every minimized 503 with path-leak,
# cache, session and protected-route semantics.


# --- Group 3: three-stage rollback with fact preservation --------------------

_REACT_JS_ASSET = r"/static/react/assets/[A-Za-z0-9._/-]+\.js"

def _capture_s01_facts(
    server: UvicornLoopback,
    application_id: str,
    cookie_header: dict[str, str],
) -> dict[str, Any]:
    deadline = time.monotonic() + 20.0
    queue: dict[str, Any] = {}
    while time.monotonic() < deadline:
        queue = server.request(
            "GET",
            "/controlled/s01/api/queries/queue",
            headers={**headers("reviewer"), **cookie_header},
            use_session=False,
        ).json()
        item = next(
            (
                candidate
                for candidate in queue["items"]
                if candidate["application_id"] == application_id
            ),
            None,
        )
        if item is not None:
            break
        time.sleep(0.05)
    assert item is not None, "queue fact never became readable"
    timeline = server.request(
        "GET",
        f"/controlled/s01/api/queries/applications/{application_id}/audit-timeline",
        headers=auditor_auth_headers(),
        use_session=False,
    )
    assert timeline.status == 200, timeline.text
    return {
        "revision": {
            key: item[key]
            for key in ("lifecycle_revision", "evidence_revision", "projection_watermark")
        },
        "route": item["route"],
        "fact": item,
        "history": timeline.json(),
    }


def test_rollback_preserves_accepted_facts_and_route_history_across_three_restarts(
    tmp_path: Path,
) -> None:
    """Replace only the static React artifact across three uvicorn starts
    sharing one SQLite authority: accepted backend facts, revisions, current
    route, and history must stay readable and equal.

    Stage 2 intentionally expects 503 for the partial (missing-asset) build
    --- the fixed base falls back to the legacy template (200), so this
    assertion is RED until Lane A's shell contract lands.  The fact
    preservation assertions run first and already pass on the fixed base.
    """
    import task4_consistency.web.app as web

    react_source = Path(web.__file__).resolve().parent / "static" / "react"
    assert (react_source / "index.html").is_file()
    react_original = tmp_path / "react-original"
    react_live = tmp_path / "react-live"
    shutil.copytree(react_source, react_original)
    shutil.copytree(react_source, react_live)
    state_path = tmp_path / "rollback.sqlite3"
    env = _create_t01_app_environment(state_path, "verified", str(react_live))

    # --- Stage 1: complete build, establish an accepted backend fact.
    with UvicornLoopback(
        env,
        app_target=T10_APP_FACTORY,
        app_factory=True,
    ) as server:
        accepted = submit(server, "rollback-t10-fact").json()
        application_id = str(accepted["application_id"])
        processed = server.request(
            "POST",
            "/controlled/s01/api/_test/commands/process",
            body={"now": 0},
            use_session=False,
        )
        assert processed.status == 200, processed.text
        assert processed.json()["status"] == "complete", processed.text
        projected = server.request(
            "POST",
            "/controlled/s01/api/_test/commands/project",
            body={},
            use_session=False,
        )
        assert projected.status == 200, projected.text
        assert projected.json()["updated"] == 1, projected.text
        session_cookie = {"Cookie": server._session_cookie}  # type: ignore[attr-defined]
        assert "s01_session=" in session_cookie["Cookie"]
        stage1 = _capture_s01_facts(server, application_id, session_cookie)
        shell = server.request(
            "GET",
            "/controlled/s01/react",
            headers=demo_auth_headers(),
            use_session=False,
        )
        assert shell.status == 200, shell.text
        stage1_assets = sorted(set(re.findall(_REACT_JS_ASSET, shell.text)))
        assert stage1_assets, "complete build must reference hashed JS assets"

    # --- Stage 2: partial build (references a missing asset) + restart.
    partial_html = re.sub(
        _REACT_JS_ASSET,
        "/static/react/assets/index-MISSING.js",
        (react_live / "index.html").read_text(encoding="utf-8"),
    )
    assert "index-MISSING.js" in partial_html
    (react_live / "index.html").write_text(partial_html, encoding="utf-8")
    with UvicornLoopback(
        env,
        app_target=T10_APP_FACTORY,
        app_factory=True,
    ) as server:
        stage2 = _capture_s01_facts(server, application_id, session_cookie)
        assert stage2 == stage1, "facts must survive the partial-build restart"
        canonical = server.request(
            "GET",
            "/controlled/s01",
            headers=demo_auth_headers(),
            use_session=False,
        )
        assert canonical.status == 503, canonical.text
        assert canonical.json() == {
            "detail": {
                "error": "S01_REACT_UNAVAILABLE",
                "message": "Controlled S01 React shell is not built",
            }
        }
        assert canonical.headers["cache-control"] == "no-store"
        react_partial = server.request(
            "GET",
            "/controlled/s01/react",
            headers=demo_auth_headers(),
            use_session=False,
        )
        # A partial build must be an explicit 503 on the canonical route and
        # the alias alike (Issue #54 cutover; the fixed base served the
        # legacy template with 200 and failed this assertion).
        assert react_partial.status == 503, (
            "contract: partial build must return 503 S01_REACT_UNAVAILABLE"
        )
        assert react_partial.json() == {
            "detail": {
                "error": "S01_REACT_UNAVAILABLE",
                "message": "Controlled S01 React shell is not built",
            }
        }
        assert react_partial.headers["cache-control"] == "no-store"

    # --- Stage 3: restore the prior static artifact + restart.
    shutil.rmtree(react_live)
    shutil.copytree(react_original, react_live)
    with UvicornLoopback(
        env,
        app_target=T10_APP_FACTORY,
        app_factory=True,
    ) as server:
        stage3 = _capture_s01_facts(server, application_id, session_cookie)
        assert stage3 == stage1, "facts must survive the restored-artifact restart"
        shell = server.request(
            "GET",
            "/controlled/s01/react",
            headers=demo_auth_headers(),
            use_session=False,
        )
        assert shell.status == 200, shell.text
        assert shell.headers["cache-control"] == "no-store"
        for asset in stage1_assets:
            asset_response = server.request("GET", asset, use_session=False)
            assert asset_response.status == 200, asset
            assert "immutable" in asset_response.headers.get("cache-control", "")
