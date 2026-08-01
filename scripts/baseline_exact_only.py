#!/usr/bin/env python3
"""Baseline: raw-string exact compare WITHOUT field normalizers.

Compares against full RuleEngine on the same fixtures to show
how many 'consistent-looking' raw pairs become false positives / misses
when only naive string equality is used.

Usage:
  .venv/bin/python scripts/baseline_exact_only.py -o out/baseline_compare.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from task4_consistency.models import Application, Verdict
from task4_consistency.rules.engine import RuleEngine
from task4_consistency.rules.loader import load_rules

ROOT = Path(__file__).resolve().parents[1]


def raw_values(app: Application, field_aliases: dict[str, list[str]], field: str, docs: list[str]) -> list[str]:
    names = list(field_aliases.get(field) or [field])
    if field not in names:
        names = [field] + names
    by = app.docs_by_type()
    vals: list[str] = []
    for dtype in docs or by.keys():
        for doc in by.get(dtype, []):
            for n in names:
                if n in doc.fields and doc.fields[n].raw not in (None, ""):
                    vals.append(str(doc.fields[n].raw).strip())
                    break
    return vals


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default=str(ROOT / "configs/rules_auto_lease.yaml"))
    ap.add_argument("-d", "--apps", default=str(ROOT / "fixtures/applications"))
    ap.add_argument("-o", "--output", default=str(ROOT / "out/baseline_compare.json"))
    args = ap.parse_args()

    cfg = load_rules(args.config)
    eng = RuleEngine(cfg)
    apps_dir = Path(args.apps)

    # Only exact-type rules for fair baseline
    exact_rules = [r for r in cfg.rules if r.type == "exact"]
    naive_consistent = 0
    naive_inconsistent = 0
    engine_consistent = 0
    engine_inconsistent = 0
    engine_uncertain = 0
    # Cases where raw strings differ but engine says consistent (normalize win)
    raw_diff_engine_ok = 0
    # Cases where raw equal but engine inconsistent/uncertain (rare)
    raw_same_engine_bad = 0
    pairs = 0

    per_rule: dict[str, dict] = {}

    for fp in sorted(apps_dir.glob("*.json")):
        data = json.loads(fp.read_text(encoding="utf-8"))
        app = Application.from_dict(data)
        report = eng.run(app)
        by_id = {c.rule_id: c for c in report.checks}

        for rule in exact_rules:
            field = rule.field or ""
            docs = list(rule.docs or [])
            raws = raw_values(app, cfg.field_aliases, field, docs)
            if len(raws) < 2:
                continue
            pairs += 1
            naive_eq = all(r == raws[0] for r in raws[1:])
            if naive_eq:
                naive_consistent += 1
            else:
                naive_inconsistent += 1

            c = by_id.get(rule.id)
            if not c:
                continue
            if c.verdict == Verdict.CONSISTENT:
                engine_consistent += 1
            elif c.verdict == Verdict.INCONSISTENT:
                engine_uncertain += 0
                engine_inconsistent += 1
            else:
                engine_uncertain += 1

            if (not naive_eq) and c.verdict == Verdict.CONSISTENT:
                raw_diff_engine_ok += 1
            if naive_eq and c.verdict != Verdict.CONSISTENT:
                raw_same_engine_bad += 1

            st = per_rule.setdefault(
                rule.id,
                {
                    "naive_eq": 0,
                    "naive_neq": 0,
                    "engine_consistent": 0,
                    "engine_inconsistent": 0,
                    "engine_uncertain": 0,
                    "normalize_rescue": 0,
                },
            )
            st["naive_eq" if naive_eq else "naive_neq"] += 1
            st[
                {
                    Verdict.CONSISTENT: "engine_consistent",
                    Verdict.INCONSISTENT: "engine_inconsistent",
                }.get(c.verdict, "engine_uncertain")
            ] += 1
            if (not naive_eq) and c.verdict == Verdict.CONSISTENT:
                st["normalize_rescue"] += 1

    out = {
        "description": "Naive raw-string exact vs full RuleEngine (exact rules only)",
        "n_apps": len(list(apps_dir.glob("*.json"))),
        "n_exact_rule_pairs": pairs,
        "naive": {
            "consistent_pairs": naive_consistent,
            "inconsistent_pairs": naive_inconsistent,
            "consistent_rate": round(naive_consistent / pairs, 4) if pairs else 0,
        },
        "engine": {
            "consistent_pairs": engine_consistent,
            "inconsistent_pairs": engine_inconsistent,
            "uncertain_pairs": engine_uncertain,
        },
        "delta": {
            "raw_diff_but_engine_consistent": raw_diff_engine_ok,
            "raw_same_but_engine_not_consistent": raw_same_engine_bad,
            "note": "raw_diff_but_engine_consistent = normalize/alias 消解的书写变体（基线会误报为不一致）",
        },
        "per_rule": per_rule,
        "claim": (
            f"在 {pairs} 个 exact 规则×申请对上，原始串不相等但引擎判定一致的 rescue="
            f"{raw_diff_engine_ok}；纯精确串基线会把这些计为不一致（误报风险）。"
        ),
    }
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
