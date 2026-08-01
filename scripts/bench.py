#!/usr/bin/env python3
"""Microbench: 100 applications × default rules → out/bench.json.

Target: mean per-application < 50ms.
Round21: --check-regression warns (exit 3) if mean > 2× previous baseline.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from task4_consistency.models import Application
from task4_consistency.rules.engine import RuleEngine
from task4_consistency.rules.loader import load_rules

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out" / "bench.json"
BASELINE = ROOT / "out" / "bench_baseline.json"


def run_bench() -> dict:
    rules = load_rules(ROOT / "configs" / "rules_auto_lease.yaml")
    eng = RuleEngine(rules)
    apps_dir = ROOT / "fixtures" / "applications"
    files = sorted(apps_dir.glob("*.json"))
    if not files:
        raise SystemExit("no fixtures")

    apps: list[Application] = []
    raws = []
    for fp in files:
        raws.append(json.loads(fp.read_text(encoding="utf-8")))
    i = 0
    while len(apps) < 100:
        data = dict(raws[i % len(raws)])
        data["application_id"] = f"BENCH-{len(apps):03d}"
        apps.append(Application.from_dict(data))
        i += 1

    for a in apps[:3]:
        eng.run(a)

    t0 = time.perf_counter()
    for a in apps:
        eng.run(a)
    elapsed = time.perf_counter() - t0
    n = len(apps)
    mean_ms = (elapsed / n) * 1000
    return {
        "n_applications": n,
        "n_rules": len(rules.rules),
        "total_seconds": round(elapsed, 4),
        "mean_ms_per_app": round(mean_ms, 3),
        "target_ms_per_app": 50.0,
        "pass": mean_ms < 50.0,
        "package": rules.package,
        "version": str(rules.version),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Task4 microbench")
    ap.add_argument(
        "--check-regression",
        action="store_true",
        help="Compare to out/bench_baseline.json (or previous bench.json); exit 3 if >2x",
    )
    ap.add_argument(
        "--write-baseline",
        action="store_true",
        help="Also write out/bench_baseline.json from this run",
    )
    args = ap.parse_args(argv)

    result = run_bench()
    OUT.parent.mkdir(parents=True, exist_ok=True)

    prev_mean = None
    base_path = BASELINE if BASELINE.exists() else (OUT if OUT.exists() else None)
    if args.check_regression and base_path and base_path.exists():
        try:
            prev = json.loads(base_path.read_text(encoding="utf-8"))
            prev_mean = float(prev.get("mean_ms_per_app") or 0)
        except (json.JSONDecodeError, TypeError, ValueError):
            prev_mean = None

    regression = False
    if prev_mean and prev_mean > 0:
        ratio = result["mean_ms_per_app"] / prev_mean
        result["baseline_mean_ms"] = prev_mean
        result["regression_ratio"] = round(ratio, 3)
        result["baseline_path"] = str(base_path.relative_to(ROOT)) if base_path else None
        if ratio > 2.0:
            regression = True
            result["regression_warn"] = True

    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.write_baseline or not BASELINE.exists():
        BASELINE.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result["baseline_written"] = True

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["pass"]:
        return 2
    if regression:
        print(
            f"REGRESSION WARN: mean_ms {result['mean_ms_per_app']} > 2x baseline {prev_mean}",
            flush=True,
        )
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
