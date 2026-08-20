"""S12 restricted evaluation runner.

The S12 evaluation worker materializes the plan-frozen checker purely from
the canonical artifact and the frozen RunSpecs passed over stdin, executes
the existing pure ``TargetChecker.run`` per application, and prints one
canonical prediction result digest.  The subprocess performs no file I/O,
receives no gold labels, no business database path and no credential, and
never touches any mutable singleton, so repeated runs prove checker
determinism and evaluation isolation.
"""

from __future__ import annotations

import hashlib
import json
import resource
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from task4_consistency.controlled.s01_checker import (
    TargetChecker,
    TargetRelease,
)

# G4 resource boundary: the fresh-process runner reads at most one bounded
# stdin payload, executes the checker under a declared runtime deadline and
# emits one canonical small result digest.  A finite memory/process boundary
# is enforced up front so a malformed child cannot grow without limit.
_MAX_STDIN_BYTES = 64 * 1024 * 1024
_MAX_OUTPUT_BYTES = 32 * 1024 * 1024
_MAX_REASON_LENGTH = 512
_MEMORY_LIMIT_BYTES = 512 * 1024 * 1024
_CPU_LIMIT_SECONDS = 60

RUNNER_REQUEST_SCHEMA = "s12-runner-request/1"
RUNNER_RESULT_SCHEMA = "s12-runner-result/1"

_MAX_PARENT_STDOUT_BYTES = 32 * 1024 * 1024
_MAX_PARENT_STDERR_BYTES = 1 * 1024 * 1024
_PARENT_TIMEOUT_SECONDS = 120


def _apply_process_boundaries() -> None:
    try:
        resource.setrlimit(
            resource.RLIMIT_AS,
            (_MEMORY_LIMIT_BYTES, _MEMORY_LIMIT_BYTES),
        )
        resource.setrlimit(
            resource.RLIMIT_CPU,
            (_CPU_LIMIT_SECONDS, _CPU_LIMIT_SECONDS + 1),
        )
    except (ValueError, OSError):
        pass


def _fail(message: str) -> int:
    sys.stderr.write(message[: _MAX_REASON_LENGTH] + "\n")
    return 2


def _deadline_expired(_signum: int, _frame: Any) -> None:
    raise TimeoutError("checker run deadline exceeded")


def _application_checks(
    release: TargetRelease,
    run_spec: dict[str, Any],
    max_runtime_ms: int,
) -> dict[str, Any]:
    """One frozen RunSpec -> the pure checker's checks, or a closed per-app
    error outcome.  A checker failure for one known application must not
    collapse the whole run: the parent materializes ``error`` per known
    opportunity of that application."""
    application_id = str(run_spec.get("application_id") or "")
    try:
        previous = signal.signal(signal.SIGALRM, _deadline_expired)
        signal.setitimer(signal.ITIMER_REAL, max_runtime_ms / 1000.0)
        try:
            result = TargetChecker(release).run(run_spec)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
    except TimeoutError:
        return {
            "application_id": application_id,
            "run_id": str(run_spec.get("run_id") or ""),
            "error": "CHECKER_DEADLINE_EXCEEDED",
        }
    except (TypeError, ValueError, KeyError):
        return {
            "application_id": application_id,
            "run_id": str(run_spec.get("run_id") or ""),
            "error": "CHECKER_EXECUTION_FAILED",
        }
    return {
        "application_id": application_id,
        "run_id": str(run_spec.get("run_id") or ""),
        "checks": [
            {
                "rule_id": check.rule_id,
                "verdict": (
                    check.verdict.value
                    if hasattr(check.verdict, "value")
                    else str(check.verdict)
                ),
                "severity": check.severity,
                "reason_codes": list(check.reason_codes or ()),
            }
            for check in result.checks
        ],
    }


