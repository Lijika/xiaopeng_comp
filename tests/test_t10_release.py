"""T10 release contracts (Issue #44): legacy caller freeze, installed
package provenance/content/cache/security, and three-stage rollback with
fact preservation.

Group 1 -- legacy mutation caller freeze (zero new callers): test-local
scanners over the production source tree (task4_consistency/**/*.py,
task4_consistency/web/static/**/*.js, packaged HTML templates under
task4_consistency/web/templates/** (they carry inline <script> blocks),
frontend/src/**/*.{ts,tsx,js,jsx}, excluding the machine-generated
task4_consistency/web/static/react/** and frontend/src/generated/**) plus
exact allowlists of the current production owners.  Direct Python store
mutations are frozen by per-(path, token) occurrence counts.  HTTP mutations
are frozen by path, canonical endpoint family, method, and occurrence; raw
HTTP endpoint counts provide diagnostics only.  The gate lives inside this
test file, not in production code.

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
import subprocess
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

_DIRECT_STORE_TOKENS = frozenset(
    {"add_alias", "remove_alias", "runtime_rules_unlink", "runtime_rules_replace"}
)

# Exact current raw-token inventory. HTTP endpoint counts provide diagnostics;
# the direct-store subset remains authoritative for mutations outside the HTTP
# semantic scanner.
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

_DIRECT_STORE_ALLOWLIST = {
    key: count for key, count in _ALLOWLIST.items() if key[1] in _DIRECT_STORE_TOKENS
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


# --- Group 1b: semantic mutation-caller freeze (R2) --------------------------
#
# Raw endpoint token counts cannot bind a call site to its HTTP method, so
# the gate additionally inventories actual mutating call signatures:
# (relative_path, canonical legacy endpoint family, mutating method) ->
# occurrence count.  GET reads are not mutations and are not inventoried;
# POST/PUT/PATCH/DELETE are.  Route definitions (@app.<method> decorators)
# are authority signatures; fetch/api call sites with an explicit method
# field are caller signatures.  Caller URLs are normalized: a statically
# equivalent concatenation ('/api/' + 'rules') equals '/api/rules', and the
# dynamic /api/kb/<seg>/<seg> family is detected by its static prefix.
_MUTATION_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_ROUTE_DEFINITION_RE = re.compile(
    r"@app\.(get|post|put|patch|delete)\(\s*['\"]([^'\"]+)['\"]"
)
_CALL_START_RE = re.compile(r"\b([\w.$]+)\s*\(")
_METHOD_FIELD_RE = re.compile(r"\bmethod\s*:\s*['\"]([A-Za-z]+)['\"]")

_MUTATION_ALLOWLIST: dict[tuple[str, str, str], int] = {
    # app.py route definitions (authority signatures)
    ("task4_consistency/web/app.py", "rules", "PUT"): 1,
    ("task4_consistency/web/app.py", "rules_reset", "POST"): 1,
    ("task4_consistency/web/app.py", "kb", "POST"): 1,
    ("task4_consistency/web/app.py", "kb_delete", "DELETE"): 1,
    # static/app.js call sites (caller signatures)
    ("task4_consistency/web/static/app.js", "rules", "PUT"): 2,
    ("task4_consistency/web/static/app.js", "rules_reset", "POST"): 1,
    ("task4_consistency/web/static/app.js", "kb", "POST"): 1,
    ("task4_consistency/web/static/app.js", "kb_delete", "DELETE"): 1,
}


def _js_unescape(body: str, quote: str) -> str:
    """Minimal JS string escape decoding for legacy endpoint URLs."""
    out: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in {quote, "\\", "/"}:
                out.append(nxt)
                i += 2
                continue
            if nxt == "n":
                out.append("\n")
                i += 2
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _literal_value(segment: str) -> str | None:
    """Static value of one quoted string / backtick template literal with no
    interpolation; None when the segment is not a literal or is dynamic."""
    seg = segment.strip()
    if len(seg) < 2 or seg[0] not in "'\"`":
        return None
    quote = seg[0]
    body = seg[1:-1]
    if quote == "`":
        if "${" in body:
            return None
        return body
    return _js_unescape(body, quote)


def _call_end(text: str, open_index: int) -> int:
    """Index of the ')' that closes the call opened at open_index; -1 when
    unbalanced.  String literals and nested calls are tracked."""
    depth = 0
    in_str: str | None = None
    i = open_index
    while i < len(text):
        ch = text[i]
        if in_str is not None:
            if ch == "\\":
                i += 2
                continue
            if ch == in_str:
                in_str = None
            i += 1
            continue
        if ch in "'\"`":
            in_str = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _first_argument(call_body: str) -> str | None:
    """Static value of the first call argument when it is a string literal
    or a concatenation of string literals ('/api/' + 'rules'); None when it
    is dynamic or not a string."""
    depth = 0
    in_str: str | None = None
    i = 0
    while i < len(call_body):
        ch = call_body[i]
        if in_str is not None:
            if ch == "\\":
                i += 2
                continue
            if ch == in_str:
                in_str = None
            i += 1
            continue
        if ch in "'\"`":
            in_str = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                break
            depth -= 1
        elif ch == "," and depth == 0:
            break
        i += 1
    parts = re.split(r"\s*\+\s*", call_body[:i].strip())
    values: list[str] = []
    for part in parts:
        value = _literal_value(part)
        if value is None:
            return None
        values.append(value)
    return "".join(values)


def _legacy_family(url_value: str | None, raw_arg: str) -> str | None:
    """Canonical legacy endpoint family of a call/route URL argument, or
    None when it targets no legacy family."""
    if url_value is not None:
        if url_value == "/api/rules/reset":
            return "rules_reset"
        if url_value == "/api/rules":
            return "rules"
        if url_value == "/api/kb":
            return "kb"
        if url_value.startswith("/api/kb/") and "/" in url_value[len("/api/kb/") :]:
            return "kb_delete"
        return None
    # Dynamic first argument: only the /api/kb/<seg>/<seg> family is
    # detectable by its static prefix (templates and concatenations).
    raw = raw_arg.strip()
    if (
        raw.startswith('"/api/kb/')
        or raw.startswith("'/api/kb/")
        or raw.startswith("`/api/kb/")
    ):
        return "kb_delete"
    return None


def _scan_mutation_calls(root: Path) -> dict[tuple[str, str, str], int]:
    """Semantic inventory of legacy HTTP mutation callers: each
    (relative_path, canonical endpoint family, mutating method) maps to the
    number of call sites.  Route definitions are counted from their
    @app.<method> decorators; callers are counted when their first argument
    normalizes to a legacy family (literal, static concatenation, or the
    dynamic /api/kb prefix) and they bind an explicit mutating method.
    """
    findings: dict[tuple[str, str, str], int] = {}
    for path in _scanned_source_files(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        relative = path.relative_to(root).as_posix()
        for method, url in _ROUTE_DEFINITION_RE.findall(text):
            family = _legacy_family(url, url)
            if family is None or method.upper() not in _MUTATION_METHODS:
                continue
            key = (relative, family, method.upper())
            findings[key] = findings.get(key, 0) + 1
        for match in _CALL_START_RE.finditer(text):
            end = _call_end(text, match.end() - 1)
            if end < 0:
                continue
            body = text[match.end() : end]
            family = _legacy_family(_first_argument(body), body)
            if family is None:
                continue
            method_match = _METHOD_FIELD_RE.search(body)
            if not method_match:
                continue
            method = method_match.group(1).upper()
            if method not in _MUTATION_METHODS:
                continue
            key = (relative, family, method)
            findings[key] = findings.get(key, 0) + 1
    return findings


def _assert_legacy_mutation_callers(root: Path) -> None:
    scanned = _scan_source_tree(root)
    direct_store = {
        key: count
        for key, count in scanned.items()
        if key[1] in _DIRECT_STORE_TOKENS
    }
    assert direct_store == _DIRECT_STORE_ALLOWLIST, (
        "legacy direct-store mutation callers changed (path, token); "
        "observed/expected differences "
        + repr(
            {
                key: (direct_store.get(key), _DIRECT_STORE_ALLOWLIST.get(key))
                for key in set(direct_store) | set(_DIRECT_STORE_ALLOWLIST)
                if direct_store.get(key) != _DIRECT_STORE_ALLOWLIST.get(key)
            }
        )
    )

    mutations = _scan_mutation_calls(root)
    raw_http_differences = {
        key: (scanned.get(key), _ALLOWLIST.get(key))
        for key in set(scanned) | set(_ALLOWLIST)
        if key[1] not in _DIRECT_STORE_TOKENS
        and scanned.get(key) != _ALLOWLIST.get(key)
    }
    assert mutations == _MUTATION_ALLOWLIST, (
        "legacy mutation caller signatures changed (path, endpoint, method); "
        "observed/expected differences "
        + repr(
            {
                key: (mutations.get(key), _MUTATION_ALLOWLIST.get(key))
                for key in set(mutations) | set(_MUTATION_ALLOWLIST)
                if mutations.get(key) != _MUTATION_ALLOWLIST.get(key)
            }
        )
        + "; supplementary raw HTTP endpoint differences "
        + repr(raw_http_differences)
    )


def test_legacy_mutation_callers_are_frozen_to_exact_production_owners() -> None:
    """Current production source must equal both authoritative inventories."""
    _assert_legacy_mutation_callers(ROOT)


def test_legacy_mutation_callers_gate_detects_method_change_with_same_token_count(
    tmp_path: Path,
) -> None:
    """A GET -> POST change on an existing /api/kb call keeps the raw
    endpoint token count identical but adds a mutating caller signature and
    must fail the gate (endpoint-text equality cannot bind a call to its
    HTTP method)."""
    tree = tmp_path / "injected-method-change"
    for relative in (
        "task4_consistency/web/app.py",
        "task4_consistency/web/static/app.js",
        "task4_consistency/kb/store.py",
        "task4_consistency/kb/__init__.py",
    ):
        target = tree / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    app_js = tree / "task4_consistency" / "web" / "static" / "app.js"
    changed = app_js.read_text(encoding="utf-8").replace(
        'const kb = await api("/api/kb");',
        'const kb = await api("/api/kb", { method: "POST", body: "{}" });',
        1,
    )
    assert 'api("/api/kb", { method: "POST"' in changed
    app_js.write_text(changed, encoding="utf-8")

    scanned = _scan_source_tree(tree)
    mutations = _scan_mutation_calls(tree)
    token_key = ("task4_consistency/web/static/app.js", "api_kb")
    assert scanned[token_key] == _ALLOWLIST[token_key], (
        "probe premise: endpoint token count must be unchanged by the method flip"
    )
    mutation_key = ("task4_consistency/web/static/app.js", "kb", "POST")
    assert mutations[mutation_key] == _MUTATION_ALLOWLIST[mutation_key] + 1
    with pytest.raises(AssertionError, match=r"kb.*POST"):
        _assert_legacy_mutation_callers(tree)


def test_legacy_mutation_callers_gate_detects_concatenated_url_put_caller(
    tmp_path: Path,
) -> None:
    """A reachable PUT caller using a statically equivalent concatenated
    '/api/' + 'rules' URL is a new mutating caller signature and must fail
    the gate even though no literal '/api/rules' token appears."""
    tree = tmp_path / "injected-concatenated-put"
    for relative in (
        "task4_consistency/web/app.py",
        "task4_consistency/web/static/app.js",
        "task4_consistency/kb/store.py",
        "task4_consistency/kb/__init__.py",
    ):
        target = tree / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    app_js = tree / "task4_consistency" / "web" / "static" / "app.js"
    app_js.write_text(
        app_js.read_text(encoding="utf-8")
        + '\nvoid fetch("/api/" + "rules", { method: "PUT", body: "{}" });\n',
        encoding="utf-8",
    )
    syntax = subprocess.run(
        ["node", "--check", str(app_js)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert syntax.returncode == 0, syntax.stderr

    scanned = _scan_source_tree(tree)
    mutations = _scan_mutation_calls(tree)
    token_key = ("task4_consistency/web/static/app.js", "api_rules")
    assert scanned[token_key] == _ALLOWLIST[token_key], (
        "probe premise: concatenated URL must not change the endpoint token count"
    )
    mutation_key = ("task4_consistency/web/static/app.js", "rules", "PUT")
    assert mutations[mutation_key] == _MUTATION_ALLOWLIST[mutation_key] + 1
    with pytest.raises(AssertionError, match=r"rules.*PUT"):
        _assert_legacy_mutation_callers(tree)


def test_legacy_mutation_callers_gate_treats_read_only_raw_tokens_as_diagnostics(
    tmp_path: Path,
) -> None:
    tree = tmp_path / "injected-read-only-token"
    for relative in (
        "task4_consistency/web/app.py",
        "task4_consistency/web/static/app.js",
        "task4_consistency/kb/store.py",
        "task4_consistency/kb/__init__.py",
    ):
        target = tree / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    app_js = tree / "task4_consistency" / "web" / "static" / "app.js"
    app_js.write_text(
        app_js.read_text(encoding="utf-8") + '\nvoid fetch("/api/rules");\n',
        encoding="utf-8",
    )

    scanned = _scan_source_tree(tree)
    token_key = ("task4_consistency/web/static/app.js", "api_rules")
    assert scanned[token_key] == _ALLOWLIST[token_key] + 1
    assert _scan_mutation_calls(tree) == _MUTATION_ALLOWLIST
    _assert_legacy_mutation_callers(tree)


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
        '  void fetch("/api/rules", { method: "PUT", body: "{}" });\n'
        "}\n",
        encoding="utf-8",
    )

    scanned = _scan_source_tree(tree)
    assert scanned[("frontend/src/evil.tsx", "api_rules")] == 1
    mutations = _scan_mutation_calls(tree)
    assert mutations[("frontend/src/evil.tsx", "rules", "PUT")] == 1
    with pytest.raises(AssertionError, match=r"frontend/src/evil\.tsx.*rules.*PUT"):
        _assert_legacy_mutation_callers(tree)


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
        extra.read_text(encoding="utf-8")
        + '\nvoid fetch("/api/rules", { method: "PUT", body: "{}" });\n',
        encoding="utf-8",
    )

    scanned = _scan_source_tree(tree)
    assert scanned[("task4_consistency/web/static/app.js", "api_rules")] == (
        _ALLOWLIST[("task4_consistency/web/static/app.js", "api_rules")] + 1
    )
    mutations = _scan_mutation_calls(tree)
    mutation_key = ("task4_consistency/web/static/app.js", "rules", "PUT")
    assert mutations[mutation_key] == _MUTATION_ALLOWLIST[mutation_key] + 1
    with pytest.raises(AssertionError, match=r"rules.*PUT"):
        _assert_legacy_mutation_callers(tree)


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
        + '\n<script>void fetch("/api/rules", { method: "PUT", body: "{}" });</script>\n',
        encoding="utf-8",
    )

    scanned = _scan_source_tree(tree)
    assert scanned[("task4_consistency/web/templates/s01.html", "api_rules")] == 1
    mutations = _scan_mutation_calls(tree)
    key = ("task4_consistency/web/templates/s01.html", "rules", "PUT")
    assert mutations[key] == 1
    with pytest.raises(AssertionError, match=r"s01\.html.*rules.*PUT"):
        _assert_legacy_mutation_callers(tree)


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
