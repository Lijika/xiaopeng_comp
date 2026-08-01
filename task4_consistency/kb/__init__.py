"""Maintainable entity knowledge base (address/org/plate aliases).

Stable public surface (Round17 / ADV-K*):
  - EntityKB, get_kb, reload_kb
  - add_alias / remove_alias / list_section / apply_aliases  (module-level)
"""

from __future__ import annotations

from task4_consistency.kb.store import (
    EntityKB,
    get_kb,
    project_graph_to_aliases,
    reload_kb,
)

__all__ = [
    "EntityKB",
    "get_kb",
    "reload_kb",
    "project_graph_to_aliases",
    "add_alias",
    "remove_alias",
    "list_section",
    "apply_aliases",
]


def add_alias(section: str, key: str, value: str) -> None:
    """Add alias to process-global KB (get_kb().add_alias)."""
    get_kb().add_alias(section, key, value)


def remove_alias(section: str, key: str) -> bool:
    """Remove alias from process-global KB."""
    return get_kb().remove_alias(section, key)


def list_section(section: str) -> dict[str, str]:
    """List one KB section as dict."""
    return get_kb().list_section(section)


def apply_aliases(text: str, section: str = "address_aliases") -> str:
    """Apply section aliases to text."""
    return get_kb().apply_aliases(text, section)