def main() -> int:
    _apply_process_boundaries()
    stdin_bytes = sys.stdin.buffer.read(_MAX_STDIN_BYTES + 1)
    if len(stdin_bytes) > _MAX_STDIN_BYTES:
        return _fail("evaluation payload exceeds the input byte limit")
    try:
        payload = json.loads(stdin_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _fail("evaluation payload is not valid JSON")
    if not isinstance(payload, dict):
        return _fail("evaluation payload is not an object")
    if payload.get("schema_version") != RUNNER_REQUEST_SCHEMA:
        return _fail("evaluation payload schema mismatch")
    try:
        release = TargetRelease.from_artifact(payload["checker_artifact"])
    except (KeyError, TypeError, ValueError):
        return _fail("checker artifact is not materializable")
    run_specs = payload.get("run_specs")
    if not isinstance(run_specs, dict) or not run_specs:
        return _fail("run_specs are missing or invalid")
    budget = payload.get("budget")
    if (
        not isinstance(budget, dict)
        or not isinstance(budget.get("max_runtime_ms"), int)
        or budget["max_runtime_ms"] <= 0
    ):
        return _fail("plan budget max_runtime_ms is missing or invalid")
    # The frozen plan budget governs the runner deadline, never the release
    # default: a plan freezes its own budget and the run must honor it.
    max_runtime_ms = int(budget["max_runtime_ms"])
    if max_runtime_ms <= 0:
        return _fail("checker max_runtime_ms limit is invalid")

    applications: list[dict[str, Any]] = []
    seen: set[str] = set()
    for application_id, run_spec in run_specs.items():
        if not isinstance(application_id, str) or not application_id:
            return _fail("run_spec application identity is invalid")
        if application_id in seen:
            return _fail("run_spec application identity is duplicated")
        seen.add(application_id)
        if not isinstance(run_spec, dict):
            return _fail("run_spec payload is not an object")
        if str(run_spec.get("application_id") or "") != application_id:
            return _fail("run_spec application identity mismatch")
        applications.append(
            _application_checks(release, run_spec, max_runtime_ms)
        )

    material = {
        "schema_version": RUNNER_RESULT_SCHEMA,
        "applications": applications,
    }
    digest = hashlib.sha256(
        json.dumps(
            material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    output = json.dumps({"digest": digest, **material})
    if len(output.encode("utf-8")) > _MAX_OUTPUT_BYTES:
        return _fail("evaluation output exceeds the output byte limit")
    sys.stdout.write(output)
    return 0


def run_s12_runner(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Parent-side restricted subprocess launcher.  The child receives only
    the canonical payload over stdin (checker artifact + frozen RunSpecs --
    never gold labels, business paths or credentials), runs under an
    allowlisted environment, bounded streams and a hard timeout, and emits
    one canonical JSON result.  Any boundary violation yields ``None``."""
    payload_bytes = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(payload_bytes) > 64 * 1024 * 1024:
        return None
    module_root = Path(__file__).resolve().parents[2]
    env = {
        "PYTHONDONTWRITEBYTECODE": "1",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    proc: subprocess.Popen[bytes] | None = None
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "task4_consistency.controlled.s12_runner"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(module_root),
            env=env,
        )
    except OSError:
        return None
    assert proc.stdout is not None and proc.stderr is not None
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    counters = {"stdout": 0, "stderr": 0}
    stream_lock = threading.Lock()

    def _drain(stream: Any, target: list[bytes], counter: str) -> None:
        limit = (
            _MAX_PARENT_STDOUT_BYTES
            if counter == "stdout"
            else _MAX_PARENT_STDERR_BYTES
        )
        try:
            for chunk in iter(lambda: stream.read(65536), b""):
                with stream_lock:
                    counters[counter] += len(chunk)
                    if counters[counter] > limit:
                        try:
                            proc.kill()
                        except OSError:
                            pass
                        return
                    target.append(chunk)
        finally:
            try:
                stream.close()
            except OSError:
                pass

    stdout_thread = threading.Thread(
        target=_drain,
        args=(proc.stdout, stdout_chunks, "stdout"),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_drain,
        args=(proc.stderr, stderr_chunks, "stderr"),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    try:
        proc.stdin.write(payload_bytes)
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    try:
        proc.wait(timeout=_PARENT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        stdout_thread.join(timeout=10)
        stderr_thread.join(timeout=10)
        return None
    stdout_thread.join(timeout=10)
    stderr_thread.join(timeout=10)
    if stdout_thread.is_alive() or stderr_thread.is_alive():
        try:
            proc.kill()
        except OSError:
            pass
        return None
    with stream_lock:
        stdout_overflow = counters["stdout"] > _MAX_PARENT_STDOUT_BYTES
        stderr_overflow = counters["stderr"] > _MAX_PARENT_STDERR_BYTES
    if proc.returncode != 0 or stdout_overflow or stderr_overflow:
        return None
    try:
        return json.loads(b"".join(stdout_chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


if __name__ == "__main__":
    raise SystemExit(main())
