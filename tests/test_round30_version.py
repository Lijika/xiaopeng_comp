"""Round30: pyproject version matches package __version__; health exposes lib_version."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

import task4_consistency
from task4_consistency.web.app import app

ROOT = Path(__file__).resolve().parents[1]


def test_pyproject_matches_package_version():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    assert m, "pyproject.toml missing version"
    assert m.group(1) == task4_consistency.__version__


def test_health_lib_version():
    client = TestClient(app)
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body.get("lib_version") == task4_consistency.__version__
    assert body.get("version") is not None  # rules package version
