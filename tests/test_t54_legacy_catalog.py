"""T54 contracted legacy entry catalog public seam (Issue #54) +
post-contraction reintroduction guard (Issue #45).

The catalog is the single code-owned authority for the retirement inventory
of the contracted legacy HTTP surfaces.  Issue #45 physically deleted the
five cataloged web files and retired the five direct mutation handlers, so
the current-tree expectation for every entry is ABSENCE.  This module tests
the public seam: the exact stable ID set (unchanged), the declared
retirement metadata, the completeness gate (no reintroduced retired file,
no reappeared handler, no uncataloged legacy route), and the public source
scan (canonical edges vs zero rollback occurrences) over a physically built
temporary tree with injected reintroductions.
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

# Retired product files (Issue #45 deletion list).
APP_PY = "task4_consistency/web/app.py"
APP_JS = "task4_consistency/web/static/app.js"
S01_HTML = "task4_consistency/web/templates/s01.html"
INDEX_HTML = "task4_consistency/web/templates/index.html"
STYLE_CSS = "task4_consistency/web/static/style.css"


def _entry(entry_id: str):
    return next(entry for entry in CONTRACTED_LEGACY_ENTRIES if entry.id == entry_id)


def _copy_contracted_tree(tree: Path) -> None:
    """The contracted production tree: the app module and the KB store
    (every cataloged web file is absent by design)."""
    for relative in (
        APP_PY,
        "task4_consistency/kb/store.py",
        "task4_consistency/kb/__init__.py",
    ):
        target = tree / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)


def _write_evil_caller(tree: Path, body: str) -> Path:
    evil = tree / "frontend" / "src" / "evil.tsx"
    evil.parent.mkdir(parents=True, exist_ok=True)
    evil.write_text(body, encoding="utf-8")
    return evil


def test_catalog_contains_exact_ten_contracted_legacy_surfaces() -> None:
    """The catalog is the frozen authority: exactly the ten stable IDs from
    the #45 contraction contract, in a stable order, all retired."""
    assert tuple(entry.id for entry in CONTRACTED_LEGACY_ENTRIES) == EXPECTED_IDS
    assert len({entry.id for entry in CONTRACTED_LEGACY_ENTRIES}) == 10
    assert all(entry.retired for entry in CONTRACTED_LEGACY_ENTRIES)


def test_retirement_metadata_is_frozen_per_entry() -> None:
    """Every entry declares the retired physical files it guards and the
    expected route-owner occurrence (0 for the retired mutation handlers,
    1 for the retained canonical React pages and the /static mount)."""
    expected_retired_files = {
        "legacy-page-root": (INDEX_HTML,),
        "legacy-page-controlled-s01": (S01_HTML,),
        "legacy-page-controlled-s02": ("task4_consistency/web/templates/s02.html",),
        "legacy-static-app-js": (APP_JS, INDEX_HTML),
        "legacy-static-style-css": (STYLE_CSS, INDEX_HTML),
        "legacy-mutation-rules-put": (APP_JS,),
        "legacy-mutation-rules-reset-post": (APP_JS,),
        "legacy-mutation-kb-post": (APP_JS,),
        "legacy-mutation-kb-delete": (APP_JS,),
        "legacy-mutation-kb-reload-post": (),
    }
    for entry in CONTRACTED_LEGACY_ENTRIES:
        assert entry.retired_files == expected_retired_files[entry.id], entry.id
        assert entry.route_owner_file == APP_PY, entry.id
        if entry.kind == "mutation":
            assert entry.expected_route_owner_occurrences == 0, entry.id
            assert dict(entry.rollback_occurrences).get(APP_PY, 0) == 0
        else:
            assert entry.expected_route_owner_occurrences == 1, entry.id


