"""T10 release contracts (Issue #44): legacy caller freeze, installed
package provenance/content/cache/security, and three-stage rollback with
fact preservation.

Group 1 -- legacy mutation caller freeze (zero new callers): a test-local
scanner over the production source tree (task4_consistency/**/*.py,
task4_consistency/web/static/**/*.js, packaged HTML templates under
task4_consistency/web/templates/** (they carry inline <script> blocks),
frontend/src/**/*.{ts,tsx,js,jsx}, excluding the machine-generated
task4_consistency/web/static/react/** and frontend/src/generated/**) plus
an exact allowlist of the current production owners.  The freeze covers
per-(path, token) occurrence counts, not a presence bit: a second legacy
mutation call inside an already-allowed file, or an inline legacy caller
inside a packaged template, is a new occurrence and fails the gate.  The
gate lives inside this test file, not in production code.

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
fact -> partial build (missing hashed asset) -> explicit 503 + legacy URL
-> restored build -> shell and hashed assets 200 again, with server-returned
authority revision, fact DTO, current route, and history all unchanged.
Integration dependency: Lane A must make a partial build return a stable
``503 S01_REACT_UNAVAILABLE`` (the fixed base falls back to the legacy
template with 200, so the stage-2 503 assertion is intentionally RED until
Lane A lands); the fact-preservation assertions run before that assertion
and already pass on the fixed base.
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

# --- Group 1: legacy mutation caller freeze ---------------------------------

# Scan roots and their exclusions.  frontend/src/generated/** holds only
# OpenAPI type declarations (no runtime call behavior) and
# task4_consistency/web/static/react/** is the machine-generated build.
# Packaged HTML templates are a scan surface: they carry inline
# <script> blocks (e.g. s01.html / s02.html) that can execute legacy
# mutation calls without a separate JS file.
_SCAN_SPECS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("task4_consistency", (".py",), ("task4_consistency/web/static/react",)),
    ("task4_consistency/web/static", (".js",), ("task4_consistency/web/static/react",)),
    ("task4_consistency/web/templates", (".html",), ()),
    ("frontend/src", (".ts", ".tsx", ".js", ".jsx"), ("frontend/src/generated",)),
)

# Token vocabulary.  Endpoint tokens cover /api/rules, /api/rules/reset,
# /api/kb and the dynamic KB delete path (two segments after /api/kb/);
# direct-store tokens cover add_alias, remove_alias, RUNTIME_RULES.unlink
# and the replace of RUNTIME_RULES (both Path.replace and os.replace).
_TOKEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("api_rules", re.compile(re.escape("/api/rules") + r"(?![\w/])")),
    ("api_rules_reset", re.compile(re.escape("/api/rules/reset"))),
    ("api_kb", re.compile(re.escape("/api/kb") + r"(?![\w/])")),
    (
        "kb_delete_path",
        re.compile(re.escape("/api/kb/") + r"[^\"'\s]+/[^\"'\s]+"),
    ),
    ("add_alias", re.compile(re.escape("add_alias"))),
    ("remove_alias", re.compile(re.escape("remove_alias"))),
    ("runtime_rules_unlink", re.compile(re.escape("RUNTIME_RULES") + r"\.unlink")),
    (
        "runtime_rules_replace",
        re.compile(
            r"RUNTIME_RULES\.replace|os\.replace\([^)]*RUNTIME_RULES\)"
        ),
    ),
)

# Exact current production-owner set with occurrence counts: HTTP authority
# definitions in app.py, the retained runtime caller static/app.js, and the
# KB mutator definitions/wrappers in kb/store.py + kb/__init__.py.  Counts
# are per-(path, token) matches of the patterns above, verified against the
# sources at the T10 freeze commit; any count change (or new path/token)
# means a new legacy mutation caller.
_ALLOWLIST: dict[tuple[str, str], int] = {
    ("task4_consistency/web/app.py", "api_rules"): 3,
    ("task4_consistency/web/app.py", "api_rules_reset"): 1,
    ("task4_consistency/web/app.py", "api_kb"): 3,
    ("task4_consistency/web/app.py", "kb_delete_path"): 1,
    ("task4_consistency/web/app.py", "add_alias"): 1,
    ("task4_consistency/web/app.py", "remove_alias"): 1,
    ("task4_consistency/web/app.py", "runtime_rules_unlink"): 2,
    ("task4_consistency/web/app.py", "runtime_rules_replace"): 2,
    ("task4_consistency/web/static/app.js", "api_rules"): 3,
    ("task4_consistency/web/static/app.js", "api_rules_reset"): 1,
    ("task4_consistency/web/static/app.js", "api_kb"): 3,
    ("task4_consistency/web/static/app.js", "kb_delete_path"): 1,
    ("task4_consistency/kb/store.py", "add_alias"): 1,
    ("task4_consistency/kb/store.py", "remove_alias"): 1,
    ("task4_consistency/kb/__init__.py", "add_alias"): 5,
    ("task4_consistency/kb/__init__.py", "remove_alias"): 4,
}


def _scanned_source_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for subdir, suffixes, exclusions in _SCAN_SPECS:
        base = root / subdir
        excluded = {root / relative for relative in exclusions}
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in suffixes:
                continue
            if any(path.is_relative_to(excluded_dir) for excluded_dir in excluded):
                continue
            files.append(path)
    files.sort(key=lambda path: path.relative_to(root).as_posix())
    return files


def _scan_source_tree(root: Path) -> dict[tuple[str, str], int]:
    """Occurrence-counted legacy mutation callers over the scanned tree.

    Each (relative_path, token) maps to the number of pattern matches in
    that file, so a second call inside an already-allowed file (or an
    inline caller inside a packaged template) changes the frozen counts.
    """
    findings: dict[tuple[str, str], int] = {}
    for path in _scanned_source_files(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        relative = path.relative_to(root).as_posix()
        for token, pattern in _TOKEN_PATTERNS:
            count = len(pattern.findall(text))
            if count:
                findings[(relative, token)] = count
    return findings


def test_legacy_mutation_callers_are_frozen_to_exact_production_owners() -> None:
    """Current production source must equal the allowlist exactly.

    Any new production file, React caller, direct store caller, extra
    occurrence inside an allowed file, or inline template caller changes
    the scanned counts and fails this gate.
    """
    scanned = _scan_source_tree(ROOT)
    assert scanned == _ALLOWLIST, (
        "legacy rule/KB mutation caller set changed; expected exact allowlist "
        "but differences "
        + repr(
            {
                key: (scanned.get(key), _ALLOWLIST.get(key))
                for key in set(scanned) | set(_ALLOWLIST)
                if scanned.get(key) != _ALLOWLIST.get(key)
            }
        )
    )


def test_legacy_mutation_callers_gate_detects_injected_new_react_caller(
    tmp_path: Path,
) -> None:
    """A new React caller in a real scanned tree makes the gate fail.

    The injected caller must physically exist in the scanned tree (no
    mocking): a minimal production tree is copied and a new
    ``frontend/src/evil.tsx`` with ``fetch("/api/rules")`` is added.
    """
    tree = tmp_path / "injected-src"
    for relative in (
        "task4_consistency/web/app.py",
        "task4_consistency/web/static/app.js",
        "task4_consistency/kb/store.py",
        "task4_consistency/kb/__init__.py",
    ):
        target = tree / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    evil = tree / "frontend" / "src" / "evil.tsx"
    evil.parent.mkdir(parents=True, exist_ok=True)
    evil.write_text(
        "export function callLegacyRules(): void {\n"
        '  void fetch("/api/rules");\n'
        "}\n",
        encoding="utf-8",
    )

    scanned = _scan_source_tree(tree)
    assert scanned[("frontend/src/evil.tsx", "api_rules")] == 1
    assert scanned != _ALLOWLIST, "gate must fail on an injected new caller"


def test_legacy_mutation_callers_gate_detects_second_call_in_allowed_js(
    tmp_path: Path,
) -> None:
    """A second legacy mutation call inside an already-allowed file is a
    new occurrence and must fail the gate (presence-only scanning missed
    this: one (path, token) bit per file collapsed both calls)."""
    tree = tmp_path / "injected-extra-call"
    for relative in (
        "task4_consistency/web/app.py",
        "task4_consistency/web/static/app.js",
        "task4_consistency/kb/store.py",
        "task4_consistency/kb/__init__.py",
    ):
        target = tree / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    extra = tree / "task4_consistency" / "web" / "static" / "app.js"
    extra.write_text(
        extra.read_text(encoding="utf-8") + '\nvoid fetch("/api/rules");\n',
        encoding="utf-8",
    )

    scanned = _scan_source_tree(tree)
    assert scanned[("task4_consistency/web/static/app.js", "api_rules")] == (
        _ALLOWLIST[("task4_consistency/web/static/app.js", "api_rules")] + 1
    )
    assert scanned != _ALLOWLIST, "gate must fail on an extra allowed-file caller"


def test_legacy_mutation_callers_gate_detects_inline_template_caller(
    tmp_path: Path,
) -> None:
    """An inline legacy mutation call inside a packaged production HTML
    template must be reported with the template path and token.

    Templates carry executable inline <script> blocks, so they are part of
    the scanned surface even when the baseline count is zero."""
    tree = tmp_path / "injected-template-caller"
    for relative in (
        "task4_consistency/web/app.py",
        "task4_consistency/web/static/app.js",
        "task4_consistency/kb/store.py",
        "task4_consistency/kb/__init__.py",
        "task4_consistency/web/templates/s01.html",
    ):
        target = tree / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    template = tree / "task4_consistency" / "web" / "templates" / "s01.html"
    template.write_text(
        template.read_text(encoding="utf-8")
        + '\n<script>void fetch("/api/rules");</script>\n',
        encoding="utf-8",
    )

    scanned = _scan_source_tree(tree)
    assert scanned[("task4_consistency/web/templates/s01.html", "api_rules")] == 1
    assert scanned != _ALLOWLIST, "gate must fail on an injected template caller"


# --- Group 2: installed package provenance / content / cache / security -----

_INSTALLED_ROOT_ENV = os.environ.get("TASK4_T10_INSTALLED_ROOT", "").strip()

installed_artifact = pytest.mark.skipif(
    not _INSTALLED_ROOT_ENV,
    reason=(
        "TASK4_T10_INSTALLED_ROOT is unset; installed-package assertions "
        "run inside the release harness (Lane C) against the wheel target"
    ),
)


def _installed_root() -> Path:
    return Path(os.environ["TASK4_T10_INSTALLED_ROOT"]).resolve()


def _imported_web_module() -> Any:
    import task4_consistency.web.app

    return task4_consistency.web.app


@installed_artifact
def test_installed_artifact_provenance_and_import_location() -> None:
    """The imported package must come from the installed root, and the
    installed root must contain the React shell and legacy assets."""
    installed_root = _installed_root()
    assert installed_root.is_dir()
    module_file = Path(_imported_web_module().__file__).resolve()
    assert module_file.is_relative_to(installed_root), (
        f"task4_consistency imported from {module_file}, outside {installed_root}"
    )
    package_dir = module_file.parents[1]
    assert package_dir == installed_root / "task4_consistency"
    react_index = package_dir / "web" / "static" / "react" / "index.html"
    assert react_index.is_file(), "installed wheel must carry the React shell"
    assert (package_dir / "web" / "static" / "app.js").is_file()
    assert list((package_dir / "web" / "templates").glob("*.html"))


@installed_artifact
def test_installed_artifact_closes_react_references_without_sourcemaps() -> None:
    """Every asset referenced by the installed index.html exists inside the
    installed wheel (closed reference), at least one hashed JS and one CSS
    entry are present, and no .map sourcemap ships."""
    package_dir = Path(_imported_web_module().__file__).resolve().parents[1]
    react_dir = package_dir / "web" / "static" / "react"
    index_html = (react_dir / "index.html").read_text(encoding="utf-8")
    references = re.findall(r'(?:src|href)="(/static/react/[^"]+)"', index_html)
    assert references, "installed index.html must reference local assets"
    for reference in references:
        relative = reference.removeprefix("/static/react/")
        assert (react_dir / relative).is_file(), (
            f"installed wheel must close reference {reference}"
        )
    assert any(reference.endswith(".js") for reference in references)
    assert any(reference.endswith(".css") for reference in references)
    sourcemaps = [path for path in react_dir.rglob("*") if path.suffix == ".map"]
    assert sourcemaps == [], "production build must not ship .map sourcemaps"


_REACT_JS_ASSET = r"/static/react/assets/[A-Za-z0-9._/-]+\.js"


@installed_artifact
def test_installed_artifact_http_seam_serves_complete_shell_and_immutable_assets(
    tmp_path: Path,
) -> None:
    """HTTP seam on the installed module: complete shell 200 + no-store,
    hashed assets immutable for one year, direct /static/react/index.html
    no-store."""
    state_path = tmp_path / "installed-served.sqlite3"
    with UvicornLoopback(
        _environment(state_path, "verified"),
        app_target=T10_APP_FACTORY,
        app_factory=True,
    ) as server:
        shell = server.request(
            "GET",
            "/controlled/s01/react",
            headers=demo_auth_headers(),
            use_session=False,
        )
        assert shell.status == 200, shell.text
        assert shell.headers["cache-control"] == "no-store"
        assert shell.headers["pragma"] == "no-cache"
        assets = sorted(set(re.findall(_REACT_JS_ASSET, shell.text)))
        assert assets, "served shell must reference hashed JS assets"
        for asset in assets:
            asset_response = server.request("GET", asset, use_session=False)
            assert asset_response.status == 200, asset
            cache_control = asset_response.headers.get("cache-control", "")
            assert "immutable" in cache_control, asset
            assert "max-age=31536000" in cache_control, asset
        direct_index = server.request(
            "GET",
            "/static/react/index.html",
            use_session=False,
        )
        assert direct_index.status == 200
        assert direct_index.headers["cache-control"] == "no-store"


@installed_artifact
def test_installed_artifact_http_seam_missing_build_returns_explicit_503(
    tmp_path: Path,
) -> None:
    """A missing React build on the installed module is an explicit
    minimized 503 with no-store; the legacy URL stays available."""
    state_path = tmp_path / "installed-missing.sqlite3"
    missing_build = tmp_path / "no-react-build"
    missing_build.mkdir()
    env = _create_t01_app_environment(state_path, "verified", str(missing_build))
    with UvicornLoopback(
        env,
        app_target=T10_APP_FACTORY,
        app_factory=True,
    ) as server:
        unavailable = server.request(
            "GET",
            "/controlled/s01/react",
            use_session=False,
        )
        assert unavailable.status == 503
        assert unavailable.json() == {
            "detail": {
                "error": "S01_REACT_UNAVAILABLE",
                "message": "Controlled S01 React shell is not built",
            }
        }
        assert unavailable.headers["cache-control"] == "no-store"
        assert str(missing_build) not in unavailable.text
        legacy = server.request(
            "GET",
            "/controlled/s01",
            headers=demo_auth_headers(),
            use_session=False,
        )
        assert legacy.status == 200, legacy.text
        assert legacy.headers["cache-control"] == "no-store"


# --- Group 3: three-stage rollback with fact preservation --------------------

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
        legacy = server.request(
            "GET",
            "/controlled/s01",
            headers=demo_auth_headers(),
            use_session=False,
        )
        assert legacy.status == 200, legacy.text
        react_partial = server.request(
            "GET",
            "/controlled/s01/react",
            headers=demo_auth_headers(),
            use_session=False,
        )
        # Lane A dependency: a partial build must be an explicit 503; the
        # fixed base serves the legacy template (200) and fails this assert.
        assert react_partial.status == 503, (
            "Lane A contract: partial build must return 503 S01_REACT_UNAVAILABLE"
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
