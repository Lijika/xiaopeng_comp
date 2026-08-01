"""Round17: stable kb module API + ADV-K* closed."""

from __future__ import annotations

from pathlib import Path

import pytest

import task4_consistency.kb as kb
from task4_consistency.kb.store import reload_kb

ROOT = Path(__file__).resolve().parents[1]


def test_kb_module_exports():
    assert callable(kb.add_alias)
    assert callable(kb.remove_alias)
    assert callable(kb.list_section)
    assert callable(kb.apply_aliases)
    assert callable(kb.get_kb)
    assert callable(kb.reload_kb)


def test_kb_module_add_remove_isolated(tmp_path):
    p = tmp_path / "kb.json"
    p.write_text(
        '{"version":1,"address_aliases":{},"org_aliases":{},"plate_prefixes":{}}',
        encoding="utf-8",
    )
    reload_kb(p)
    kb.add_alias("org_aliases", "某某融资租赁有限公司", "某某金租")
    assert kb.list_section("org_aliases")["某某融资租赁有限公司"] == "某某金租"
    assert kb.remove_alias("org_aliases", "某某融资租赁有限公司") is True
    assert "某某融资租赁有限公司" not in kb.list_section("org_aliases")
    reload_kb(ROOT / "configs" / "kb" / "entity_kb.json")


def test_kb_unknown_section_rejected(tmp_path):
    p = tmp_path / "kb.json"
    p.write_text(
        '{"version":1,"address_aliases":{},"org_aliases":{},"plate_prefixes":{}}',
        encoding="utf-8",
    )
    reload_kb(p)
    with pytest.raises(KeyError):
        kb.add_alias("person", "张三", "李四")
    reload_kb(ROOT / "configs" / "kb" / "entity_kb.json")