def test_contracted_tree_scan_passes_with_zero_presence() -> None:
    """The production tree satisfies the complete post-contraction
    contract: all entries retired, no reintroduced retired file, no
    reappeared handler, zero canonical source edges, zero occurrences and
    no direct-store drift."""
    report = scan_legacy_contract(ROOT)
    assert report.completeness_ok, (
        f"retired_owners_present={[r for r in report.retired_owners_present]} "
        f"route_owner_mismatches={[m for m in report.route_owner_mismatches]} "
        f"uncataloged_routes={[r for r in report.uncataloged_routes]} "
        f"occurrence_mismatches={[m for m in report.occurrence_mismatches]}"
    )
    assert report.zero_canonical_source_edges
    assert report.retired_owners_present == ()
    assert report.route_owner_mismatches == ()
    assert report.occurrence_mismatches == ()
    assert report.direct_store_mismatches == ()
    assert report.ok
    assert "/" in CANONICAL_REACT_ROUTES
    assert "/controlled/s01" in CANONICAL_REACT_ROUTES
    assert "/controlled/s02" in CANONICAL_REACT_ROUTES
    assert all(entry.route_owner_symbol for entry in CONTRACTED_LEGACY_ENTRIES)
    # Retired mutation handlers are absent (observed 0 == expected 0) and
    # the retained canonical React pages / static mount stay (observed 1).
    for entry in CONTRACTED_LEGACY_ENTRIES:
        assert report.entries[entry.id].route_owner_occurrences == (
            entry.expected_route_owner_occurrences
        ), entry.id
    assert all(
        item.route_owner_occurrences == 1
        for item in report.entries.values()
        if item.kind != "mutation"
    )


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


