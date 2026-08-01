"""Evaluation metrics against labeled fixtures.

Metric definitions (single source of truth):
- coverage = decisive_pairs / total_pairs
  where decisive = predicted not uncertain (among labeled pairs)
- false_positive_rate = FP / (TN + FP)
  FP: expected consistent, predicted inconsistent
  TN: expected consistent, predicted consistent
- false_negative_rate = FN / (TP + FN)
  FN: expected inconsistent, predicted consistent
  TP: expected inconsistent, predicted inconsistent
- predicted uncertain is excluded from FPR/FNR denominators
- expected uncertain with decisive prediction is excluded from FPR/FNR
  (not counted as error; tracked in uncertain_when_labeled / labels_uncertain)

Soft-label fallback (derive expected from prediction) is DISABLED.
Fixtures without expected_verdicts are skipped with a warning.
"""

from __future__ import annotations

import json
import warnings
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from task4_consistency.models import Application, Report, Verdict
from task4_consistency.rules.engine import RuleEngine
from task4_consistency.rules.loader import RuleConfig, load_rules


@dataclass
class EvalPair:
    application_id: str
    rule_id: str
    expected: str
    predicted: str


@dataclass
class Metrics:
    coverage: float
    false_positive_rate: float
    false_negative_rate: float
    accuracy: float
    total_pairs: int
    decisive_pairs: int
    true_positive: int  # expected inconsistent, predicted inconsistent
    true_negative: int  # expected consistent, predicted consistent
    false_positive: int  # expected consistent, predicted inconsistent
    false_negative: int  # expected inconsistent, predicted consistent
    uncertain_when_labeled: int
    mean_app_coverage: float = 0.0
    n_inconsistent_labeled_decisive: int = 0
    # miss_rate: expected=inconsistent and predicted in {consistent, uncertain}
    # (catches ADV-07 FNR-hide via uncertain)
    miss_rate: float = 0.0
    n_expected_inconsistent: int = 0
    n_missed_inconsistent: int = 0
    warnings: list[str] = field(default_factory=list)
    pairs: list[dict[str, str]] = field(default_factory=list)
    per_application: dict[str, Any] = field(default_factory=dict)
    pass_thresholds: dict[str, bool] = field(default_factory=dict)
    metric_definitions: dict[str, str] = field(default_factory=dict)
    # Round19 suite honesty
    suite: str = "main"
    mode: str = "labeled"  # labeled | smoke
    honesty_note: str = ""
    n_apps_loaded: int = 0
    n_check_ok: int = 0
    n_check_fail: int = 0
    verdict_counts: dict[str, int] = field(default_factory=dict)
    uncertain_rate: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Repo-relative suite roots (resolved against package parent)
_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SUITE_DIRS: dict[str, list[Path]] = {
    "main": [_PACKAGE_ROOT / "fixtures" / "applications"],
    "semi": [_PACKAGE_ROOT / "fixtures" / "semi"],
    "all": [
        _PACKAGE_ROOT / "fixtures" / "applications",
        _PACKAGE_ROOT / "fixtures" / "semi",
    ],
}

HONESTY_MAIN = (
    "Official delivery metrics from suite=main only. "
    "field_source=synthetic/step2-bound fixtures are not external OCR. "
    "Do not claim real-OCR evaluation unless field_source=external_ocr."
)
HONESTY_SEMI_SMOKE = (
    "suite=semi smoke mode: no reliable labels → FP/FN/coverage gates NOT claimed. "
    "Only load/run stability + verdict distribution. "
    "Never use semi smoke to assert miss_rate/FNR thresholds."
)
HONESTY_SEMI_LABELED = (
    "suite=semi labeled: FP/FN computed separately from main; "
    "must NOT be merged into main denominator for delivery claims."
)


