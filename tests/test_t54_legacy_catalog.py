"""T54 contracted legacy entry catalog public seam (Issue #54).

The catalog is the single code-owned authority for the retirement inventory
of the contracted legacy HTTP surfaces.  This module tests the public seam:
the exact stable ID set, the declared owner/route metadata (including the
previously unscanned ``POST /api/kb/reload`` family), the completeness gate
(missing owners / uncataloged legacy routes), and the public source scan
(canonical edges vs rollback-internal occurrences) over a physically built
temporary tree with injected callers.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from task4_consistency.web.legacy_catalog import (
    CANONICAL_REACT_ROUTES,
    CONTRACTED_LEGACY_ENTRIES,
    match_legacy_surface,
    scan_legacy_contract,
)

ROOT = Path(__file__).resolve().parents[1]

EXPECTED_IDS = (
    "legacy-page-root",
    "legacy-page-controlled-s01",
    "legacy-page-controlled-s02",
    "legacy-static-app-js",
    "legacy-static-style-css",
    "legacy-mutation-rules-put",
    "legacy-mutation-rules-reset-post",
    "legacy-mutation-kb-post",
    "legacy-mutation-kb-delete",
    "legacy-mutation-kb-reload-post",
)

# Declared rollback-owner files shared by the mutation entries (route
# definitions in app.py, frozen call sites in static/app.js).
APP_PY = "task4_consistency/web/app.py"
APP_JS = "task4_consistency/web/static/app.js"
S01_HTML = "task4_consistency/web/templates/s01.html"


def _entry(entry_id: str):
    return next(entry for entry in CONTRACTED_LEGACY_ENTRIES if entry.id == entry_id)


def _copy_production_tree(tree: Path) -> None:
    for relative in (
        APP_PY,
        APP_JS,
        "task4_consistency/kb/store.py",
        "task4_consistency/kb/__init__.py",
        S01_HTML,
        "task4_consistency/web/templates/s02.html",
        "task4_consistency/web/templates/index.html",
        "task4_consistency/web/static/style.css",
    ):
        target = tree / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)


def test_catalog_contains_exact_ten_contracted_legacy_surfaces() -> None:
    """The catalog is the frozen authority: exactly the ten stable IDs from
    the #45 contraction contract, in a stable order."""
    assert tuple(entry.id for entry in CONTRACTED_LEGACY_ENTRIES) == EXPECTED_IDS
    assert len({entry.id for entry in CONTRACTED_LEGACY_ENTRIES}) == 10


def test_reload_entry_declares_route_owner_and_never_canonical_edges() -> None:
    """``POST /api/kb/reload`` is a contracted surface with its FastAPI
    route definition as the declared owner; the fixed base has zero
    canonical source edges for it."""
    reload_entry = _entry("legacy-mutation-kb-reload-post")
    assert reload_entry.method == "POST"
    assert reload_entry.path == "/api/kb/reload"
    assert reload_entry.kind == "mutation"
    assert APP_PY in reload_entry.owners
    report = scan_legacy_contract(ROOT)
    assert report.completeness_ok
    assert report.zero_canonical_source_edges
    assert report.ok
    assert not any(
        edge.entry_id == reload_entry.id for edge in report.canonical_source_edges
    )
    assert match_legacy_surface("POST", "/api/kb/reload") == reload_entry.id


def test_fixed_base_scan_reports_exact_owners_and_zero_canonical_edges() -> None:
    """The production tree at the fixed base satisfies the complete
    retirement contract: every declared owner present, every rollback
    occurrence exactly frozen, no uncataloged legacy route, and zero
    canonical source edges for every entry."""
    report = scan_legacy_contract(ROOT)
    assert report.completeness_ok
    assert report.zero_canonical_source_edges
    assert report.missing_owners == ()
    assert report.uncataloged_routes == ()
    assert report.occurrence_mismatches == ()
    assert report.direct_store_mismatches == ()
    assert report.ok
    # Declared owners are exactly the current production files.
    assert report.entries["legacy-mutation-rules-put"].declared_owners == (
        APP_PY,
        APP_JS,
    )
    assert report.entries["legacy-page-controlled-s01"].declared_owners == (S01_HTML,)
    # The canonical React routes are catalog-driven route inventory.
    assert "/" in CANONICAL_REACT_ROUTES
    assert "/controlled/s01" in CANONICAL_REACT_ROUTES
    assert "/controlled/s02" in CANONICAL_REACT_ROUTES


