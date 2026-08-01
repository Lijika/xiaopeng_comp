"""Round43: audit envelope schema_ver=1 + legacy read compat."""

from __future__ import annotations

import json
from pathlib import Path

from task4_consistency.audit import (
    AUDIT_SCHEMA_VER,
    normalize_audit_record,
    read_audit_tail,
    write_audit,
)


def test_write_audit_includes_schema_ver_1(tmp_path, monkeypatch):
    log = tmp_path / "a.log"
    monkeypatch.setenv("TASK4_AUDIT_LOG", str(log))
    assert write_audit("rules_save", ok=True, detail={"n": 1}) is True
    line = log.read_text(encoding="utf-8").strip().splitlines()[-1]
    rec = json.loads(line)
    assert rec["schema_ver"] == 1 == AUDIT_SCHEMA_VER
    assert rec["action"] == "rules_save"
    assert rec["ok"] is True
    assert isinstance(rec["detail"], dict)
    assert "ts" in rec and "actor" in rec


def test_read_tail_legacy_without_schema_ver(tmp_path, monkeypatch):
    log = tmp_path / "mix.log"
    monkeypatch.setenv("TASK4_AUDIT_LOG", str(log))
    # legacy line
    legacy = {"ts": "2020-01-01T00:00:00+00:00", "action": "old", "actor": "web", "ok": True, "detail": {}}
    log.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
    write_audit("new_action", detail={"x": 1})
    events = read_audit_tail(10)
    assert len(events) == 2
    assert events[0]["schema_ver"] == 0  # legacy normalized
    assert events[0]["action"] == "old"
    assert events[1]["schema_ver"] == 1
    assert events[1]["action"] == "new_action"


def test_normalize_legacy():
    r = normalize_audit_record({"action": "x", "ts": "t"})
    assert r["schema_ver"] == 0
    assert r["detail"] == {}
