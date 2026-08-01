"""JSON-backed entity knowledge base with CRUD."""

from __future__ import annotations

import json
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_PATH = _ROOT / "configs" / "kb" / "entity_kb.json"

_lock = threading.RLock()
_instance: "EntityKB | None" = None

# ADV-K3: refuse address aliases that remap one known city to another
_CITY_TOKENS = (
    "北京",
    "上海",
    "天津",
    "重庆",
    "南京",
    "苏州",
    "无锡",
    "常州",
    "杭州",
    "宁波",
    "广州",
    "深圳",
    "成都",
    "武汉",
    "西安",
    "青岛",
    "厦门",
    "合肥",
    "福州",
    "济南",
    "郑州",
    "长沙",
    "沈阳",
    "大连",
    "哈尔滨",
    "长春",
    "昆明",
    "贵阳",
    "南宁",
    "海口",
    "石家庄",
    "太原",
    "呼和浩特",
    "银川",
    "西宁",
    "乌鲁木齐",
    "拉萨",
    "兰州",
)


def _cities_in(text: str) -> set[str]:
    return {c for c in _CITY_TOKENS if c in text}


def _validate_address_alias(key: str, value: str) -> None:
    """Block cross-city remaps (江苏苏州→江苏南京)."""
    ck, cv = _cities_in(key), _cities_in(value)
    if ck and cv and ck != cv:
        raise ValueError(
            f"address alias must not remap cities {sorted(ck)} → {sorted(cv)} (ADV-K3)"
        )


def project_graph_to_aliases(graph: dict[str, Any] | None) -> dict[str, dict[str, str]]:
    """Project graph ``same_as`` edges into address/org alias maps.

    - ``addr:*`` nodes → ``address_aliases`` (src.label → dst.label)
    - ``org:*`` nodes → ``org_aliases``
    - ``part_of`` / ``type`` / ``not_same_as`` are not projected
    - other id prefixes (e.g. brand:) skipped
    """
    out: dict[str, dict[str, str]] = {"address_aliases": {}, "org_aliases": {}}
    if not isinstance(graph, dict):
        return out
    nodes: dict[str, dict[str, Any]] = {}
    for n in graph.get("nodes") or []:
        if isinstance(n, dict) and n.get("id"):
            nodes[str(n["id"])] = n
    for e in graph.get("edges") or []:
        if not isinstance(e, dict) or e.get("rel") != "same_as":
            continue
        src = nodes.get(str(e.get("src") or ""))
        dst = nodes.get(str(e.get("dst") or ""))
        if not src or not dst:
            continue
        sk = str(src.get("label") or "").strip()
        dk = str(dst.get("label") or "").strip()
        if not sk or not dk or sk == dk:
            continue
        sid = str(src.get("id") or "")
        if sid.startswith("addr:"):
            section = "address_aliases"
        elif sid.startswith("org:"):
            section = "org_aliases"
        else:
            continue
        out[section].setdefault(sk, dk)
    return out


class EntityKB:
    """Thread-safe in-memory KB loaded from JSON."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else _DEFAULT_PATH
        self._data: dict[str, Any] = {
            "version": 1,
            "address_aliases": {},
            "org_aliases": {},
            "plate_prefixes": {},
        }
        self.load()

    def load(self) -> None:
        with _lock:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("KB root must be object")
                address_aliases = dict(raw.get("address_aliases") or {})
                org_aliases = dict(raw.get("org_aliases") or {})
                graph = raw.get("graph") if isinstance(raw.get("graph"), dict) else None
                # same_as → aliases；显式表优先，图投影只补缺
                projected = project_graph_to_aliases(graph)
                for k, v in projected["address_aliases"].items():
                    address_aliases.setdefault(k, v)
                for k, v in projected["org_aliases"].items():
                    org_aliases.setdefault(k, v)
                self._data = {
                    "version": raw.get("version", 1),
                    "description": raw.get("description", ""),
                    "address_aliases": address_aliases,
                    "org_aliases": org_aliases,
                    "plate_prefixes": dict(raw.get("plate_prefixes") or {}),
                }
                if graph is not None:
                    self._data["graph"] = graph
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.save()

    def save(self) -> None:
        with _lock:
            # 保留磁盘上已有 graph（若内存未带 graph，避免 CRUD 写回时抹掉）
            if "graph" not in self._data and self.path.is_file():
                try:
                    prev = json.loads(self.path.read_text(encoding="utf-8"))
                    if isinstance(prev, dict) and isinstance(prev.get("graph"), dict):
                        self._data["graph"] = prev["graph"]
                except (OSError, json.JSONDecodeError):
                    pass
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

    def to_dict(self) -> dict[str, Any]:
        with _lock:
            return deepcopy(self._data)

    def list_section(self, section: str) -> dict[str, str]:
        with _lock:
            sec = self._data.get(section)
            if not isinstance(sec, dict):
                raise KeyError(f"unknown KB section: {section}")
            return {str(k): str(v) for k, v in sec.items()}

    def add_alias(self, section: str, key: str, value: str) -> None:
        key, value = str(key).strip(), str(value).strip()
        if not key or not value:
            raise ValueError("key/value required")
        # ADV-K10: refuse single-char / tiny keys that rewrite whole addresses
        if len(key) < 2:
            raise ValueError("alias key too short (min 2 chars; ADV-K10)")
        if section == "address_aliases":
            _validate_address_alias(key, value)
        with _lock:
            if section not in self._data or not isinstance(self._data[section], dict):
                raise KeyError(f"unknown KB section: {section}")
            self._data[section][key] = value
            self.save()

    def remove_alias(self, section: str, key: str) -> bool:
        with _lock:
            sec = self._data.get(section)
            if not isinstance(sec, dict) or key not in sec:
                return False
            del sec[key]
            self.save()
            return True

    def apply_aliases(self, text: str, section: str = "address_aliases") -> str:
        """Replace longer keys first."""
        aliases = self.list_section(section)
        out = text
        for key in sorted(aliases.keys(), key=len, reverse=True):
            if key in out:
                out = out.replace(key, aliases[key])
        return out

    def resolve_org(self, text: str) -> str:
        return self.apply_aliases(text, "org_aliases")


def get_kb(path: Path | None = None) -> EntityKB:
    global _instance
    with _lock:
        if _instance is None or (path and Path(path) != _instance.path):
            _instance = EntityKB(path)
        return _instance


def reload_kb(path: Path | None = None) -> EntityKB:
    global _instance
    with _lock:
        _instance = EntityKB(path)
        return _instance