def test_scan_detects_uncataloged_legacy_route_when_reload_entry_removed(
    tmp_path: Path,
) -> None:
    """Completeness fails with the exact method/path and owner symbol when a
    legacy route remains but no catalog entry claims it."""
    tree = tmp_path / "injected-route-without-entry"
    _copy_production_tree(tree)
    entries_without_reload = tuple(
        entry for entry in CONTRACTED_LEGACY_ENTRIES if entry.id != "legacy-mutation-kb-reload-post"
    )
    report = scan_legacy_contract(tree, entries=entries_without_reload)
    assert not report.completeness_ok
    assert not report.ok
    assert any(
        route.method == "POST"
        and route.path == "/api/kb/reload"
        and route.owner_file == APP_PY
        and route.owner_symbol == "kb_reload"
        for route in report.uncataloged_routes
    ), report.uncataloged_routes


def test_scan_reports_missing_declared_owner(tmp_path: Path) -> None:
    """Renaming or deleting a declared template/static/handler owner fails
    completeness with the entry ID and the missing owner path."""
    tree = tmp_path / "injected-missing-owner"
    _copy_production_tree(tree)
    (tree / S01_HTML).rename(tree / f"{S01_HTML}.bak")
    report = scan_legacy_contract(tree)
    assert not report.completeness_ok
    assert not report.ok
    assert any(
        missing.entry_id == "legacy-page-controlled-s01" and missing.owner == S01_HTML
        for missing in report.missing_owners
    ), report.missing_owners


def test_match_legacy_surface_covers_ten_surfaces_and_stays_closed() -> None:
    """The HTTP matcher resolves every contracted surface (including the
    dynamic KB delete family) and never claims non-legacy or mismatched
    requests."""
    assert match_legacy_surface("GET", "/") == "legacy-page-root"
    assert match_legacy_surface("GET", "/controlled/s01") == "legacy-page-controlled-s01"
    assert match_legacy_surface("GET", "/controlled/s02") == "legacy-page-controlled-s02"
    assert match_legacy_surface("GET", "/static/app.js") == "legacy-static-app-js"
    assert match_legacy_surface("GET", "/static/style.css") == "legacy-static-style-css"
    assert match_legacy_surface("PUT", "/api/rules") == "legacy-mutation-rules-put"
    assert match_legacy_surface("POST", "/api/rules/reset") == "legacy-mutation-rules-reset-post"
    assert match_legacy_surface("POST", "/api/kb") == "legacy-mutation-kb-post"
    assert match_legacy_surface("DELETE", "/api/kb/address_aliases/foo") == (
        "legacy-mutation-kb-delete"
    )
    assert match_legacy_surface("POST", "/api/kb/reload") == (
        "legacy-mutation-kb-reload-post"
    )
    # Closed schema: no query values, no raw arbitrary segments, no
    # non-legacy routes, and method mismatches never match.
    assert match_legacy_surface("GET", "/api/kb/reload") is None
    assert match_legacy_surface("DELETE", "/api/kb/reload") is None
    assert match_legacy_surface("GET", "/api/rules") is None
    assert match_legacy_surface("GET", "/api/health") is None
    assert match_legacy_surface("GET", "/controlled/s01/api/queries/queue") is None
    assert match_legacy_surface("GET", "/static/react/index.html") is None
    assert match_legacy_surface("GET", "/api/kb/one-segment") is None


