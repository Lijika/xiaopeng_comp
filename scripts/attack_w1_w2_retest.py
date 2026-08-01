#!/usr/bin/env python3
"""Round16 retest ADV-W1 / ADV-W2 against live Web.

  .venv/bin/python scripts/attack_w1_w2_retest.py
  .venv/bin/python scripts/attack_w1_w2_retest.py --base http://127.0.0.1:8765

Exit 0 iff all subcases CLOSED. Spec: docs/ATTACK_CASES.md § Round16 复验规格.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "configs" / "runtime_rules.yaml"


def req(base: str, method: str, path: str, body=None):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    headers = {"Content-Type": "application/json; charset=utf-8"} if body is not None else {}
    r = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw) if raw else raw
        except json.JSONDecodeError:
            return e.code, raw
    except Exception as e:
        return 0, str(e)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    results: list[tuple[str, bool, str]] = []

    def rec(cid: str, closed: bool, detail: str) -> None:
        results.append((cid, closed, detail))
        print(f"{cid:8} | {'CLOSED' if closed else 'OPEN':6} | {detail}")

    # cleanup
    req(base, "POST", "/api/rules/reset")
    if RUNTIME.exists():
        try:
            RUNTIME.unlink()
        except OSError:
            pass

    hcode, hbody = req(base, "GET", "/api/health")
    if hcode != 200:
        print(f"FAIL: health {hcode} {hbody} — start web first / restart after Round16")
        return 2

    # ----- W1-a missing id -----
    code, _ = req(
        base,
        "PUT",
        "/api/rules",
        {
            "content": {
                "version": 1,
                "field_aliases": {},
                "rules": [{"type": "exact", "field": "vin", "docs": ["发票"]}],
            }
        },
    )
    poisoned = False
    if RUNTIME.exists():
        try:
            from task4_consistency.rules.loader import load_rules

            load_rules(RUNTIME)
        except Exception:
            poisoned = True
    rec(
        "W1-a",
        code >= 400 and not poisoned,
        f"put={code} runtime_exists={RUNTIME.exists()} poisoned={poisoned}",
    )
    if RUNTIME.exists() and poisoned:
        RUNTIME.unlink(missing_ok=True)

    # ----- W1-b bad yaml -----
    code, _ = req(base, "PUT", "/api/rules", {"yaml_text": "rules: ["})
    hcode, hbody = req(base, "GET", "/api/health")
    ok_health = hcode == 200 and isinstance(hbody, dict) and hbody.get("ok") is True
    rec("W1-b", code >= 400 and ok_health, f"put={code} health={hcode} ok={ok_health}")

    # ----- W1-c rollback -----
    code, rg = req(base, "GET", "/api/rules")
    if code != 200 or not isinstance(rg, dict):
        rec("W1-c", False, f"GET rules failed {code}")
    else:
        content = copy.deepcopy(rg["content"])
        content["version"] = "w1c-good"
        code_ok, _ = req(base, "PUT", "/api/rules", {"content": content})
        code_bad, _ = req(
            base,
            "PUT",
            "/api/rules",
            {
                "content": {
                    "version": "w1c-bad",
                    "field_aliases": {},
                    "rules": [{"type": "exact"}],
                }
            },
        )
        code_g, rg2 = req(base, "GET", "/api/rules")
        ver = None
        if code_g == 200 and isinstance(rg2, dict):
            ver = (rg2.get("content") or {}).get("version")
        rec(
            "W1-c",
            code_ok == 200 and code_bad >= 400 and ver == "w1c-good",
            f"good={code_ok} bad={code_bad} version_now={ver!r}",
        )

    req(base, "POST", "/api/rules/reset")
    if RUNTIME.exists():
        RUNTIME.unlink(missing_ok=True)

    # ----- W2 cases need baseline -----
    code, rg = req(base, "GET", "/api/rules")
    if code != 200 or not isinstance(rg, dict):
        print("FAIL: cannot GET rules for W2")
        return 2
    baseline = copy.deepcopy(rg["content"])

    # W2-a delete VIN
    c = copy.deepcopy(baseline)
    c["rules"] = [r for r in c.get("rules") or [] if r.get("id") != "R_VIN_CROSS"]
    code, body = req(base, "PUT", "/api/rules", {"content": c})
    rec("W2-a", code >= 400, f"put={code} body={str(body)[:100]}")

    # W2-b drop all critical
    c = copy.deepcopy(baseline)
    for r in c.get("rules") or []:
        if str(r.get("severity", "")).lower() == "critical":
            r["severity"] = "major"
    code, body = req(base, "PUT", "/api/rules", {"content": c})
    rec("W2-b", code >= 400, f"put={code} body={str(body)[:100]}")

    # W2-c demote VIN to info
    c = copy.deepcopy(baseline)
    for r in c.get("rules") or []:
        if r.get("id") == "R_VIN_CROSS":
            r["severity"] = "info"
    code, body = req(base, "PUT", "/api/rules", {"content": c})
    rec("W2-c", code >= 400, f"put={code} body={str(body)[:100]}")

    # W2-d on_missing skip
    c = copy.deepcopy(baseline)
    for r in c.get("rules") or []:
        if r.get("id") == "R_VIN_CROSS":
            r["on_missing"] = "skip"
    code, body = req(base, "PUT", "/api/rules", {"content": c})
    rec("W2-d", code >= 400, f"put={code} body={str(body)[:100]}")

    # W2-f version bump ok
    c = copy.deepcopy(baseline)
    c["version"] = str(c.get("version", "1")) + "-w2f"
    code, body = req(base, "PUT", "/api/rules", {"content": c})
    rec("W2-f", code == 200, f"put={code}")

    req(base, "POST", "/api/rules/reset")
    if RUNTIME.exists():
        RUNTIME.unlink(missing_ok=True)

    print("---")
    open_n = sum(1 for _, closed, _ in results if not closed)
    for cid, closed, detail in results:
        if not closed:
            print(f"OPEN {cid}: {detail}")
    print(f"open={open_n}/{len(results)}")
    return 0 if open_n == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
