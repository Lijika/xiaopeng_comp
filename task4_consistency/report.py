"""Report builders and diff helpers."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from task4_consistency.models import (
    CheckResult,
    DiffHighlight,
    Report,
    ReportSummary,
    Verdict,
)


def first_diff(left: str, right: str) -> DiffHighlight:
    n = min(len(left), len(right))
    for i in range(n):
        if left[i] != right[i]:
            return DiffHighlight(pos=i, left=left[i], right=right[i], detail=f"diff at index {i}")
    if len(left) != len(right):
        return DiffHighlight(
            pos=n,
            left=left[n:] if len(left) > n else "",
            right=right[n:] if len(right) > n else "",
            detail="length mismatch",
        )
    return DiffHighlight(pos=None, left=left, right=right, detail="no char diff")


def build_report(
    application_id: str,
    checks: list[CheckResult],
    rule_config_version: str | int | None = None,
    rule_package: str | None = None,
    rule_changelog: list[str] | None = None,
) -> Report:
    consistent = sum(1 for c in checks if c.verdict == Verdict.CONSISTENT)
    inconsistent = sum(1 for c in checks if c.verdict == Verdict.INCONSISTENT)
    uncertain = sum(1 for c in checks if c.verdict == Verdict.UNCERTAIN)
    skipped = sum(1 for c in checks if c.verdict == Verdict.SKIPPED)
    active_total = consistent + inconsistent + uncertain
    decisive = consistent + inconsistent
    coverage = (decisive / active_total) if active_total else 0.0
    summary = ReportSummary(
        consistent=consistent,
        inconsistent=inconsistent,
        uncertain=uncertain,
        skipped=skipped,
        coverage=round(coverage, 4),
        total=active_total,
        total_including_skipped=len(checks),
    )
    return Report(
        application_id=application_id,
        summary=summary,
        checks=checks,
        rule_config_version=rule_config_version,
        rule_package=rule_package,
        rule_changelog=rule_changelog,
    )


def report_to_json(report: Report, indent: int = 2) -> str:
    return json.dumps(report.to_dict(), ensure_ascii=False, indent=indent)


def write_report(report: Report, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report_to_json(report), encoding="utf-8")


def report_to_markdown(report: Report) -> str:
    lines = [
        f"# Consistency Report — {report.application_id}",
        "",
        f"- consistent: **{report.summary.consistent}**",
        f"- inconsistent: **{report.summary.inconsistent}**",
        f"- uncertain: **{report.summary.uncertain}**",
        f"- skipped: **{report.summary.skipped}**",
        f"- coverage: **{report.summary.coverage:.2%}** (excl. skipped)",
        "",
        "## Checks",
        "",
    ]
    icons = {
        "consistent": "OK",
        "inconsistent": "FAIL",
        "uncertain": "??",
        "skipped": "SKIP",
    }
    for c in report.checks:
        icon = icons.get(c.verdict.value, "?")
        lines.append(f"### [{icon}] {c.rule_id} — {c.name}")
        lines.append(f"- verdict: `{c.verdict.value}` / severity: `{c.severity.value}`")
        if c.flags:
            lines.append(f"- flags: `{', '.join(c.flags)}`")
        lines.append(f"- {c.message}")
        if c.snapshots:
            lines.append("- snapshots:")
            for s in c.snapshots:
                extra = ""
                if s.ocr_fix:
                    extra += " ocr_fix"
                if s.notes:
                    extra += f" notes={s.notes}"
                lines.append(
                    f"  - {s.doc_type}/{s.field}: raw=`{s.raw}` norm=`{s.normalized}` "
                    f"conf={s.confidence}{extra}"
                )
        if c.diff_highlight:
            dh = c.diff_highlight
            lines.append(f"- diff: pos={dh.pos} left=`{dh.left}` right=`{dh.right}`")
        lines.append("")
    return "\n".join(lines)


def report_to_html(report: Report) -> str:
    """Self-contained HTML report with severity groups and flags."""
    s = report.summary
    color = {
        "consistent": "#d4edda",
        "inconsistent": "#f8d7da",
        "uncertain": "#fff3cd",
        "skipped": "#e2e3e5",
    }

    def flags_html(c: CheckResult) -> str:
        if not c.flags:
            return ""
        return " ".join(
            f"<span class='flag'>{html.escape(f)}</span>" for f in c.flags
        )

    def row(c: CheckResult) -> str:
        bg = color.get(c.verdict.value, "#fff")
        snaps = "<br/>".join(
            html.escape(
                f"{x.doc_type}/{x.field}: raw={x.raw!r} norm={x.normalized!r} "
                f"conf={x.confidence}"
                + (" ocr_fix" if x.ocr_fix else "")
                + (f" notes={x.notes}" if x.notes else "")
            )
            for x in c.snapshots
        )
        diff = ""
        if c.diff_highlight:
            d = c.diff_highlight
            diff = html.escape(f"pos={d.pos} left={d.left!r} right={d.right!r}")
        return (
            f"<tr style='background:{bg}'>"
            f"<td>{html.escape(c.rule_id)}</td>"
            f"<td>{html.escape(c.name)}</td>"
            f"<td><b>{html.escape(c.verdict.value)}</b> {flags_html(c)}</td>"
            f"<td>{html.escape(c.severity.value)}</td>"
            f"<td>{html.escape(c.message)}</td>"
            f"<td>{snaps}</td>"
            f"<td>{diff}</td>"
            f"</tr>"
        )

    critical_fail = [
        c
        for c in report.checks
        if c.verdict == Verdict.INCONSISTENT and c.severity.value == "critical"
    ]
    major_fail = [
        c
        for c in report.checks
        if c.verdict == Verdict.INCONSISTENT and c.severity.value == "major"
    ]
    uncertain_crit = [
        c
        for c in report.checks
        if c.verdict == Verdict.UNCERTAIN
        and c.severity.value in ("critical", "major")
    ]
    low_conf = [c for c in report.checks if "low_conf" in (c.flags or [])]

    def list_block(title: str, items: list[CheckResult]) -> str:
        if not items:
            return f"<h3>{html.escape(title)}</h3><p class='muted'>无</p>"
        lis = "".join(
            f"<li><b>{html.escape(c.rule_id)}</b> "
            f"[{html.escape(c.verdict.value)}] {html.escape(c.message)} "
            f"{flags_html(c)}</li>"
            for c in items
        )
        return f"<h3>{html.escape(title)}</h3><ul>{lis}</ul>"

    body_groups = (
        list_block("Critical 不一致（必须处理）", critical_fail)
        + list_block("Major 不一致", major_fail)
        + list_block("Critical/Major 存疑", uncertain_crit)
        + list_block("低置信度相关", low_conf)
    )

    rows = "".join(row(c) for c in report.checks)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<title>Consistency Report — {html.escape(report.application_id)}</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 24px; color: #222; }}
table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
th, td {{ border: 1px solid #ccc; padding: 6px 8px; vertical-align: top; }}
th {{ background: #f0f0f0; position: sticky; top: 0; }}
.summary {{ display: flex; flex-wrap: wrap; gap: 12px 20px; margin: 12px 0 20px; }}
.summary .kpi {{ background: #f8f9fa; border: 1px solid #e9ecef; border-radius: 8px;
  padding: 8px 12px; min-width: 100px; }}
.summary .kpi b {{ display: block; font-size: 1.25rem; }}
.flag {{ display: inline-block; background: #6c757d; color: #fff; border-radius: 4px;
  padding: 1px 6px; font-size: 11px; margin-left: 4px; }}
.flag:first-child {{ margin-left: 0; }}
.muted {{ color: #888; }}
h2 {{ margin-top: 28px; border-bottom: 1px solid #ddd; padding-bottom: 4px; }}
h3 {{ margin-top: 16px; font-size: 1rem; }}
</style>
</head>
<body>
<h1>Consistency Report — {html.escape(report.application_id)}</h1>
<div class="summary">
  <div class="kpi">consistent<b>{s.consistent}</b></div>
  <div class="kpi">inconsistent<b>{s.inconsistent}</b></div>
  <div class="kpi">uncertain<b>{s.uncertain}</b></div>
  <div class="kpi">skipped<b>{s.skipped}</b></div>
  <div class="kpi">coverage<b>{s.coverage:.1%}</b></div>
  <div class="kpi">config<b>{html.escape(str(report.rule_config_version))}</b></div>
</div>
<h2>复核行动清单</h2>
{body_groups}
<h2>全部检查</h2>
<table>
<thead><tr>
<th>rule_id</th><th>name</th><th>verdict / flags</th><th>severity</th>
<th>message</th><th>snapshots</th><th>diff</th>
</tr></thead>
<tbody>
{rows}
</tbody>
</table>
<p class="muted">生成自 task4_consistency · 合成/OCR 字段输入 · 见 README 商业边界</p>
</body>
</html>
"""


def write_html_report(report: Report, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report_to_html(report), encoding="utf-8")


def load_application_json(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)