def load_labels(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def extract_expected_verdicts(
    app_data: dict[str, Any], labels: dict[str, Any] | None
) -> dict[str, str]:
    """Prefer fixture-embedded expected_verdicts; fallback to labels file.

    Does NOT invent labels from predictions.
    """
    if "expected_verdicts" in app_data and isinstance(app_data["expected_verdicts"], dict):
        return {str(k): str(v) for k, v in app_data["expected_verdicts"].items()}
    app_id = str(app_data.get("application_id") or "")
    if labels and app_id in labels:
        entry = labels[app_id]
        if isinstance(entry, dict) and "expected_verdicts" in entry:
            return {str(k): str(v) for k, v in entry["expected_verdicts"].items()}
        if isinstance(entry, dict):
            return {
                str(k): str(v)
                for k, v in entry.items()
                if k not in {"overall", "expected_overall", "label"}
            }
    if labels and "applications" in labels:
        entry = labels["applications"].get(app_id) or {}
        if "expected_verdicts" in entry:
            return {str(k): str(v) for k, v in entry["expected_verdicts"].items()}
        return {
            str(k): str(v)
            for k, v in entry.items()
            if k not in {"overall", "expected_overall", "label"}
        }
    return {}


def evaluate_report(
    report: Report,
    expected: dict[str, str],
) -> tuple[list[EvalPair], dict[str, Any]]:
    pred = {c.rule_id: c.verdict.value for c in report.checks}
    pairs: list[EvalPair] = []
    for rule_id, exp in expected.items():
        pairs.append(
            EvalPair(
                application_id=report.application_id,
                rule_id=rule_id,
                expected=exp,
                predicted=pred.get(rule_id, Verdict.UNCERTAIN.value),
            )
        )
    return pairs, {"coverage": report.summary.coverage, "predicted": pred}


# Default acceptance gates (ADV-14: miss_rate is gated, not advisory-only)
DEFAULT_THRESHOLDS: dict[str, float] = {
    "coverage_min": 0.80,
    "false_positive_rate_max": 0.05,
    "false_negative_rate_max": 0.03,
    "miss_rate_max": 0.10,
}


def compute_metrics(
    pairs: list[EvalPair],
    app_coverages: list[float],
    *,
    thresholds: dict[str, float] | None = None,
) -> Metrics:
    tp = tn = fp = fn = 0
    uncertain_pred = 0
    decisive = 0
    skipped_pairs = 0
    active_pairs = 0  # non-skipped labeled pairs (coverage denom)
    n_exp_inc = 0
    n_missed = 0  # expected inconsistent but pred consistent or uncertain

    for p in pairs:
        exp = p.expected
        pred = p.predicted
        # skipped predictions/labels excluded from all rates and coverage denom
        if pred == Verdict.SKIPPED.value or exp == Verdict.SKIPPED.value:
            skipped_pairs += 1
            continue
        active_pairs += 1

        if exp == Verdict.INCONSISTENT.value:
            n_exp_inc += 1
            if pred in (Verdict.CONSISTENT.value, Verdict.UNCERTAIN.value):
                n_missed += 1

        if pred == Verdict.UNCERTAIN.value:
            uncertain_pred += 1
            continue
        if exp == Verdict.UNCERTAIN.value:
            # label uncertain but model decisive — not FP/FN for consistency metrics
            continue
        decisive += 1
        if exp == Verdict.INCONSISTENT.value and pred == Verdict.INCONSISTENT.value:
            tp += 1
        elif exp == Verdict.CONSISTENT.value and pred == Verdict.CONSISTENT.value:
            tn += 1
        elif exp == Verdict.CONSISTENT.value and pred == Verdict.INCONSISTENT.value:
            fp += 1
        elif exp == Verdict.INCONSISTENT.value and pred == Verdict.CONSISTENT.value:
            fn += 1
        else:
            if exp == pred:
                tn += 1
            elif exp == Verdict.CONSISTENT.value:
                fp += 1
            else:
                fn += 1

    consistent_labeled_decisive = tn + fp
    fpr = (fp / consistent_labeled_decisive) if consistent_labeled_decisive else 0.0

    inconsistent_labeled_decisive = tp + fn
    fnr = (fn / inconsistent_labeled_decisive) if inconsistent_labeled_decisive else 0.0
    miss_rate = (n_missed / n_exp_inc) if n_exp_inc else 0.0

    # Coverage: decisive among non-skipped labeled pairs
    pair_coverage = (decisive / active_pairs) if active_pairs else 0.0
    mean_app_cov = sum(app_coverages) / len(app_coverages) if app_coverages else 0.0
    coverage = pair_coverage

    accuracy = ((tp + tn) / decisive) if decisive else 0.0

    thr = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    cov_min = float(thr["coverage_min"])
    fpr_max = float(thr["false_positive_rate_max"])
    fnr_max = float(thr["false_negative_rate_max"])
    miss_max = float(thr["miss_rate_max"])

    warns: list[str] = []
    if inconsistent_labeled_decisive < 15:
        warns.append(
            f"n_inconsistent_labeled_decisive={inconsistent_labeled_decisive} < 15; "
            "FNR estimate is statistically thin"
        )
    if active_pairs < 30:
        warns.append(f"active_pairs={active_pairs} is small; expand fixtures for stronger claims")
    if n_exp_inc and miss_rate > miss_max:
        warns.append(
            f"miss_rate={miss_rate:.4f} > {miss_max}: expected inconsistent hidden as "
            "consistent/uncertain"
        )

    for w in warns:
        warnings.warn(w, stacklevel=2)

    return Metrics(
        coverage=round(coverage, 4),
        false_positive_rate=round(fpr, 4),
        false_negative_rate=round(fnr, 4),
        accuracy=round(accuracy, 4),
        total_pairs=active_pairs,
        decisive_pairs=decisive,
        true_positive=tp,
        true_negative=tn,
        false_positive=fp,
        false_negative=fn,
        uncertain_when_labeled=uncertain_pred,
        mean_app_coverage=round(mean_app_cov, 4),
        n_inconsistent_labeled_decisive=inconsistent_labeled_decisive,
        miss_rate=round(miss_rate, 4),
        n_expected_inconsistent=n_exp_inc,
        n_missed_inconsistent=n_missed,
        warnings=warns + ([f"skipped_pairs={skipped_pairs}"] if skipped_pairs else []),
        pairs=[asdict(p) for p in pairs],
        pass_thresholds={
            f"coverage>={cov_min}": coverage >= cov_min,
            f"false_positive_rate<={fpr_max}": fpr <= fpr_max,
            f"false_negative_rate<={fnr_max}": fnr <= fnr_max,
            f"miss_rate<={miss_max}": miss_rate <= miss_max,
        },
        metric_definitions={
            "coverage": "decisive_pairs / active_labeled_pairs (excl. skipped)",
            "false_positive_rate": "FP / (TN+FP); expected=consistent, predicted decisive",
            "false_negative_rate": "FN / (TP+FN); expected=inconsistent, predicted decisive",
            "miss_rate": (
                "among expected=inconsistent: fraction predicted in "
                "{consistent, uncertain} (includes uncertain hide); "
                f"gated by miss_rate_max={miss_max}"
            ),
            "mean_app_coverage": "mean of per-application report coverage (info only)",
        },
    )


def _field_source_from_data(data: dict[str, Any]) -> Any:
    meta = data.get("meta")
    if isinstance(meta, dict) and "field_source" in meta:
        return meta.get("field_source")
    return data.get("field_source")


def _collect_json_files(dirs: list[Path]) -> list[Path]:
    """Stable sorted fixture list (Round28: deterministic across suite=all)."""
    files: list[Path] = []
    for d in sorted(dirs, key=lambda p: str(p)):
        if not d.is_dir():
            continue
        files.extend(sorted(d.glob("*.json")))
    # final sort by path string so multi-root order is name-stable
    return sorted(files, key=lambda p: str(p))


def evaluate_directory(
    apps_dir: str | Path,
    rules: RuleConfig | str | Path,
    labels_path: str | Path | None = None,
    *,
    thresholds: dict[str, float] | None = None,
    suite: str = "main",
    mode: str | None = None,
) -> Metrics:
    """Evaluate one directory. Prefer evaluate_suite() for Round19 multi-root."""
    return evaluate_paths(
        [Path(apps_dir)],
        rules,
        labels_path=labels_path,
        thresholds=thresholds,
        suite=suite,
        mode=mode,
    )


def evaluate_paths(
    apps_dirs: list[Path],
    rules: RuleConfig | str | Path,
    labels_path: str | Path | None = None,
    *,
    thresholds: dict[str, float] | None = None,
    suite: str = "main",
    mode: str | None = None,
) -> Metrics:
    if not isinstance(rules, RuleConfig):
        rules = load_rules(rules)
    engine = RuleEngine(rules)

    labels: dict[str, Any] | None = None
    if labels_path is not None:
        labels = load_labels(labels_path)
    else:
        # first existing labels next to each apps dir's parent/labels
        for apps_dir in apps_dirs:
            candidate = apps_dir.parent / "labels" / "expected_verdicts.json"
            if candidate.exists():
                labels = load_labels(candidate)
                break
            # semi-specific labels path
            semi_labels = apps_dir / "labels" / "expected_verdicts.json"
            if semi_labels.exists():
                labels = load_labels(semi_labels)
                break

    files = _collect_json_files(apps_dirs)
    all_pairs: list[EvalPair] = []
    coverages: list[float] = []
    per_app: dict[str, Any] = {}
    skipped: list[str] = []
    fs_warn: list[str] = []
    n_ok = 0
    n_fail = 0
    verdict_counts: dict[str, int] = {
        "consistent": 0,
        "inconsistent": 0,
        "uncertain": 0,
        "skipped": 0,
    }
    labeled_files = 0

    for fp in files:
        try:
            with fp.open("r", encoding="utf-8") as f:
                data = json.load(f)
            app = Application.from_dict(data)
            report = engine.run(app)
            n_ok += 1
            s = report.summary
            verdict_counts["consistent"] += s.consistent
            verdict_counts["inconsistent"] += s.inconsistent
            verdict_counts["uncertain"] += s.uncertain
            verdict_counts["skipped"] += s.skipped
        except Exception as e:
            n_fail += 1
            per_app[str(fp)] = {"file": str(fp), "error": str(e)}
            continue

        fs = _field_source_from_data(data)
        if fs is None and suite == "main":
            fs_warn.append(fp.name)

        # honesty: step2 synthetic must not claim external_ocr
        nested_meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        src = nested_meta.get("source") or data.get("source")
        if src == "semi_real_step2" and fs == "external_ocr":
            warnings.warn(
                f"{fp}: source=semi_real_step2 cannot use field_source=external_ocr",
                stacklevel=2,
            )

        expected = extract_expected_verdicts(data, labels)
        if not expected:
            skipped.append(str(fp))
            per_app[app.application_id] = {
                "file": str(fp),
                "summary": report.summary.to_dict(),
                "expected": {},
                "predicted": {c.rule_id: c.verdict.value for c in report.checks},
                "field_source": fs,
            }
            continue
        labeled_files += 1
        pairs, info = evaluate_report(report, expected)
        all_pairs.extend(pairs)
        coverages.append(report.summary.coverage)
        per_app[app.application_id] = {
            "file": str(fp),
            "summary": report.summary.to_dict(),
            "expected": expected,
            "predicted": info["predicted"],
            "field_source": fs,
        }

    n_loaded = n_ok + n_fail
    total_verdicts = sum(verdict_counts.values()) or 1
    unc_rate = verdict_counts["uncertain"] / total_verdicts

    # mode selection
    if mode is None:
        mode = "labeled" if labeled_files > 0 else "smoke"
    mode = str(mode).lower()
    if mode not in {"labeled", "smoke"}:
        raise ValueError("mode must be labeled|smoke")

    if mode == "smoke" or labeled_files == 0:
        # smoke: no FP/FN claims
        metrics = Metrics(
            coverage=0.0,
            false_positive_rate=0.0,
            false_negative_rate=0.0,
            accuracy=0.0,
            total_pairs=0,
            decisive_pairs=0,
            true_positive=0,
            true_negative=0,
            false_positive=0,
            false_negative=0,
            uncertain_when_labeled=0,
            mean_app_coverage=0.0,
            n_inconsistent_labeled_decisive=0,
            miss_rate=0.0,
            n_expected_inconsistent=0,
            n_missed_inconsistent=0,
            warnings=[
                f"smoke_mode: labeled_files={labeled_files}; FP/FN not computed",
            ]
            + ([f"unlabeled_fixtures={len(skipped)}"] if skipped else []),
            pairs=[],
            per_application=per_app,
            pass_thresholds={"smoke_load_ok": n_fail == 0},
            metric_definitions={
                "mode": "smoke — n_apps_loaded / verdict_counts / uncertain_rate only",
                "forbidden": "Do not claim coverage/FPR/FNR/miss_rate from smoke",
            },
            suite=suite,
            mode="smoke",
            honesty_note=HONESTY_SEMI_SMOKE if suite in {"semi", "all"} else HONESTY_MAIN,
            n_apps_loaded=n_loaded,
            n_check_ok=n_ok,
            n_check_fail=n_fail,
            verdict_counts=verdict_counts,
            uncertain_rate=round(unc_rate, 4),
        )
        if not files:
            metrics.warnings = list(metrics.warnings) + ["no_json_fixtures_in_suite_dirs"]
        return metrics

    metrics = compute_metrics(all_pairs, coverages, thresholds=thresholds)
    metrics.per_application = per_app
    metrics.suite = suite
    metrics.mode = "labeled"
    metrics.n_apps_loaded = n_loaded
    metrics.n_check_ok = n_ok
    metrics.n_check_fail = n_fail
    metrics.verdict_counts = verdict_counts
    metrics.uncertain_rate = round(unc_rate, 4)
    if suite == "main":
        metrics.honesty_note = HONESTY_MAIN
    elif suite == "semi":
        metrics.honesty_note = HONESTY_SEMI_LABELED
    else:
        metrics.honesty_note = (
            "suite=all is debug-only; delivery claims must use suite=main alone. "
            + HONESTY_MAIN
        )
    extra_warns: list[str] = []
    if skipped:
        extra_warns.append(f"skipped_unlabeled_fixtures={len(skipped)}: {skipped[:5]}")
    if fs_warn:
        extra_warns.append(
            f"missing_field_source_warning n={len(fs_warn)} (compat; not fail) e.g. {fs_warn[:3]}"
        )
    if suite == "all":
        extra_warns.append("suite=all debug-only; do not use as official delivery number")
    if extra_warns:
        metrics.warnings = list(metrics.warnings) + extra_warns
    return metrics


def evaluate_suite(
    suite: str = "main",
    rules: RuleConfig | str | Path = "",
    *,
    labels_path: str | Path | None = None,
    thresholds: dict[str, float] | None = None,
    mode: str | None = None,
    apps_dirs: list[Path] | None = None,
) -> Metrics:
    """Evaluate by suite name (main|semi|all). Default main."""
    suite = (suite or "main").lower()
    if suite not in SUITE_DIRS:
        raise ValueError(f"unknown suite {suite!r}; allowed={sorted(SUITE_DIRS)}")
    dirs = apps_dirs if apps_dirs is not None else list(SUITE_DIRS[suite])
    return evaluate_paths(
        dirs,
        rules,
        labels_path=labels_path,
        thresholds=thresholds,
        suite=suite,
        mode=mode,
    )


def write_metrics(metrics: Metrics, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metrics.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def metrics_to_html(metrics: Metrics) -> str:
    """Batch evaluation HTML: KPI summary + per-app failure list."""
    import html as html_mod

    thr_rows = "".join(
        f"<tr><td>{html_mod.escape(k)}</td>"
        f"<td style='color:{'green' if v else 'red'}'><b>{'PASS' if v else 'FAIL'}</b></td></tr>"
        for k, v in (metrics.pass_thresholds or {}).items()
    )
    kpi = f"""
    <div class="kpis">
      <div class="kpi">coverage<b>{metrics.coverage:.2%}</b></div>
      <div class="kpi">FPR<b>{metrics.false_positive_rate:.2%}</b></div>
      <div class="kpi">FNR<b>{metrics.false_negative_rate:.2%}</b></div>
      <div class="kpi">miss_rate<b>{metrics.miss_rate:.2%}</b></div>
      <div class="kpi">TP/TN/FP/FN
        <b>{metrics.true_positive}/{metrics.true_negative}/{metrics.false_positive}/{metrics.false_negative}</b>
      </div>
      <div class="kpi">pairs<b>{metrics.total_pairs}</b></div>
      <div class="kpi">n_inc_decisive<b>{metrics.n_inconsistent_labeled_decisive}</b></div>
    </div>
    """
    fails: list[str] = []
    for app_id, info in sorted((metrics.per_application or {}).items()):
        exp = info.get("expected") or {}
        pred = info.get("predicted") or {}
        bad = []
        for rid, e in exp.items():
            p = pred.get(rid, "uncertain")
            if e != p:
                bad.append(f"{rid}: expected={e} predicted={p}")
            elif e == "inconsistent" and p == "inconsistent":
                bad.append(f"{rid}: inconsistent (hit)")
        # only list apps with problems or critical hits for review
        problem = [
            f"{rid}: expected={e} predicted={pred.get(rid)}"
            for rid, e in exp.items()
            if e != pred.get(rid)
        ]
        if problem:
            lis = "".join(f"<li>{html_mod.escape(x)}</li>" for x in problem)
            fails.append(
                f"<details open><summary><b>{html_mod.escape(app_id)}</b> "
                f"({html_mod.escape(str(info.get('file','')))})</summary>"
                f"<ul>{lis}</ul></details>"
            )
    fail_block = (
        "".join(fails) if fails else "<p class='ok'>无 expected/predicted 不一致项</p>"
    )
    warns = "".join(f"<li>{html_mod.escape(w)}</li>" for w in (metrics.warnings or []))
    warn_block = f"<ul>{warns}</ul>" if warns else "<p class='muted'>无</p>"
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<title>Evaluate Metrics Summary</title>
<style>
body{{font-family:system-ui,sans-serif;margin:24px}}
.kpis{{display:flex;flex-wrap:wrap;gap:12px}}
.kpi{{background:#f8f9fa;border:1px solid #e9ecef;border-radius:8px;padding:10px 14px;min-width:110px}}
.kpi b{{display:block;font-size:1.2rem;margin-top:4px}}
table{{border-collapse:collapse;margin:16px 0}}
td,th{{border:1px solid #ccc;padding:6px 10px}}
.ok{{color:green}}.muted{{color:#888}}
details{{margin:8px 0;padding:8px;background:#fafafa;border:1px solid #eee}}
</style></head><body>
<h1>Batch Evaluate Summary</h1>
{kpi}
<h2>Thresholds</h2>
<table><tr><th>gate</th><th>result</th></tr>{thr_rows}</table>
<h2>Per-app mismatches (expected ≠ predicted)</h2>
{fail_block}
<h2>Warnings</h2>
{warn_block}
<p class="muted">task4_consistency evaluate · Round7</p>
</body></html>
"""


def write_metrics_html(metrics: Metrics, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(metrics_to_html(metrics), encoding="utf-8")