class TestReintroductionAttackMatrix:
    """Reintroducing any retired file, handler, caller or direct store
    mutation must fail the public scan (Issue #45 post-contraction
    guard)."""

    def test_scan_fails_when_retired_page_file_reappears(self, tmp_path: Path) -> None:
        tree = tmp_path / "reintroduced-page-file"
        _copy_contracted_tree(tree)
        (tree / INDEX_HTML).parent.mkdir(parents=True, exist_ok=True)
        (tree / INDEX_HTML).write_text("<html>legacy root</html>\n", encoding="utf-8")
        report = scan_legacy_contract(tree)
        assert not report.completeness_ok
        assert not report.ok
        assert any(
            present.entry_id == "legacy-page-root" and present.owner == INDEX_HTML
            for present in report.retired_owners_present
        ), report.retired_owners_present

    def test_scan_fails_when_retired_static_file_reappears(self, tmp_path: Path) -> None:
        tree = tmp_path / "reintroduced-static-file"
        _copy_contracted_tree(tree)
        (tree / APP_JS).parent.mkdir(parents=True, exist_ok=True)
        (tree / APP_JS).write_text("void 0;\n", encoding="utf-8")
        report = scan_legacy_contract(tree)
        assert not report.completeness_ok
        assert not report.ok
        assert any(
            present.entry_id == "legacy-static-app-js" and present.owner == APP_JS
            for present in report.retired_owners_present
        ), report.retired_owners_present

    def test_scan_fails_when_retired_handler_reappears(self, tmp_path: Path) -> None:
        tree = tmp_path / "reintroduced-handler"
        _copy_contracted_tree(tree)
        app = tree / APP_PY
        app.write_text(
            app.read_text(encoding="utf-8")
            + '\n\n@app.put("/api/rules")\ndef put_rules() -> dict:\n    return {"ok": True}\n',
            encoding="utf-8",
        )
        report = scan_legacy_contract(tree)
        assert not report.ok
        assert any(
            mismatch.entry_id == "legacy-mutation-rules-put"
            and mismatch.owner_symbol == "put_rules"
            and mismatch.observed == 1
            and mismatch.expected == 0
            for mismatch in report.route_owner_mismatches
        ), report.route_owner_mismatches
        assert any(
            mismatch.entry_id == "legacy-mutation-rules-put"
            and mismatch.observed == 1
            and mismatch.expected == 0
            for mismatch in report.occurrence_mismatches
        ), report.occurrence_mismatches

    def test_scan_fails_when_react_caller_reappears(self, tmp_path: Path) -> None:
        tree = tmp_path / "reintroduced-caller"
        _copy_contracted_tree(tree)
        _write_evil_caller(
            tree,
            "export function callLegacyRules(): void {\n"
            '  void fetch("/api/rules", { method: "PUT", body: "{}" });\n'
            "}\n",
        )
        report = scan_legacy_contract(tree)
        assert not report.zero_canonical_source_edges
        assert not report.ok
        assert any(
            edge.entry_id == "legacy-mutation-rules-put"
            and edge.path == "frontend/src/evil.tsx"
            and edge.occurrences == 1
            for edge in report.canonical_source_edges
        ), report.canonical_source_edges

    def test_scan_fails_when_template_read_reappears(self, tmp_path: Path) -> None:
        tree = tmp_path / "reintroduced-template-read"
        _copy_contracted_tree(tree)
        app = tree / APP_PY
        app.write_text(
            app.read_text(encoding="utf-8")
            + "\ndef injected():\n    return S01_TEMPLATE.read_text()\n",
            encoding="utf-8",
        )
        report = scan_legacy_contract(tree)
        assert not report.zero_canonical_source_edges
        assert not report.ok
        assert any(
            edge.entry_id == "legacy-page-controlled-s01" and edge.path == APP_PY
            for edge in report.canonical_source_edges
        ), report.canonical_source_edges

    def test_scan_fails_when_retired_static_reference_reappears(
        self, tmp_path: Path
    ) -> None:
        tree = tmp_path / "reintroduced-static-reference"
        _copy_contracted_tree(tree)
        (tree / INDEX_HTML).parent.mkdir(parents=True, exist_ok=True)
        (tree / INDEX_HTML).write_text(
            '<html><link rel="stylesheet" href="/static/style.css"></html>\n',
            encoding="utf-8",
        )
        report = scan_legacy_contract(tree)
        assert not report.ok
        assert any(
            present.entry_id == "legacy-page-root" and present.owner == INDEX_HTML
            for present in report.retired_owners_present
        ), report.retired_owners_present
        assert any(
            mismatch.entry_id == "legacy-static-style-css"
            and mismatch.path == INDEX_HTML
            and mismatch.observed == 1
            and mismatch.expected == 0
            for mismatch in report.occurrence_mismatches
        ), report.occurrence_mismatches

    def test_scan_fails_when_direct_store_mutation_reappears(self, tmp_path: Path) -> None:
        tree = tmp_path / "reintroduced-direct-store"
        _copy_contracted_tree(tree)
        app = tree / APP_PY
        app.write_text(
            app.read_text(encoding="utf-8")
            + "\ndef injected():\n"
            + '    return get_kb().add_alias("address_aliases", "x", "y")\n',
            encoding="utf-8",
        )
        report = scan_legacy_contract(tree)
        assert not report.ok
        assert any(
            mismatch.path == APP_PY
            and mismatch.token == "add_alias"
            and mismatch.observed == 1
            and mismatch.expected == 0
            for mismatch in report.direct_store_mismatches
        ), report.direct_store_mismatches

    def test_scan_detects_uncataloged_legacy_route_when_reload_entry_removed(
        self, tmp_path: Path
    ) -> None:
        """Completeness fails with the exact method/path and owner symbol
        when a legacy route is reintroduced but no catalog entry claims it."""
        tree = tmp_path / "injected-route-without-entry"
        _copy_contracted_tree(tree)
        app = tree / APP_PY
        app.write_text(
            app.read_text(encoding="utf-8")
            + '\n\n@app.post("/api/kb/reload")\ndef kb_reload() -> dict:\n    return {"ok": True}\n',
            encoding="utf-8",
        )
        entries_without_reload = tuple(
            entry
            for entry in CONTRACTED_LEGACY_ENTRIES
            if entry.id != "legacy-mutation-kb-reload-post"
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
