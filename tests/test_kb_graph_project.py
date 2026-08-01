"""KB graph same_as → address/org alias projection + /api/kb/graph."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from task4_consistency.kb.store import EntityKB, project_graph_to_aliases, reload_kb
from task4_consistency.web import app as webapp

ROOT = Path(__file__).resolve().parents[1]


def test_project_graph_to_aliases_same_as_only():
    graph = {
        "nodes": [
            {"id": "addr:a", "type": "alias", "label": "高新技术产业开发区"},
            {"id": "addr:b", "type": "district", "label": "高新区"},
            {"id": "org:full", "type": "alias", "label": "中国人民财产保险股份有限公司"},
            {"id": "org:short", "type": "insurer", "label": "人保财险"},
            {"id": "brand:x", "type": "brand", "label": "一汽大众"},
            {"id": "brand:y", "type": "brand", "label": "上汽大众"},
            {"id": "addr:c", "type": "city", "label": "南京市"},
        ],
        "edges": [
            {"src": "addr:a", "rel": "same_as", "dst": "addr:b"},
            {"src": "org:full", "rel": "same_as", "dst": "org:short"},
            {"src": "addr:b", "rel": "part_of", "dst": "addr:c"},
            {"src": "brand:x", "rel": "not_same_as", "dst": "brand:y"},
            {"src": "brand:x", "rel": "same_as", "dst": "brand:y"},  # brand skipped
        ],
    }
    proj = project_graph_to_aliases(graph)
    assert proj["address_aliases"]["高新技术产业开发区"] == "高新区"
    assert proj["org_aliases"]["中国人民财产保险股份有限公司"] == "人保财险"
    # part_of / not_same_as / brand same_as 不进 alias
    assert "高新区" not in proj["address_aliases"]
    assert "一汽大众" not in proj["org_aliases"]
    assert "一汽大众" not in proj["address_aliases"]


def test_project_graph_empty_and_broken():
    assert project_graph_to_aliases(None) == {
        "address_aliases": {},
        "org_aliases": {},
    }
    assert project_graph_to_aliases({}) == {
        "address_aliases": {},
        "org_aliases": {},
    }
    # missing nodes: skip edge
    g = {
        "nodes": [{"id": "addr:a", "label": "甲"}],
        "edges": [{"src": "addr:a", "rel": "same_as", "dst": "addr:missing"}],
    }
    assert project_graph_to_aliases(g)["address_aliases"] == {}


def test_entity_kb_load_projects_graph_fill_missing(tmp_path):
    """显式 alias 优先；图 same_as 只补缺。"""
    kb_path = tmp_path / "kb.json"
    kb_path.write_text(
        json.dumps(
            {
                "version": 1,
                "address_aliases": {"高新技术产业开发区": "高新区-显式"},
                "org_aliases": {},
                "plate_prefixes": {},
                "graph": {
                    "nodes": [
                        {
                            "id": "addr:gxq_full",
                            "type": "alias",
                            "label": "高新技术产业开发区",
                        },
                        {"id": "addr:gaoxin", "type": "district", "label": "高新区"},
                        {
                            "id": "org:picc_full",
                            "type": "alias",
                            "label": "中国人民财产保险股份有限公司",
                        },
                        {"id": "org:picc", "type": "insurer", "label": "人保财险"},
                        {
                            "id": "addr:only_graph",
                            "type": "alias",
                            "label": "经开区全称图边",
                        },
                        {
                            "id": "addr:only_dst",
                            "type": "district",
                            "label": "经开区",
                        },
                    ],
                    "edges": [
                        {
                            "src": "addr:gxq_full",
                            "rel": "same_as",
                            "dst": "addr:gaoxin",
                        },
                        {
                            "src": "org:picc_full",
                            "rel": "same_as",
                            "dst": "org:picc",
                        },
                        {
                            "src": "addr:only_graph",
                            "rel": "same_as",
                            "dst": "addr:only_dst",
                        },
                    ],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    kb = EntityKB(kb_path)
    # explicit wins
    assert kb.list_section("address_aliases")["高新技术产业开发区"] == "高新区-显式"
    # graph fills missing
    assert kb.list_section("address_aliases")["经开区全称图边"] == "经开区"
    assert (
        kb.list_section("org_aliases")["中国人民财产保险股份有限公司"] == "人保财险"
    )
    # apply uses projected
    assert "经开区" in kb.apply_aliases("某某经开区全称图边路1号", "address_aliases")
    assert kb.resolve_org("中国人民财产保险股份有限公司") == "人保财险"
    # graph retained in to_dict
    d = kb.to_dict()
    assert "graph" in d
    assert len(d["graph"]["edges"]) >= 1


def test_default_kb_graph_api_and_projection():
    """默认 entity_kb.json：graph 可 GET；same_as 投影与显式别名一致。"""
    kb = reload_kb(ROOT / "configs" / "kb" / "entity_kb.json")
    d = kb.to_dict()
    assert "graph" in d
    nodes = {n["id"]: n for n in d["graph"]["nodes"]}
    assert "addr:gxq_full" in nodes
    proj = project_graph_to_aliases(d["graph"])
    # 投影结果应能被 apply（显式或投影）
    for k, v in proj["address_aliases"].items():
        assert kb.list_section("address_aliases").get(k) == v
    for k, v in proj["org_aliases"].items():
        assert kb.list_section("org_aliases").get(k) == v

    import os

    os.environ.pop("TASK4_WEB_TOKEN", None)
    client = TestClient(webapp.app)
    r = client.get("/api/kb/graph")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "graph" in body
    g = body["graph"]
    assert isinstance(g.get("nodes"), list)
    assert isinstance(g.get("edges"), list)
    assert g["nodes"] and g["edges"]


def test_project_same_as_first_wins_and_skips_blank_label():
    graph = {
        "nodes": [
            {"id": "addr:a", "label": "甲区全称"},
            {"id": "addr:b1", "label": "甲区"},
            {"id": "addr:b2", "label": "甲区-B"},
            {"id": "addr:empty", "label": "   "},
            {"id": "addr:dst", "label": "乙"},
        ],
        "edges": [
            {"src": "addr:a", "rel": "same_as", "dst": "addr:b1"},
            {"src": "addr:a", "rel": "same_as", "dst": "addr:b2"},  # ignored (first wins)
            {"src": "addr:empty", "rel": "same_as", "dst": "addr:dst"},
        ],
    }
    proj = project_graph_to_aliases(graph)
    assert proj["address_aliases"]["甲区全称"] == "甲区"
    assert "甲区全称" in proj["address_aliases"]
    assert list(proj["address_aliases"].values()).count("甲区-B") == 0
    assert "" not in proj["address_aliases"]
    assert "   " not in proj["address_aliases"]


def test_graph_projection_affects_normalize_address(tmp_path):
    """仅图边、无显式 alias 时，normalize_address 仍应吃到投影。"""
    from task4_consistency.normalize.address import normalize_address

    kb_path = tmp_path / "kb_graph_only.json"
    kb_path.write_text(
        json.dumps(
            {
                "version": 1,
                "address_aliases": {},
                "org_aliases": {},
                "plate_prefixes": {},
                "graph": {
                    "nodes": [
                        {"id": "addr:full", "label": "图投影开发区全称XYZ"},
                        {"id": "addr:short", "label": "图投影区"},
                    ],
                    "edges": [
                        {"src": "addr:full", "rel": "same_as", "dst": "addr:short"},
                    ],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    reload_kb(kb_path)
    # apply path used by normalize
    from task4_consistency.kb.store import get_kb

    assert get_kb().apply_aliases("江苏南京图投影开发区全称XYZ路1号") == (
        "江苏南京图投影区路1号"
    )
    # restore default for other tests
    reload_kb(ROOT / "configs" / "kb" / "entity_kb.json")
    _ = normalize_address  # import side-effect ok