class TestPublicScanAttackMatrix:
    """Injected production-source callers must be reported through the
    public scan exactly as the frozen contract requires (consolidated from
    the former T10 parser-internal cases)."""

    def test_scan_reports_second_call_inside_allowed_owner(self, tmp_path: Path) -> None:
        tree = tmp_path / "injected-extra-call"
        _copy_production_tree(tree)
        app_js = tree / APP_JS
        app_js.write_text(
            app_js.read_text(encoding="utf-8")
            + '\nvoid fetch("/api/rules", { method: "PUT", body: "{}" });\n',
            encoding="utf-8",
        )
        report = scan_legacy_contract(tree)
        assert not report.ok
        assert any(
            mismatch.entry_id == "legacy-mutation-rules-put"
            and mismatch.path == APP_JS
            and mismatch.observed == 3
            and mismatch.expected == 2
            for mismatch in report.occurrence_mismatches
        ), report.occurrence_mismatches

    def test_scan_normalizes_concatenated_put_caller(self, tmp_path: Path) -> None:
        tree = tmp_path / "injected-concatenated-put"
        _copy_production_tree(tree)
        app_js = tree / APP_JS
        app_js.write_text(
            app_js.read_text(encoding="utf-8")
            + '\nvoid fetch("/api/" + "rules", { method: "PUT", body: "{}" });\n',
            encoding="utf-8",
        )
        report = scan_legacy_contract(tree)
        assert not report.ok
        assert any(
            mismatch.entry_id == "legacy-mutation-rules-put"
            for mismatch in report.occurrence_mismatches
        ), report.occurrence_mismatches

    def test_scan_semantic_method_flip_keeps_raw_token_count(self, tmp_path: Path) -> None:
        tree = tmp_path / "injected-method-change"
        _copy_production_tree(tree)
        app_js = tree / APP_JS
        changed = app_js.read_text(encoding="utf-8").replace(
            'const kb = await api("/api/kb");',
            'const kb = await api("/api/kb", { method: "POST", body: "{}" });',
            1,
        )
        app_js.write_text(changed, encoding="utf-8")
        report = scan_legacy_contract(tree)
        # Raw endpoint text count stays equal; the semantic scan sees the
        # new kb POST occurrence.
        assert report.raw_token_counts[(APP_JS, "api_kb")] == 3
        assert not report.ok
        assert any(
            mismatch.entry_id == "legacy-mutation-kb-post"
            and mismatch.path == APP_JS
            and mismatch.observed == 2
            and mismatch.expected == 1
            for mismatch in report.occurrence_mismatches
        ), report.occurrence_mismatches

    def test_scan_treats_read_only_raw_tokens_as_diagnostics(self, tmp_path: Path) -> None:
        tree = tmp_path / "injected-read-only-token"
        _copy_production_tree(tree)
        app_js = tree / APP_JS
        app_js.write_text(
            app_js.read_text(encoding="utf-8") + '\nvoid fetch("/api/rules");\n',
            encoding="utf-8",
        )
        report = scan_legacy_contract(tree)
        assert report.raw_token_counts[(APP_JS, "api_rules")] == 4
        assert report.zero_canonical_source_edges
        assert report.ok

    def test_scan_reports_inline_template_caller_on_s01_facet(self, tmp_path: Path) -> None:
        tree = tmp_path / "injected-template-caller"
        _copy_production_tree(tree)
        s01 = tree / S01_HTML
        s01.write_text(
            s01.read_text(encoding="utf-8")
            + '\n<script>void fetch("/api/rules", { method: "PUT", body: "{}" });</script>\n',
            encoding="utf-8",
        )
        report = scan_legacy_contract(tree)
        assert not report.ok
        assert any(
            mismatch.entry_id == "legacy-page-controlled-s01"
            and mismatch.path == S01_HTML
            and mismatch.observed == 1
            and mismatch.expected == 0
            for mismatch in report.occurrence_mismatches
        ), report.occurrence_mismatches

    def test_scan_reports_new_react_caller_for_reload(self, tmp_path: Path) -> None:
        tree = tmp_path / "injected-reload-caller"
        _copy_production_tree(tree)
        evil = tree / "frontend" / "src" / "evil.tsx"
        evil.parent.mkdir(parents=True, exist_ok=True)
        evil.write_text(
            "export function callLegacyReload(): void {\n"
            '  void fetch("/api/kb/reload", { method: "POST", body: "{}" });\n'
            "}\n",
            encoding="utf-8",
        )
        report = scan_legacy_contract(tree)
        assert not report.zero_canonical_source_edges
        assert not report.ok
        assert any(
            edge.entry_id == "legacy-mutation-kb-reload-post"
            and edge.path == "frontend/src/evil.tsx"
            and edge.occurrences == 1
            for edge in report.canonical_source_edges
        ), report.canonical_source_edges

    def test_scan_reports_new_react_caller_for_rules(self, tmp_path: Path) -> None:
        tree = tmp_path / "injected-src"
        _copy_production_tree(tree)
        evil = tree / "frontend" / "src" / "evil.tsx"
        evil.parent.mkdir(parents=True, exist_ok=True)
        evil.write_text(
            "export function callLegacyRules(): void {\n"
            '  void fetch("/api/rules", { method: "PUT", body: "{}" });\n'
            "}\n",
            encoding="utf-8",
        )
        report = scan_legacy_contract(tree)
        assert any(
            edge.entry_id == "legacy-mutation-rules-put"
            and edge.path == "frontend/src/evil.tsx"
            for edge in report.canonical_source_edges
        ), report.canonical_source_edges
