import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_cli_check_and_evaluate(tmp_path):
    out_report = tmp_path / "report.json"
    out_metrics = tmp_path / "metrics.json"
    check = subprocess.run(
        [
            sys.executable,
            "-m",
            "task4_consistency.cli",
            "check",
            str(ROOT / "fixtures/applications/app_consistent_01.json"),
            "-c",
            str(ROOT / "configs/rules_auto_lease.yaml"),
            "-o",
            str(out_report),
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.returncode == 0, check.stderr
    report = json.loads(out_report.read_text(encoding="utf-8"))
    assert report["application_id"] == "APP-OK-001"
    assert report["summary"]["inconsistent"] == 0

    ev = subprocess.run(
        [
            sys.executable,
            "-m",
            "task4_consistency.cli",
            "evaluate",
            str(ROOT / "fixtures/applications"),
            "-c",
            str(ROOT / "configs/rules_auto_lease.yaml"),
            "-o",
            str(out_metrics),
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert ev.returncode == 0, ev.stderr + ev.stdout
    metrics = json.loads(out_metrics.read_text(encoding="utf-8"))
    assert metrics["coverage"] >= 0.8


def test_cli_evaluate_labels_c_dev_reg(tmp_path) -> None:
    """The CLI evaluate output is explicitly C-DEV-REG and carries no formal
    THRESHOLD PASS claim."""
    import subprocess
    import sys

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "task4_consistency",
            "evaluate",
            "-c",
            str(ROOT / "configs" / "rules_auto_lease.yaml"),
            "--suite",
            "main",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert "C-DEV-REG" in completed.stderr
    assert "THRESHOLD PASS" not in completed.stderr
    assert "THRESHOLD PASS" not in completed.stdout
