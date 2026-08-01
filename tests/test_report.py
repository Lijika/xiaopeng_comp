from task4_consistency.models import CheckResult, Severity, Verdict
from task4_consistency.report import build_report, first_diff, report_to_html, report_to_markdown


def test_first_diff():
    d = first_diff("ABC", "ADC")
    assert d.pos == 1
    assert d.left == "B"
    assert d.right == "D"


def test_build_report_coverage():
    checks = [
        CheckResult("R1", "n1", Verdict.CONSISTENT, Severity.MAJOR, "ok"),
        CheckResult("R2", "n2", Verdict.INCONSISTENT, Severity.CRITICAL, "bad"),
        CheckResult("R3", "n3", Verdict.UNCERTAIN, Severity.MINOR, "??"),
        CheckResult("R4", "n4", Verdict.SKIPPED, Severity.INFO, "skip"),
    ]
    report = build_report("APP", checks)
    assert report.summary.total == 3  # excl skipped
    assert report.summary.skipped == 1
    assert report.summary.total_including_skipped == 4
    assert report.summary.consistent == 1
    assert report.summary.inconsistent == 1
    assert report.summary.uncertain == 1
    assert abs(report.summary.coverage - round(2 / 3, 4)) < 1e-9
    md = report_to_markdown(report)
    assert "APP" in md
    assert "skipped" in md
    html = report_to_html(report)
    assert "APP" in html
