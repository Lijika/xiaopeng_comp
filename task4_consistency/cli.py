"""CLI entry: check / evaluate.

Exit codes:
  0  success (evaluate thresholds pass / check ok)
  1  evaluate threshold fail / usage error
  2  check --strict with inconsistent findings
  3  input file not found
  4  invalid JSON / application schema
  5  invalid rule config
  6  unexpected runtime error
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

from task4_consistency.evaluate import write_metrics, write_metrics_html
from task4_consistency.models import Application
from task4_consistency.report import report_to_markdown, write_html_report, write_report
from task4_consistency.rules.engine import RuleEngine
from task4_consistency.rules.loader import load_rules

EXIT_OK = 0
EXIT_THRESHOLD = 1
EXIT_STRICT = 2
EXIT_NOT_FOUND = 3
EXIT_BAD_INPUT = 4
EXIT_BAD_CONFIG = 5
EXIT_RUNTIME = 6


def cmd_check(args: argparse.Namespace) -> int:
    app_path = Path(args.application)
    if not app_path.exists():
        print(f"ERROR: application not found: {app_path}", file=sys.stderr)
        return EXIT_NOT_FOUND
    try:
        with app_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"ERROR: invalid JSON: {e}", file=sys.stderr)
        return EXIT_BAD_INPUT
    except OSError as e:
        print(f"ERROR: cannot read application: {e}", file=sys.stderr)
        return EXIT_NOT_FOUND

    try:
        app = Application.from_dict(data)
    except Exception as e:
        print(f"ERROR: invalid application schema: {e}", file=sys.stderr)
        return EXIT_BAD_INPUT

    try:
        config = load_rules(args.config)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_NOT_FOUND
    except ValueError as e:
        print(f"ERROR: invalid rule config: {e}", file=sys.stderr)
        return EXIT_BAD_CONFIG

    # CLI override: enable ISO 3779 VIN check digit without editing YAML
    if getattr(args, "strict_vin", False):
        config.vin_strict_check_digit = True

    try:
        engine = RuleEngine(config)
        report = engine.run(app)
    except Exception as e:
        print(f"ERROR: runtime: {e}", file=sys.stderr)
        if args.verbose:
            traceback.print_exc()
        return EXIT_RUNTIME

    if args.output:
        write_report(report, args.output)
    else:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))

    if args.markdown:
        md_path = Path(args.markdown)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(report_to_markdown(report), encoding="utf-8")

    if args.html:
        write_html_report(report, args.html)

    if args.strict and report.summary.inconsistent > 0:
        return EXIT_STRICT
    return EXIT_OK


def cmd_evaluate(args: argparse.Namespace) -> int:
    from task4_consistency.evaluate import evaluate_paths, evaluate_suite

    try:
        config_path = args.config
        load_rules(config_path)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_NOT_FOUND
    except ValueError as e:
        print(f"ERROR: invalid rule config: {e}", file=sys.stderr)
        return EXIT_BAD_CONFIG

    thr_overrides: dict[str, float] = {}
    if getattr(args, "max_miss_rate", None) is not None:
        thr_overrides["miss_rate_max"] = float(args.max_miss_rate)

    suite = getattr(args, "suite", None) or "main"
    mode = getattr(args, "mode", None)

    try:
        if args.apps_dir:
            apps_dir = Path(args.apps_dir)
            if not apps_dir.exists():
                print(f"ERROR: apps dir not found: {apps_dir}", file=sys.stderr)
                return EXIT_NOT_FOUND
            metrics = evaluate_paths(
                [apps_dir],
                args.config,
                labels_path=args.labels,
                thresholds=thr_overrides or None,
                suite=suite,
                mode=mode,
            )
        else:
            metrics = evaluate_suite(
                suite=suite,
                rules=args.config,
                labels_path=args.labels,
                thresholds=thr_overrides or None,
                mode=mode,
            )
    except Exception as e:
        print(f"ERROR: evaluate failed: {e}", file=sys.stderr)
        if args.verbose:
            traceback.print_exc()
        return EXIT_RUNTIME

    if args.output:
        write_metrics(metrics, args.output)
        html_path = Path(args.html) if getattr(args, "html", None) else Path(args.output).with_suffix(
            ".html"
        )
        write_metrics_html(metrics, html_path)
    elif getattr(args, "html", None):
        write_metrics_html(metrics, args.html)
    print(json.dumps(metrics.to_dict(), ensure_ascii=False, indent=2))

    # smoke: only smoke_load_ok; labeled: full gates
    ok = all(metrics.pass_thresholds.values()) if metrics.pass_thresholds else True
    if metrics.mode == "smoke":
        print(
            f"SMOKE {'PASS' if ok else 'FAIL'} suite={metrics.suite} "
            f"n_ok={metrics.n_check_ok} n_fail={metrics.n_check_fail}",
            file=sys.stderr,
        )
        if metrics.warnings:
            for w in metrics.warnings:
                print(f"WARNING: {w}", file=sys.stderr)
        return EXIT_OK if ok else EXIT_THRESHOLD

    if not ok:
        failed = [k for k, v in metrics.pass_thresholds.items() if not v]
        print(f"THRESHOLD FAIL: {failed}", file=sys.stderr)
        return EXIT_THRESHOLD
    print("THRESHOLD PASS", file=sys.stderr)
    if metrics.warnings:
        for w in metrics.warnings:
            print(f"WARNING: {w}", file=sys.stderr)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    from task4_consistency import __version__ as _lib_ver

    p = argparse.ArgumentParser(
        prog="task4_consistency",
        description="Cross-document consistency checker (Task 4)",
    )
    p.add_argument(
        "--version",
        action="version",
        version=f"task4_consistency { _lib_ver }",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose errors")
    sub = p.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="Run consistency checks on one application JSON")
    check.add_argument("application", help="Path to application JSON")
    check.add_argument("-c", "--config", required=True, help="Rules YAML path")
    check.add_argument("-o", "--output", help="Write JSON report to path")
    check.add_argument("--markdown", help="Optional markdown report path")
    check.add_argument("--html", help="Optional HTML report path")
    check.add_argument("--strict", action="store_true", help="Exit 2 if any inconsistent")
    check.add_argument(
        "--strict-vin",
        action="store_true",
        help="Force vin_strict_check_digit=true for this run (ISO 3779 check digit)",
    )
    check.set_defaults(func=cmd_check)

    ev = sub.add_parser("evaluate", help="Evaluate fixtures directory against labels")
    ev.add_argument(
        "apps_dir",
        nargs="?",
        default=None,
        help="Optional directory of application JSON (default: suite roots)",
    )
    ev.add_argument("-c", "--config", required=True, help="Rules YAML path")
    ev.add_argument("-o", "--output", help="Write metrics JSON to path")
    ev.add_argument("-l", "--labels", help="Optional labels JSON path")
    ev.add_argument(
        "--suite",
        choices=["main", "semi", "all"],
        default="main",
        help="Fixture suite: main=applications, semi=external_ocr, all=debug (default main)",
    )
    ev.add_argument(
        "--mode",
        choices=["labeled", "smoke"],
        default=None,
        help="Force labeled (FP/FN) or smoke (load-only); default auto by labels",
    )
    ev.add_argument(
        "--max-miss-rate",
        type=float,
        default=None,
        help="Override miss_rate gate (default 0.10); ADV-14 gated threshold",
    )
    ev.add_argument(
        "--html",
        help="Write metrics HTML summary (default: same stem as -o with .html)",
    )
    ev.set_defaults(func=cmd_evaluate)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # propagate verbose to subcommands
    if not hasattr(args, "verbose"):
        args.verbose = False
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
