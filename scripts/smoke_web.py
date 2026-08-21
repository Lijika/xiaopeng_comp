#!/usr/bin/env python3
"""Round24 Web smoke (no live server): health + check one fixture via TestClient.

  .venv/bin/python scripts/smoke_web.py
Exit 0 on success, 1 on failure.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    os.environ.pop("TASK4_WEB_TOKEN", None)
    try:
        from fastapi.testclient import TestClient

        from task4_consistency.web.app import app
    except Exception as e:
        print(f"FAIL import web app: {e}", file=sys.stderr)
        return 1

    client = TestClient(app)
    errors: list[str] = []

    # 1) health
    r = client.get("/api/health")
    if r.status_code != 200:
        errors.append(f"health HTTP {r.status_code}")
    else:
        body = r.json()
        for key in ("ok", "rules_path", "kb_ok", "version"):
            if key not in body:
                errors.append(f"health missing {key}")
        if body.get("ok") is not True:
            errors.append(f"health ok={body.get('ok')}")
        if body.get("kb_ok") is not True:
            errors.append(f"health kb_ok={body.get('kb_ok')}")
        print(
            f"health OK rules={body.get('rules_path')} "
            f"pkg={body.get('package')}@{body.get('version')} kb_ok={body.get('kb_ok')}"
        )

    # 2) list fixtures + check first / preferred consistent sample
    r = client.get("/api/fixtures")
    if r.status_code != 200:
        errors.append(f"fixtures HTTP {r.status_code}")
        fixtures = []
    else:
        fixtures = r.json().get("fixtures") or []
        if not fixtures:
            errors.append("fixtures empty")
        print(f"fixtures n={len(fixtures)}")

    preferred = "app_consistent_01.json"
    name = preferred if any(f.get("file") == preferred for f in fixtures) else (
        fixtures[0]["file"] if fixtures else None
    )
    if name:
        r = client.get(f"/api/fixtures/{name}")
        if r.status_code != 200:
            errors.append(f"get fixture {name} HTTP {r.status_code}")
        else:
            app_json = r.json()
            r = client.post("/api/check", json={"application": app_json})
            if r.status_code != 200:
                errors.append(f"check HTTP {r.status_code}: {r.text[:200]}")
            else:
                rep = r.json().get("report") or {}
                summary = rep.get("summary") or {}
                n = summary.get("total") or summary.get("total_including_skipped") or 0
                print(
                    f"check OK fixture={name} application_id={rep.get('application_id')} "
                    f"total={n} inconsistent={summary.get('inconsistent')} "
                    f"uncertain={summary.get('uncertain')}"
                )
                if "checks" not in rep:
                    errors.append("check response missing report.checks")
                if not isinstance(rep.get("checks"), list):
                    errors.append("report.checks not list")

    # 3) HTML shell
    r = client.get("/")
    react_shell = 'type="module"' in r.text and "/static/react/assets/" in r.text
    if r.status_code != 200 or not react_shell:
        errors.append("canonical React shell missing or not 200")
    else:
        print("index OK")

    if errors:
        for e in errors:
            print(f"FAIL: {e}", file=sys.stderr)
        return 1
    print("SMOKE_WEB PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
