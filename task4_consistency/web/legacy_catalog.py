"""Contracted Legacy Entry Catalog (Issue #54).

The single code-owned authority for the retirement inventory of the
contracted legacy HTTP surfaces owned by the #45 contraction contract.  It
exists so that FastAPI route checks, the source-caller scanner, runtime
observation, release qualification, and deletion evidence all consume one
catalog instead of test-local copies.

Every entry carries a stable identity plus the metadata needed for source
scanning, HTTP route/static matching, traffic classification, release
evidence, and later physical deletion: the HTTP method and canonical path
pattern, the declared rollback owners (files that are allowed to contain
the surface today), and the frozen occurrence count per owner for the
semantic caller inventory.

The catalog owns retirement-contract metadata only.  Application Lifecycle,
Policy Governance, security audit, authentication, authorization, and
session ownership remain unchanged (ADR-0004 / ADR-0006).  ``#45`` owns the
physical removal of the cataloged files and handlers; this ticket records
zero canonical caller edges and removes their canonical page ownership.

Semantic caller scanning (lifted from the former T10 test-local scanners)
inventories actual mutating call signatures: route definitions are
authority signatures, and ``fetch``/``api`` call sites with an explicit
mutating method are caller signatures.  Caller URLs are normalized: a
statically equivalent concatenation (``'/api/' + 'rules'``) equals
``/api/rules``, and the dynamic ``/api/kb/<seg>/<seg>`` family is detected
by its static prefix.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# --- Catalog entries ---------------------------------------------------------

# Scan roots and their exclusions.  frontend/src/generated/** holds only
# OpenAPI type declarations (no runtime call behavior),
# task4_consistency/web/static/react/** is the machine-generated build, and
# the catalog module itself is the scanner authority (its vocabulary is the
# token names, not callers) and is excluded exactly like the former
# test-local scanner in tests/ was.
_SCAN_SPECS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("task4_consistency", (".py",), (
        "task4_consistency/web/static/react",
        "task4_consistency/web/legacy_catalog.py",
    )),
    ("task4_consistency/web/static", (".js",), ("task4_consistency/web/static/react",)),
    ("task4_consistency/web/templates", (".html",), ()),
    ("frontend/src", (".ts", ".tsx", ".js", ".jsx"), ("frontend/src/generated",)),
)

# Token vocabulary.  Endpoint tokens cover /api/rules, /api/rules/reset,
# /api/kb, /api/kb/reload and the dynamic KB delete path (two segments
# after /api/kb/); direct-store tokens cover add_alias, remove_alias,
# RUNTIME_RULES.unlink and the replace of RUNTIME_RULES (both Path.replace
# and os.replace).
_TOKEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("api_rules", re.compile(re.escape("/api/rules") + r"(?![\w/])")),
    ("api_rules_reset", re.compile(re.escape("/api/rules/reset"))),
    ("api_kb", re.compile(re.escape("/api/kb") + r"(?![\w/])")),
    ("api_kb_reload", re.compile(re.escape("/api/kb/reload"))),
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

# Exact frozen direct-store inventory (authoritative raw mutations outside
# the HTTP semantic scanner): (relative_path, token) -> occurrence count.
_DIRECT_STORE_ALLOWLIST: dict[tuple[str, str], int] = {
    ("task4_consistency/web/app.py", "add_alias"): 1,
    ("task4_consistency/web/app.py", "remove_alias"): 1,
    ("task4_consistency/web/app.py", "runtime_rules_unlink"): 2,
    ("task4_consistency/web/app.py", "runtime_rules_replace"): 2,
    ("task4_consistency/kb/store.py", "add_alias"): 1,
    ("task4_consistency/kb/store.py", "remove_alias"): 1,
    ("task4_consistency/kb/__init__.py", "add_alias"): 5,
    ("task4_consistency/kb/__init__.py", "remove_alias"): 4,
}

_MUTATION_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_ROUTE_DEFINITION_RE = re.compile(
    r"@app\.(get|post|put|patch|delete)\(\s*['\"]([^'\"]+)['\"]"
)
_CALL_START_RE = re.compile(r"\b([\w.$]+)\s*\(")
_METHOD_FIELD_RE = re.compile(r"\bmethod\s*:\s*['\"]([A-Za-z]+)['\"]")


@dataclass(frozen=True)
class LegacySurface:
    """One contracted legacy HTTP surface and its retirement metadata.

    ``path`` is the canonical path pattern (``{section}/{key}`` marks the
    dynamic KB delete family).  ``owners`` are the declared rollback-owner
    files; ``rollback_occurrences`` freezes how many semantic occurrences
    each owner may hold today.  A page owner's expected count of zero
    covers every family: the template's inline JavaScript is a declared
    owner facet and must not gain any mutation call.
    """

    id: str
    kind: str  # "page" | "static" | "mutation"
    method: str
    path: str
    description: str
    owners: tuple[str, ...] = ()
    rollback_occurrences: tuple[tuple[str, int], ...] = ()
    facet: str = ""

    def matches(self, method: str, path: str) -> bool:
        if method.upper() != self.method:
            return False
        if self.path == "/api/kb/{section}/{key}":
            return path.startswith("/api/kb/") and "/" in path[len("/api/kb/") :]
        return path == self.path


CONTRACTED_LEGACY_ENTRIES: tuple[LegacySurface, ...] = (
    LegacySurface(
        id="legacy-page-root",
        kind="page",
        method="GET",
        path="/",
        description="Root legacy template owner",
        owners=("task4_consistency/web/templates/index.html",),
        rollback_occurrences=(("task4_consistency/web/templates/index.html", 0),),
    ),
    LegacySurface(
        id="legacy-page-controlled-s01",
        kind="page",
        method="GET",
        path="/controlled/s01",
        description="S01 template plus inline script owner",
        owners=("task4_consistency/web/templates/s01.html",),
        rollback_occurrences=(("task4_consistency/web/templates/s01.html", 0),),
        facet="template + inline script",
    ),
    LegacySurface(
        id="legacy-page-controlled-s02",
        kind="page",
        method="GET",
        path="/controlled/s02",
        description="S02 template plus inline script owner",
        owners=("task4_consistency/web/templates/s02.html",),
        rollback_occurrences=(("task4_consistency/web/templates/s02.html", 0),),
        facet="template + inline script",
    ),
    LegacySurface(
        id="legacy-static-app-js",
        kind="static",
        method="GET",
        path="/static/app.js",
        description="Legacy demo application script",
        owners=("task4_consistency/web/static/app.js",),
    ),
    LegacySurface(
        id="legacy-static-style-css",
        kind="static",
        method="GET",
        path="/static/style.css",
        description="Legacy demo stylesheet",
        owners=("task4_consistency/web/static/style.css",),
    ),
    LegacySurface(
        id="legacy-mutation-rules-put",
        kind="mutation",
        method="PUT",
        path="/api/rules",
        description="Direct rule package mutation",
        owners=(
            "task4_consistency/web/app.py",
            "task4_consistency/web/static/app.js",
        ),
        rollback_occurrences=(
            ("task4_consistency/web/app.py", 1),
            ("task4_consistency/web/static/app.js", 2),
        ),
    ),
    LegacySurface(
        id="legacy-mutation-rules-reset-post",
        kind="mutation",
        method="POST",
        path="/api/rules/reset",
        description="Direct rule package reset",
        owners=(
            "task4_consistency/web/app.py",
            "task4_consistency/web/static/app.js",
        ),
        rollback_occurrences=(
            ("task4_consistency/web/app.py", 1),
            ("task4_consistency/web/static/app.js", 1),
        ),
    ),
    LegacySurface(
        id="legacy-mutation-kb-post",
        kind="mutation",
        method="POST",
        path="/api/kb",
        description="Direct KB alias mutation",
        owners=(
            "task4_consistency/web/app.py",
            "task4_consistency/web/static/app.js",
        ),
        rollback_occurrences=(
            ("task4_consistency/web/app.py", 1),
            ("task4_consistency/web/static/app.js", 1),
        ),
    ),
    LegacySurface(
        id="legacy-mutation-kb-delete",
        kind="mutation",
        method="DELETE",
        path="/api/kb/{section}/{key}",
        description="Direct KB alias deletion",
        owners=(
            "task4_consistency/web/app.py",
            "task4_consistency/web/static/app.js",
        ),
        rollback_occurrences=(
            ("task4_consistency/web/app.py", 1),
            ("task4_consistency/web/static/app.js", 1),
        ),
    ),
    LegacySurface(
        id="legacy-mutation-kb-reload-post",
        kind="mutation",
        method="POST",
        path="/api/kb/reload",
        description="Direct KB singleton reload (process state mutation)",
        owners=("task4_consistency/web/app.py",),
        rollback_occurrences=(("task4_consistency/web/app.py", 1),),
    ),
)

_ENTRY_BY_ID = {entry.id: entry for entry in CONTRACTED_LEGACY_ENTRIES}

# The #54 canonical route cutover: these canonical routes serve the
# qualified React build; their catalog entries become rollback-only owners.
CANONICAL_REACT_ROUTES: tuple[str, ...] = ("/", "/controlled/s01", "/controlled/s02")

# Every current-artifact route that resolves to the qualified React shell:
# the canonical routes plus their /react aliases.  Observation suppresses
# catalog matches for these paths on the current artifact (the request
# belongs to the React owner); under ``rollback-probe`` the prior artifact
# still resolves them to their legacy owners, so suppression never applies
# there.
REACT_OWNED_PATHS: tuple[str, ...] = (
    "/",
    "/demo/react",
    "/controlled/s01",
    "/controlled/s01/react",
    "/controlled/s02",
    "/controlled/s02/react",
    "/controlled/s05/react",
    "/controlled/s08/react",
    "/controlled/s09/react",
)


def match_legacy_surface(method: str, path: str) -> str | None:
    """The stable catalog entry ID for an HTTP request, or None when the
    request resolves to no contracted legacy surface.

    The path is matched without query values or raw arbitrary segments; the
    dynamic KB delete family matches by its static prefix only, so no
    concrete section/key text ever enters a match result.
    """
    clean_path = path.split("?", 1)[0].split("#", 1)[0]
    for entry in CONTRACTED_LEGACY_ENTRIES:
        if entry.matches(method, clean_path):
            return entry.id
    return None


# --- Semantic caller scanner (public) ---------------------------------------


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
        if url_value == "/api/kb/reload":
            return "kb_reload"
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
    """Occurrence-counted legacy tokens over the scanned tree.  HTTP
    endpoint counts are diagnostics; the direct-store subset is
    authoritative for mutations outside the semantic scanner."""
    findings: dict[tuple[str, str], int] = {}
    for path in _scanned_source_files(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        relative = path.relative_to(root).as_posix()
        for token, pattern in _TOKEN_PATTERNS:
            count = len(pattern.findall(text))
            if count:
                findings[(relative, token)] = count
    return findings


def _scan_mutation_edges(
    root: Path,
) -> tuple[
    dict[tuple[str, str, str], int],
    dict[tuple[str, str, str], int],
    list[tuple[str, str, str, str]],
]:
    """Semantic inventory of legacy HTTP mutation callers split by origin.

    Returns ``(route_definitions, call_sites, route_details)``: route
    definitions are counted from ``@app.<method>`` decorators (authority
    signatures, with each concrete (owner_file, method, url, symbol) kept
    in ``route_details``); callers are counted when their first argument
    normalizes to a legacy family (literal, static concatenation, or the
    dynamic /api/kb prefix) and they bind an explicit mutating method.
    """
    route_definitions: dict[tuple[str, str, str], int] = {}
    route_details: list[tuple[str, str, str, str]] = []
    call_sites: dict[tuple[str, str, str], int] = {}
    for path in _scanned_source_files(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        relative = path.relative_to(root).as_posix()
        for match in _ROUTE_DEFINITION_RE.finditer(text):
            method = match.group(1).upper()
            url = match.group(2)
            family = _legacy_family(url, url)
            if family is None or method not in _MUTATION_METHODS:
                continue
            key = (relative, family, method)
            route_definitions[key] = route_definitions.get(key, 0) + 1
            route_details.append(
                (relative, method, url, _owner_symbol(text, match.end()))
            )
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
            call_sites[key] = call_sites.get(key, 0) + 1
    return route_definitions, call_sites, route_details


# --- Public report -----------------------------------------------------------


@dataclass(frozen=True)
class CanonicalEdge:
    """One source occurrence of a legacy mutation family outside every
    declared rollback owner of its catalog entry."""

    entry_id: str
    path: str
    family: str
    method: str
    occurrences: int


@dataclass(frozen=True)
class OccurrenceMismatch:
    """A declared rollback owner holds a different occurrence count than
    the frozen contract (including a page inline-script facet gaining any
    mutation call)."""

    entry_id: str
    path: str
    family: str
    method: str
    observed: int
    expected: int


@dataclass(frozen=True)
class MissingOwner:
    entry_id: str
    owner: str


@dataclass(frozen=True)
class UncatalogedRoute:
    """A legacy mutation route definition that no catalog entry claims."""

    method: str
    path: str
    owner_file: str
    owner_symbol: str


@dataclass(frozen=True)
class DirectStoreMismatch:
    path: str
    token: str
    observed: int
    expected: int


@dataclass(frozen=True)
class EntryReport:
    id: str
    kind: str
    method: str
    path: str
    description: str
    declared_owners: tuple[str, ...]
    rollback_occurrences: tuple[dict[str, object], ...]
    canonical_edge_occurrences: int
    rollback_edge_occurrences: int


@dataclass(frozen=True)
class LegacyContractReport:
    """The complete public scan result over one source tree.

    ``ok`` means the full retirement contract holds: completeness
    (declared owners present, every legacy route cataloged, frozen
    rollback occurrences) plus zero canonical source edges and zero
    direct-store drift.
    """

    entries: dict[str, EntryReport] = field(default_factory=dict)
    canonical_source_edges: tuple[CanonicalEdge, ...] = ()
    occurrence_mismatches: tuple[OccurrenceMismatch, ...] = ()
    missing_owners: tuple[MissingOwner, ...] = ()
    uncataloged_routes: tuple[UncatalogedRoute, ...] = ()
    direct_store_mismatches: tuple[DirectStoreMismatch, ...] = ()
    raw_token_counts: dict[tuple[str, str], int] = field(default_factory=dict)

    @property
    def completeness_ok(self) -> bool:
        return (
            not self.missing_owners
            and not self.uncataloged_routes
            and not self.occurrence_mismatches
        )

    @property
    def zero_canonical_source_edges(self) -> bool:
        return not self.canonical_source_edges

    @property
    def ok(self) -> bool:
        return (
            self.completeness_ok
            and self.zero_canonical_source_edges
            and not self.direct_store_mismatches
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "entries": [
                {
                    "id": report.id,
                    "kind": report.kind,
                    "method": report.method,
                    "path": report.path,
                    "description": report.description,
                    "declared_owners": list(report.declared_owners),
                    "rollback_occurrences": [
                        dict(item) for item in report.rollback_occurrences
                    ],
                    "canonical_edge_occurrences": report.canonical_edge_occurrences,
                    "rollback_edge_occurrences": report.rollback_edge_occurrences,
                }
                for report in self.entries.values()
            ],
            "canonical_source_edges": [
                {
                    "entry_id": edge.entry_id,
                    "path": edge.path,
                    "family": edge.family,
                    "method": edge.method,
                    "occurrences": edge.occurrences,
                }
                for edge in self.canonical_source_edges
            ],
            "occurrence_mismatches": [
                {
                    "entry_id": mismatch.entry_id,
                    "path": mismatch.path,
                    "family": mismatch.family,
                    "method": mismatch.method,
                    "observed": mismatch.observed,
                    "expected": mismatch.expected,
                }
                for mismatch in self.occurrence_mismatches
            ],
            "missing_owners": [
                {"entry_id": missing.entry_id, "owner": missing.owner}
                for missing in self.missing_owners
            ],
            "uncataloged_routes": [
                {
                    "method": route.method,
                    "path": route.path,
                    "owner_file": route.owner_file,
                    "owner_symbol": route.owner_symbol,
                }
                for route in self.uncataloged_routes
            ],
            "direct_store_mismatches": [
                {
                    "path": mismatch.path,
                    "token": mismatch.token,
                    "observed": mismatch.observed,
                    "expected": mismatch.expected,
                }
                for mismatch in self.direct_store_mismatches
            ],
            "raw_token_counts": [
                {"path": path, "token": token, "occurrences": count}
                for (path, token), count in sorted(self.raw_token_counts.items())
            ],
            "completeness_ok": self.completeness_ok,
            "zero_canonical_source_edges": self.zero_canonical_source_edges,
            "ok": self.ok,
        }


_DEF_NAME_RE = re.compile(r"\bdef\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def _owner_symbol(text: str, search_start: int) -> str:
    match = _DEF_NAME_RE.search(text, search_start)
    return match.group(1) if match else "<unknown>"


def _entry_family_path(entry: LegacySurface) -> str:
    """The semantic family of a mutation entry."""
    if entry.path == "/api/kb/{section}/{key}":
        return "kb_delete"
    if entry.path == "/api/kb/reload":
        return "kb_reload"
    if entry.path == "/api/rules/reset":
        return "rules_reset"
    if entry.path == "/api/rules":
        return "rules"
    if entry.path == "/api/kb":
        return "kb"
    raise KeyError(entry.path)


def _family_entries(entries: tuple[LegacySurface, ...]) -> dict[str, LegacySurface]:
    """Mutation entries keyed by their semantic family."""
    return {
        _entry_family_path(entry): entry
        for entry in entries
        if entry.kind == "mutation"
    }


def scan_legacy_contract(
    root: Path, entries: tuple[LegacySurface, ...] = CONTRACTED_LEGACY_ENTRIES
) -> LegacyContractReport:
    """Public scan of one source tree against the contracted catalog.

    Reports declared owners, rollback-internal edges, canonical source
    edges, missing/extra owners, direct-store drift, and diagnostic raw
    token counts.  Route definitions inside declared owners are authority
    signatures; call sites inside declared owners are frozen
    rollback-internal edges; every other occurrence is a canonical source
    edge.  A legacy route definition outside every catalog entry is an
    uncataloged route (completeness failure).
    """
    root = Path(root)

    scanned = _scan_source_tree(root)
    route_definitions, call_sites, route_details = _scan_mutation_edges(root)
    family_entries = _family_entries(entries)

    # Completeness: owner files exist; legacy routes are cataloged.
    missing_owners = [
        MissingOwner(entry.id, owner)
        for entry in entries
        for owner in entry.owners
        if not (root / owner).is_file()
    ]

    uncataloged_routes: list[UncatalogedRoute] = []
    for relative, method, url, symbol in route_details:
        family = _legacy_family(url, url)
        if family is None or method not in _MUTATION_METHODS:
            continue
        entry = family_entries.get(family)
        if entry is not None and entry.method == method:
            continue
        uncataloged_routes.append(
            UncatalogedRoute(method=method, path=url, owner_file=relative, owner_symbol=symbol)
        )

    # Canonical edges vs rollback-internal occurrences per entry.
    canonical_edges: list[CanonicalEdge] = []
    occurrence_mismatches: list[OccurrenceMismatch] = []
    for entry in entries:
        if entry.kind == "mutation":
            family = _entry_family_path(entry)
            expected = dict(entry.rollback_occurrences)
            for (relative, fam, method), count in sorted(call_sites.items()):
                if fam != family:
                    continue
                if relative in entry.owners:
                    if count != expected.get(relative, 0):
                        occurrence_mismatches.append(
                            OccurrenceMismatch(
                                entry.id, relative, fam, method,
                                count, expected.get(relative, 0),
                            )
                        )
                else:
                    canonical_edges.append(
                        CanonicalEdge(entry.id, relative, fam, method, count)
                    )
            for (relative, fam, method), count in sorted(route_definitions.items()):
                if fam != family:
                    continue
                if relative in entry.owners:
                    if count != expected.get(relative, 0):
                        occurrence_mismatches.append(
                            OccurrenceMismatch(
                                entry.id, relative, fam, method,
                                count, expected.get(relative, 0),
                            )
                        )
                else:
                    canonical_edges.append(
                        CanonicalEdge(entry.id, relative, fam, method, count)
                    )
        else:
            # Page / static entries: every mutation call inside a declared
            # owner is a rollback-internal occurrence frozen at zero.
            for owner, expected_count in entry.rollback_occurrences:
                observed = sum(
                    count
                    for (relative, _fam, _method), count in call_sites.items()
                    if relative == owner
                )
                if observed != expected_count:
                    occurrence_mismatches.append(
                        OccurrenceMismatch(
                            entry.id, owner, "any", "any",
                            observed, expected_count,
                        )
                    )

    direct_store_mismatches: list[DirectStoreMismatch] = []
    direct_store = {
        key: count
        for key, count in scanned.items()
        if key[1] in _DIRECT_STORE_TOKENS
    }
    for key in sorted(set(direct_store) | set(_DIRECT_STORE_ALLOWLIST)):
        observed = direct_store.get(key, 0)
        expected = _DIRECT_STORE_ALLOWLIST.get(key, 0)
        if observed != expected:
            direct_store_mismatches.append(
                DirectStoreMismatch(key[0], key[1], observed, expected)
            )

    entry_reports: dict[str, EntryReport] = {}
    for entry in entries:
        if entry.kind == "mutation":
            family = _entry_family_path(entry)
            owner_paths = frozenset(entry.owners)
            rollback_edges = sum(
                count
                for (relative, fam, _method), count in call_sites.items()
                if fam == family and relative in owner_paths
            ) + sum(
                count
                for (relative, fam, _method), count in route_definitions.items()
                if fam == family and relative in owner_paths
            )
        else:
            rollback_edges = sum(
                count
                for (relative, _fam, _method), count in call_sites.items()
                if relative in entry.owners
            )
        canonical_count = sum(
            edge.occurrences for edge in canonical_edges if edge.entry_id == entry.id
        )
        entry_reports[entry.id] = EntryReport(
            id=entry.id,
            kind=entry.kind,
            method=entry.method,
            path=entry.path,
            description=entry.description,
            declared_owners=entry.owners,
            rollback_occurrences=tuple(
                {"path": path, "expected": count}
                for path, count in entry.rollback_occurrences
            ),
            canonical_edge_occurrences=canonical_count,
            rollback_edge_occurrences=rollback_edges,
        )

    return LegacyContractReport(
        entries=entry_reports,
        canonical_source_edges=tuple(canonical_edges),
        occurrence_mismatches=tuple(occurrence_mismatches),
        missing_owners=tuple(missing_owners),
        uncataloged_routes=tuple(uncataloged_routes),
        direct_store_mismatches=tuple(direct_store_mismatches),
        raw_token_counts=scanned,
    )


# --- Small scan CLI ----------------------------------------------------------


def _cli_scan(args: argparse.Namespace) -> int:
    report = scan_legacy_contract(Path(args.root))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"scan ok={report.ok} completeness={report.completeness_ok} "
          f"zero_canonical_edges={report.zero_canonical_source_edges} -> {output}")
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m task4_consistency.web.legacy_catalog",
        description="Contracted Legacy Entry Catalog: public source scan.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan = subparsers.add_parser("scan", help="scan a source tree against the catalog")
    scan.add_argument("--root", required=True, help="repository root to scan")
    scan.add_argument(
        "--output", required=True, help="JSON report output path"
    )
    scan.set_defaults(handler=_cli_scan)
    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
