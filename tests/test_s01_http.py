"""S01 public HTTP contract tests against a real uvicorn loopback process.

TestClient is deliberately not the acceptance seam here.  The app's historical
in-process ASGI transport can stall in the default execution sandbox, while
the target contract is an actual HTTP adapter.
"""

from __future__ import annotations

import http.client
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Iterator

import pytest

from task4_consistency.controlled.s01 import S01CommandPrincipal


ROOT = Path(__file__).resolve().parents[1]
SCENARIO = "app_r53_bad_engine.json"
DEMO_CREDENTIAL = "s01-registered-demo-test-credential"
OPERATOR_CREDENTIAL = "s01-registered-operator-test-credential"
AUDITOR_CREDENTIAL = "s01-registered-auditor-test-credential"
_SERVER_START_TIMEOUT_SECONDS = 5.0
_REQUEST_TIMEOUT_SECONDS = 2.0
_SERVER_STOP_TIMEOUT_SECONDS = 5.0
TEST_INTEGRATOR = S01CommandPrincipal(
    subject="registered-test-integrator",
    role="integrator",
    scope="C-DEMO",
    source_id="s01-test-client",
)


@dataclass(frozen=True)
class LoopbackResponse:
    status: int
    headers: dict[str, str]
    text: str

    def json(self) -> dict[str, Any]:
        value = json.loads(self.text)
        assert isinstance(value, dict)
        return value


class UvicornLoopback:
    """Bounded real-socket harness for the S01 public HTTP seam."""

    def __init__(
        self,
        extra_env: dict[str, str] | None = None,
        *,
        app_target: str = "task4_consistency.web.app:app",
        app_factory: bool = False,
    ) -> None:
        self._extra_env = dict(extra_env or {})
        self._extra_env.setdefault("TASK4_S01_DEMO_CREDENTIAL", DEMO_CREDENTIAL)
        self._extra_env.setdefault("TASK4_S01_DEMO_SUBJECT", "c-demo-test-user")
        self._extra_env.setdefault("TASK4_S01_OPERATOR_CREDENTIAL", OPERATOR_CREDENTIAL)
        self._extra_env.setdefault("TASK4_S01_OPERATOR_SUBJECT", "c-demo-test-operator")
        self._extra_env.setdefault("TASK4_S01_AUDITOR_CREDENTIAL", AUDITOR_CREDENTIAL)
        self._extra_env.setdefault("TASK4_S01_AUDITOR_SUBJECT", "c-demo-test-auditor")
        self._app_target = app_target
        self._app_factory = app_factory
        if "TASK4_S01_STATE_PATH" not in self._extra_env:
            self._extra_env["TASK4_S01_STATE_PATH"] = str(
                Path(tempfile.mkdtemp(prefix="xiaopeng-s01-http-"))
                / "target.sqlite3"
            )
        self._port = 0
        self._process: subprocess.Popen[str] | None = None
        self._session_cookie: str | None = None

    @property
    def session_cookie(self) -> str | None:
        """The captured ``s01_session`` cookie, if a session was opened."""
        return self._session_cookie

    def __enter__(self) -> "UvicornLoopback":
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            self._port = int(reservation.getsockname()[1])

        env = os.environ.copy()
        env.update(self._extra_env)
        env["NO_PROXY"] = "127.0.0.1,localhost"
        env["no_proxy"] = "127.0.0.1,localhost"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        command = [
            sys.executable,
            "-m",
            "uvicorn",
            self._app_target,
            "--host",
            "127.0.0.1",
            "--port",
            str(self._port),
            "--log-level",
            "warning",
        ]
        if self._app_factory:
            command.append("--factory")
        self._process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.monotonic() + _SERVER_START_TIMEOUT_SECONDS
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                break
            try:
                response = self.request("GET", "/api/health")
                if response.status == 200:
                    return self
            except (ConnectionError, OSError, TimeoutError, http.client.HTTPException) as error:
                last_error = error
            time.sleep(0.05)
        output = self._stop_and_collect_output()
        raise AssertionError(
            "uvicorn loopback did not become ready within "
            f"{_SERVER_START_TIMEOUT_SECONDS}s; last_error={last_error!r}; output={output!r}"
        )

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._stop_and_collect_output()

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        use_session: bool = True,
    ) -> LoopbackResponse:
        encoded: bytes | None = None
        request_headers = {"Accept": "application/json"}
        if body is not None:
            encoded = json.dumps(body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if use_session and self._session_cookie is not None:
            request_headers["Cookie"] = self._session_cookie
        if headers:
            request_headers.update(headers)
        connection = http.client.HTTPConnection(
            "127.0.0.1", self._port, timeout=_REQUEST_TIMEOUT_SECONDS
        )
        try:
            connection.request(method, path, body=encoded, headers=request_headers)
            response = connection.getresponse()
            text = response.read().decode("utf-8")
            response_headers = {
                key.lower(): value for key, value in response.getheaders()
            }
            set_cookie = response_headers.get("set-cookie")
            if set_cookie:
                parsed = SimpleCookie()
                parsed.load(set_cookie)
                session = parsed.get("s01_session")
                if session is not None:
                    self._session_cookie = f"s01_session={session.value}"
            return LoopbackResponse(
                status=response.status,
                headers=response_headers,
                text=text,
            )
        finally:
            connection.close()

    def raw_request(
        self,
        method: str,
        path: str,
        *,
        body: bytes = b"",
        content_type: str | None = "application/json",
        headers: dict[str, str] | None = None,
        use_session: bool = True,
    ) -> LoopbackResponse:
        """Send arbitrary raw request bytes (malformed JSON, empty, wrong or
        missing content type) and read the full response over a real socket."""
        request_headers = {"Accept": "application/json"}
        if content_type is not None:
            request_headers["Content-Type"] = content_type
        request_headers["Content-Length"] = str(len(body))
        if use_session and self._session_cookie is not None:
            request_headers["Cookie"] = self._session_cookie
        if headers:
            request_headers.update(headers)
        connection = http.client.HTTPConnection(
            "127.0.0.1", self._port, timeout=_REQUEST_TIMEOUT_SECONDS
        )
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            text = response.read().decode("utf-8", errors="replace")
            response_headers = {
                key.lower(): value for key, value in response.getheaders()
            }
            return LoopbackResponse(
                status=response.status,
                headers=response_headers,
                text=text,
            )
        finally:
            connection.close()

    def send_without_reading(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any],
        headers: dict[str, str],
    ) -> None:
        """Send a complete request then drop the response like a lost network reply."""
        self.open_s01_session()
        encoded = json.dumps(body).encode("utf-8")
        lines = [
            f"{method} {path} HTTP/1.1",
            f"Host: 127.0.0.1:{self._port}",
            "Content-Type: application/json",
            f"Content-Length: {len(encoded)}",
            "Connection: close",
        ]
        if self._session_cookie is not None:
            lines.append(f"Cookie: {self._session_cookie}")
        lines.extend(f"{key}: {value}" for key, value in headers.items())
        request = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + encoded
        with socket.create_connection(
            ("127.0.0.1", self._port), timeout=_REQUEST_TIMEOUT_SECONDS
        ) as connection:
            connection.sendall(request)
            connection.shutdown(socket.SHUT_WR)

    def open_s01_session(self) -> None:
        if self._session_cookie is not None:
            return
        response = self.request(
            "POST",
            "/controlled/s01/api/session",
            body={},
            headers=demo_auth_headers(),
            use_session=False,
        )
        assert response.status == 204
        assert self._session_cookie is not None

    def _stop_and_collect_output(self) -> str:
        process = self._process
        self._process = None
        if process is None:
            return ""
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=_SERVER_STOP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=_SERVER_STOP_TIMEOUT_SECONDS)
        if process.stdout is None:
            return ""
        return process.stdout.read()


def s01_test_loopback(
    extra_env: dict[str, str] | None = None,
) -> UvicornLoopback:
    return UvicornLoopback(
        extra_env,
        app_target="task4_consistency.web.app:create_s01_test_app",
        app_factory=True,
    )


def s01_fault_test_loopback(
    extra_env: dict[str, str] | None = None,
) -> UvicornLoopback:
    values = {"TASK4_S01_TEST_BACKGROUND_ENABLED": "0"}
    values.update(extra_env or {})
    return s01_test_loopback(values)


@pytest.fixture
def loopback() -> Iterator[UvicornLoopback]:
    with UvicornLoopback() as server:
        yield server


@pytest.fixture
def fault_loopback() -> Iterator[UvicornLoopback]:
    with s01_fault_test_loopback() as server:
        yield server


def headers(role: str, scope: str = "C-DEMO") -> dict[str, str]:
    return {"X-S01-Role": role, "X-S01-Scope": scope}


def demo_auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {DEMO_CREDENTIAL}"}


def operator_auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {OPERATOR_CREDENTIAL}"}


def auditor_auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {AUDITOR_CREDENTIAL}"}


def submit(server: UvicornLoopback, key: str = "http-s01") -> LoopbackResponse:
    server.open_s01_session()
    return server.request(
        "POST",
        "/controlled/s01/api/commands/submit",
        body={"scenario_id": SCENARIO, "idempotency_key": key},
        headers=headers("integrator"),
    )


def wait_for_projected_queue_item(
    server: UvicornLoopback,
    application_id: str,
    *,
    timeout_seconds: float = _SERVER_START_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = server.request(
            "GET",
            "/controlled/s01/api/queries/queue",
            headers=headers("reviewer"),
        )
        assert response.status == 200
        item = next(
            (
                candidate
                for candidate in response.json()["items"]
                if candidate["application_id"] == application_id
            ),
            None,
        )
        if item is not None:
            return item
        time.sleep(0.05)
    raise AssertionError("background worker/projector did not publish the application")


def create_internal_failure_app() -> Any:
    from task4_consistency.web.app import app

    @app.get("/controlled/s01/api/_test/internal-error")
    def raise_internal_error() -> None:
        raise RuntimeError("injected unhandled S01 failure")

    return app


def create_expiring_session_app() -> Any:
    import task4_consistency.web.app as web

    clock_path = Path(os.environ["TASK4_S01_TEST_SESSION_CLOCK_PATH"])
    web.S01_SESSION_CLOCK = lambda: float(clock_path.read_text(encoding="ascii"))
    web.S01_SESSION_TTL_SECONDS = int(
        os.environ["TASK4_S01_TEST_SESSION_TTL_SECONDS"]
    )
    return web.create_s01_test_app()


def business_fact_counts(state_path: Path) -> dict[str, int]:
    tables = (
        "applications",
        "receipts",
        "lifecycle_events",
        "evidence_events",
        "audit_events",
        "jobs",
        "attempts",
        "runs",
        "findings",
        "outbox",
    )
    with sqlite3.connect(state_path) as connection:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }


def test_default_app_ignores_environment_artifact_overrides(tmp_path: Path) -> None:
    fixture_root = tmp_path / "untrusted-fixtures"
    fixture_root.mkdir()
    source = ROOT / "fixtures" / "applications" / SCENARIO
    changed = json.loads(source.read_text(encoding="utf-8"))
    changed["documents"][0]["fields"]["engine_no"]["raw"] = "UNTRUSTED-OVERRIDE"
    (fixture_root / SCENARIO).write_text(
        json.dumps(changed, ensure_ascii=False), encoding="utf-8"
    )

    with UvicornLoopback({"TASK4_S01_FIXTURE_ROOT": str(fixture_root)}) as server:
        accepted = submit(server, "http-trusted-default-manifest").json()

    assert accepted["disposition"] == "accepted"
    assert accepted["source_sha256"] == (
        "8f3bf94619690887fbbb3a5c4fa3bfdb815f178874e0b0dda2469b69454b2a58"
    )


def test_loopback_submit_replay_conflict_and_rejection(tmp_path: Path) -> None:
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    source = ROOT / "fixtures" / "applications" / SCENARIO
    copied_source = fixture_root / SCENARIO
    shutil.copyfile(source, copied_source)

    with s01_fault_test_loopback(
        {"TASK4_S01_TEST_FIXTURE_ROOT": str(fixture_root)}
    ) as server:
        first = submit(server, "http-replay")
        assert first.status == 200
        accepted = first.json()
        assert accepted["disposition"] == "accepted"
        assert accepted["application_id"]
        assert accepted["application_id"].startswith("app_")
        assert accepted["application_id"] != "APP-R53-BAD-ENGINE"
        assert accepted["receipt_id"]
        assert accepted["job_id"]
        assert accepted["lifecycle_revision"] == 1
        assert accepted["evidence_revision"] == 1
        assert accepted["track"] == "C-DEMO"
        assert accepted["capability_gate"] == "G1"
        assert accepted["envelope_version"] == "c-demo-envelope/1"
        assert accepted["source_sha256"] == (
            "8f3bf94619690887fbbb3a5c4fa3bfdb815f178874e0b0dda2469b69454b2a58"
        )
        envelope_fingerprint = accepted["envelope_fingerprint"]
        assert len(envelope_fingerprint) == 64
        assert set(envelope_fingerprint) <= set("0123456789abcdef")
        assert accepted["envelope_id"].startswith("envelope_")
        assert accepted["stream_id"].startswith("stream_")
        assert accepted["source_revision_id"].startswith("source_revision_")
        assert accepted["batch_id"].startswith("batch_")
        assert accepted["occurred_at"] is None
        assert accepted["occurred_at_status"] == "unknown"
        assert accepted["produced_at"] is None
        assert accepted["produced_at_status"] == "unknown"
        assert accepted["observed_at"] is None
        assert accepted["observed_at_status"] == "unknown"
        assert accepted["received_at"] == "2000-01-01T00:00:00Z"
        assert accepted["received_at_status"] == "fixed_c_demo_protocol_time"
        assert accepted["adapter_id"] == "legacy-fixture-c-demo"
        assert accepted["adapter_version"] == "1"
        assert accepted["schema_version"] == "1"
        assert accepted["semantic_version"] == "1"
        assert "no-store" in first.headers["cache-control"]

        replay = submit(server, "http-replay")
        assert replay.status == 200
        assert replay.json()["replayed"] is True
        assert replay.json()["receipt_id"] == accepted["receipt_id"]
        assert replay.json()["envelope_fingerprint"] == envelope_fingerprint
        assert replay.json()["envelope_id"] == accepted["envelope_id"]

        changed = json.loads(copied_source.read_text(encoding="utf-8"))
        changed["documents"][0]["fields"]["engine_no"]["raw"] = "CHANGED-SOURCE"
        copied_source.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
        replay_after_source_change = submit(server, "http-replay")
        assert replay_after_source_change.status == 200
        assert replay_after_source_change.json()["disposition"] == "accepted"
        assert replay_after_source_change.json()["replayed"] is True
        assert replay_after_source_change.json()["receipt_id"] == accepted["receipt_id"]
        assert replay_after_source_change.json()["application_id"] == accepted["application_id"]

        conflict = server.request(
            "POST",
            "/controlled/s01/api/commands/submit",
            body={
                "scenario_id": "different-scenario.json",
                "idempotency_key": "http-replay",
            },
            headers=headers("integrator"),
        )
        assert conflict.status == 200
        assert conflict.json()["disposition"] == "rejected"
        assert conflict.json()["reason_code"] == "IDEMPOTENCY_CONFLICT"
        assert conflict.json()["lifecycle_revision"] == 0
        assert conflict.json()["evidence_revision"] == 0

        rejected = server.request(
            "POST",
            "/controlled/s01/api/commands/submit",
            body={"scenario_id": "../outside.json", "idempotency_key": "http-invalid"},
            headers=headers("integrator"),
        )
        assert rejected.status == 200
        assert rejected.json()["disposition"] == "rejected"
        assert rejected.json()["reason_code"] == "SCENARIO_NOT_ALLOWED"
        assert rejected.json()["lifecycle_revision"] == 0
        assert rejected.json()["evidence_revision"] == 0


def test_http_principal_scopes_idempotency_and_is_recorded_in_audit(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "principal-scoped.sqlite3"
    with s01_fault_test_loopback(
        {"TASK4_S01_TEST_STATE_PATH": str(state_path)}
    ) as server:
        server.open_s01_session()
        first_cookie = server._session_cookie
        assert first_cookie is not None
        first = server.request(
            "POST",
            "/controlled/s01/api/commands/submit",
            body={"scenario_id": SCENARIO, "idempotency_key": "shared-session-key"},
            headers={**headers("integrator"), "Cookie": first_cookie},
            use_session=False,
        ).json()
        first_process = server.request(
            "POST",
            "/controlled/s01/api/_test/commands/process",
            body={"now": 0},
            use_session=False,
        )
        first_project = server.request(
            "POST",
            "/controlled/s01/api/_test/commands/project",
            body={},
            use_session=False,
        )
        first_queue = server.request(
            "GET",
            "/controlled/s01/api/queries/queue",
            headers={**headers("reviewer"), "Cookie": first_cookie},
            use_session=False,
        ).json()

        second_session = server.request(
            "POST",
            "/controlled/s01/api/session",
            body={},
            headers=demo_auth_headers(),
            use_session=False,
        )
        assert second_session.status == 204
        second_cookie = server._session_cookie
        assert second_cookie is not None
        assert second_cookie != first_cookie
        second_queue_before_submit = server.request(
            "GET",
            "/controlled/s01/api/queries/queue",
            headers={**headers("reviewer"), "Cookie": second_cookie},
            use_session=False,
        ).json()
        second_reads_first = server.request(
            "GET",
            f"/controlled/s01/api/queries/applications/{first['application_id']}/workspace",
            headers={**headers("reviewer"), "Cookie": second_cookie},
            use_session=False,
        )
        second = server.request(
            "POST",
            "/controlled/s01/api/commands/submit",
            body={"scenario_id": SCENARIO, "idempotency_key": "shared-session-key"},
            headers={**headers("integrator"), "Cookie": second_cookie},
            use_session=False,
        ).json()
        second_process = server.request(
            "POST",
            "/controlled/s01/api/_test/commands/process",
            body={"now": 1},
            use_session=False,
        )
        second_project = server.request(
            "POST",
            "/controlled/s01/api/_test/commands/project",
            body={},
            use_session=False,
        )
        second_queue = server.request(
            "GET",
            "/controlled/s01/api/queries/queue",
            headers={**headers("reviewer"), "Cookie": second_cookie},
            use_session=False,
        ).json()
        first_queue_after_second = server.request(
            "GET",
            "/controlled/s01/api/queries/queue",
            headers={**headers("reviewer"), "Cookie": first_cookie},
            use_session=False,
        ).json()
        first_reads_second = server.request(
            "GET",
            f"/controlled/s01/api/queries/applications/{second['application_id']}/workspace",
            headers={**headers("reviewer"), "Cookie": first_cookie},
            use_session=False,
        )

    assert first["disposition"] == "accepted"
    assert first["replayed"] is False
    assert first_process.json()["status"] == "complete"
    assert first_project.json()["updated"] == 1
    assert [item["application_id"] for item in first_queue["items"]] == [
        first["application_id"]
    ]
    assert second_queue_before_submit == {"items": [], "recovery_items": [], "projection_watermark": 0}
    assert second_reads_first.status == 404
    assert second["disposition"] == "accepted"
    assert second["reason_code"] is None
    assert second["replayed"] is False
    assert second["application_id"] != first["application_id"]
    assert second["receipt_id"] != first["receipt_id"]
    assert second_process.json()["status"] == "complete"
    assert second_project.json()["updated"] == 1
    assert [item["application_id"] for item in second_queue["items"]] == [
        second["application_id"]
    ]
    assert second_queue["projection_watermark"] == 1
    assert [item["application_id"] for item in first_queue_after_second["items"]] == [
        first["application_id"]
    ]
    assert first_queue_after_second["projection_watermark"] == 1
    assert first_reads_second.status == 404

    with sqlite3.connect(state_path) as connection:
        audit_events = [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT payload FROM audit_events ORDER BY rowid"
            ).fetchall()
        ]
    admission_events = [
        event for event in audit_events if event["action"] == "controlled_admission"
    ]
    assert len(admission_events) == 2
    assert {event["subject"] for event in admission_events} == {"c-demo-test-user"}
    assert len({event["scope"] for event in admission_events}) == 2
    assert all(event["scope"].startswith("C-DEMO/session/") for event in admission_events)
    assert all(event["role"] == "integrator" for event in admission_events)
    assert {event["result"] for event in admission_events} == {"accepted"}


def test_http_stop_new_cohort_audits_the_authenticated_operator(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "http-operator-stop-audit.sqlite3"
    with s01_fault_test_loopback(
        {"TASK4_S01_TEST_STATE_PATH": str(state_path)}
    ) as server:
        server.open_s01_session()
        stopped = server.request(
            "POST",
            "/controlled/s01/api/commands/stop-new-cohort",
            body={},
            headers=operator_auth_headers(),
        )

    assert stopped.status == 200
    with sqlite3.connect(state_path) as connection:
        stop_event = json.loads(
            connection.execute(
                "SELECT payload FROM audit_events ORDER BY rowid DESC LIMIT 1"
            ).fetchone()[0]
        )
    assert stop_event["action"] == "controlled_cohort_stop"
    assert stop_event["subject"] == "c-demo-test-operator"
    assert stop_event["scope"] == "C-DEMO"
    assert stop_event["source_id"] == "c-demo-operator-control-plane"
    assert stop_event["role"] == "operator"
    assert stop_event["result"] == "stopped"


def test_http_auditor_rebuilds_minimized_ordered_timeline(
    fault_loopback: UvicornLoopback,
) -> None:
    admission = submit(fault_loopback, "http-audit-timeline").json()
    processed = fault_loopback.request(
        "POST",
        "/controlled/s01/api/_test/commands/process",
        body={"now": 1},
        use_session=False,
    )
    stopped = fault_loopback.request(
        "POST",
        "/controlled/s01/api/commands/stop-new-cohort",
        headers=operator_auth_headers(),
        use_session=False,
    )
    path = (
        "/controlled/s01/api/queries/applications/"
        f"{admission['application_id']}/audit-timeline"
    )
    anonymous = fault_loopback.request("GET", path, use_session=False)
    reviewer = fault_loopback.request("GET", path, headers=headers("reviewer"))
    audited = fault_loopback.request(
        "GET", path, headers=auditor_auth_headers(), use_session=False
    )
    hidden = fault_loopback.request(
        "GET",
        "/controlled/s01/api/queries/applications/not-present/audit-timeline",
        headers=auditor_auth_headers(),
        use_session=False,
    )

    assert processed.json()["status"] == "complete"
    assert stopped.status == 200
    assert anonymous.status == reviewer.status == 403
    assert admission["application_id"] not in anonymous.text
    assert admission["application_id"] not in reviewer.text
    assert hidden.status == 404
    assert audited.status == 200
    assert audited.headers["cache-control"] == "no-store"
    timeline = audited.json()
    assert timeline["integrity"] == "verified"
    assert [event["action"] for event in timeline["events"]] == [
        "controlled_admission",
        "controlled_run_result",
        "controlled_cohort_stop",
    ]
    assert [event["event_time_key"] for event in timeline["events"]] == sorted(
        event["event_time_key"] for event in timeline["events"]
    )
    assert all(
        set(event) == {
            "event_id",
            "event_time",
            "event_sequence",
            "event_time_key",
            "actor",
            "action",
            "result",
            "context",
        }
        for event in timeline["events"]
    )
    assert all("envelope" not in event["context"] for event in timeline["events"])


def test_workbench_rejection_exposes_stable_disposition_reason_and_receipt(
    fault_loopback: UvicornLoopback,
) -> None:
    fault_loopback.open_s01_session()
    first = fault_loopback.request(
        "POST",
        "/controlled/s01/api/workbench/commands/submit",
        body={"scenario_id": "../outside.json", "idempotency_key": "workbench-reject"},
        headers=headers("integrator"),
    )
    replay = fault_loopback.request(
        "POST",
        "/controlled/s01/api/workbench/commands/submit",
        body={"scenario_id": "../outside.json", "idempotency_key": "workbench-reject"},
        headers=headers("integrator"),
    )

    assert first.status == 200
    assert first.json()["disposition"] == "rejected"
    assert first.json()["reason_code"] == "SCENARIO_NOT_ALLOWED"
    assert first.json()["receipt_id"] is not None
    assert replay.json()["replayed"] is True
    assert replay.json()["receipt_id"] == first.json()["receipt_id"]


@pytest.mark.parametrize("invalid_fields", (None, [1], "not-a-field-map"))
def test_loopback_malformed_document_fields_are_stably_rejected(
    tmp_path: Path, invalid_fields: object
) -> None:
    fixture_root = tmp_path / "malformed-fields"
    fixture_root.mkdir()
    (fixture_root / SCENARIO).write_text(
        json.dumps(
            {
                "application_id": "UPSTREAM-MALFORMED-FIELDS",
                "meta": {"field_source": "synthetic"},
                "documents": [
                    {
                        "doc_id": "doc-1",
                        "doc_type": "registration",
                        "fields": invalid_fields,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with s01_test_loopback(
        {"TASK4_S01_TEST_FIXTURE_ROOT": str(fixture_root)}
    ) as server:
        rejected = submit(server, "http-malformed-fields")
        queue = server.request(
            "GET", "/controlled/s01/api/queries/queue", headers=headers("reviewer")
        )

    assert rejected.status == 200
    assert rejected.headers.get("cache-control") == "no-store"
    assert rejected.headers.get("pragma") == "no-cache"
    assert rejected.json()["disposition"] == "rejected"
    assert rejected.json()["reason_code"] == "INVALID_CANONICAL_ENVELOPE"
    assert rejected.json()["lifecycle_revision"] == 0
    assert rejected.json()["evidence_revision"] == 0
    assert queue.status == 200
    assert queue.headers.get("cache-control") == "no-store"
    assert queue.json() == {"items": [], "recovery_items": [], "projection_watermark": 0}


def test_loopback_reconciles_lost_response_with_idempotent_replay(
    loopback: UvicornLoopback,
) -> None:
    key = "http-response-loss"
    loopback.send_without_reading(
        "POST",
        "/controlled/s01/api/commands/submit",
        body={"scenario_id": SCENARIO, "idempotency_key": key},
        headers=headers("integrator"),
    )

    deadline = time.monotonic() + _SERVER_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        replay = submit(loopback, key)
        assert replay.status == 200
        if replay.json()["replayed"]:
            assert replay.json()["disposition"] == "accepted"
            return
        time.sleep(0.05)
    raise AssertionError("response-loss replay never returned the original receipt")


def test_submit_runs_only_through_background_worker_and_projector(
    loopback: UvicornLoopback,
) -> None:
    admission = submit(loopback, "http-background-authority").json()

    caller_controlled_worker = loopback.request(
        "POST",
        "/controlled/s01/api/commands/process",
        body={"worker_id": "caller", "now": 0, "crash": True},
        headers=headers("worker"),
    )
    item = wait_for_projected_queue_item(loopback, admission["application_id"])

    assert caller_controlled_worker.status == 404
    assert item["phase"] == "Manual Review"
    assert item["route"] == "manual_review"
    assert item["mandatory_blockers"][0]["rule_id"] == "R_ENGINE_CROSS"


def test_forged_role_headers_cannot_create_an_authenticated_principal(
    loopback: UvicornLoopback,
) -> None:
    forged_submit = loopback.request(
        "POST",
        "/controlled/s01/api/commands/submit",
        body={"scenario_id": SCENARIO, "idempotency_key": "forged-principal"},
        headers=headers("integrator"),
    )
    forged_queue = loopback.request(
        "GET",
        "/controlled/s01/api/queries/queue",
        headers=headers("reviewer"),
    )

    assert forged_submit.status == 403
    assert forged_queue.status == 200
    assert forged_queue.json() == {"items": [], "recovery_items": [], "projection_watermark": 0}


def test_loopback_process_queue_workspace_and_minimized_evidence(
    loopback: UvicornLoopback,
) -> None:
    admission = submit(loopback, "http-path").json()
    item = wait_for_projected_queue_item(loopback, admission["application_id"])
    assert item["application_id"] == admission["application_id"]
    assert item["phase"] == "Manual Review"
    assert item["route"] == "manual_review"
    assert item["evidence_ready"] is True
    assert item["mandatory_blockers"][0]["rule_id"] == "R_ENGINE_CROSS"
    assert item["projection_watermark"] >= 1

    workspace = loopback.request(
        "GET",
        f"/controlled/s01/api/queries/applications/{admission['application_id']}/workspace",
        headers=headers("reviewer"),
    )
    assert workspace.status == 200
    body = workspace.json()
    assert body["selected_finding"]["run_id"] == body["current_run_id"]
    links = body["selected_finding"]["evidence_links"]
    assert links
    assert body["actions"] == ["read_evidence"]
    assert all(
        link["raw_masked"] == "[REDACTED]"
        for link in links
        if link["value_state"] == "present"
    )
    assert {link["document_id"] for link in links} == {"reg", "pol", "inv"}
    unregistered_trace_fields = {
        "producer_id",
        "producer_family",
        "producer_run_id",
        "model_id",
        "model_version",
        "source_receipt_id",
    }
    assert all(
        unregistered_trace_fields.isdisjoint(link)
        and "source_object_ref" not in link
        and "coordinate_system" not in link
        and "raw" not in link
        for link in links
    )
    provenance_manifest_digests = {
        link["provenance_manifest_digest"] for link in links
    }
    assert len(provenance_manifest_digests) == 1
    assert len(provenance_manifest_digests.pop()) == 64
    for forbidden in ("label", "expected_verdicts", "source_object_ref", "coordinate_system"):
        assert forbidden not in workspace.text


def test_loopback_manual_review_lifecycle_typed_contract_over_http(
    loopback: UvicornLoopback,
) -> None:
    admission = submit(loopback, "http-t02-lifecycle").json()
    application_id = admission["application_id"]
    item = wait_for_projected_queue_item(loopback, application_id)
    work_item_id = item["work_item_id"]

    work = loopback.request(
        "GET",
        f"/controlled/s01/api/queries/review-work-items/{work_item_id}",
        headers=headers("reviewer"),
    )
    assert work.status == 200
    assert work.headers.get("cache-control") == "no-store"
    body = work.json()
    assert body["status"] == "unclaimed"
    assert body["claim_subject"] is None
    assert body["claim_fence"] == 0
    context = body["command_context"]
    automatic = body["automatic_findings"]
    finding_ids = [finding["finding_id"] for finding in automatic]
    assert finding_ids
    run_authority = body["run_authority"]
    evidence_revision = body["evidence_revision"]
    assert len(run_authority["authority_digest"]) == 64

    workspace = loopback.request(
        "GET",
        f"/controlled/s01/api/queries/applications/{application_id}/workspace",
        headers=headers("reviewer"),
    )
    assert workspace.status == 200
    assert workspace.headers.get("cache-control") == "no-store"
    workspace_body = workspace.json()
    assert workspace_body["selected_finding"]["rule_id"] == "R_ENGINE_CROSS"
    assert workspace_body["selected_finding"]["evidence_links"]
    assert all(
        link["raw_masked"] == "[REDACTED]"
        for link in workspace_body["selected_finding"]["evidence_links"]
    )
    workspace_text = workspace.text
    for forbidden in (
        "producer_id",
        "producer_family",
        "producer_run_id",
        "model_id",
        "model_version",
        "source_receipt_id",
        "source_object_ref",
        "coordinate_system",
        "label",
        "expected_verdicts",
    ):
        assert forbidden not in workspace_text
    for raw in ("S2ENG54A", "LSVAA4182N5000054"):
        assert raw not in workspace_text

    hidden_work = loopback.request(
        "GET",
        f"/controlled/s01/api/queries/review-work-items/{work_item_id}",
        headers=headers("reviewer"),
        use_session=False,
    )
    hidden_claim = loopback.request(
        "POST",
        f"/controlled/s01/api/commands/review-work-items/{work_item_id}/claim",
        body={"expected_context": context},
        headers=headers("reviewer"),
        use_session=False,
    )
    assert hidden_work.status == 404
    assert hidden_work.json()["detail"] == {"error": "S03_NOT_FOUND"}
    assert hidden_claim.status == 404
    assert hidden_claim.json()["detail"] == {"error": "S03_NOT_FOUND"}

    claimed = loopback.request(
        "POST",
        f"/controlled/s01/api/commands/review-work-items/{work_item_id}/claim",
        body={"expected_context": context},
        headers=headers("reviewer"),
    )
    assert claimed.status == 200
    assert claimed.headers.get("cache-control") == "no-store"
    claimed_body = claimed.json()
    assert claimed_body["status"] == "claimed"
    assert claimed_body["claim_subject"] == "c-demo-test-user"
    assert claimed_body["claim_fence"] == 1
    assert claimed_body["claim_expires_at"] > 0

    renewed = loopback.request(
        "POST",
        f"/controlled/s01/api/commands/review-work-items/{work_item_id}/renew",
        body={
            "expected_fence": 1,
            "expected_context": context,
            "idempotency_key": "t02-http-renew",
        },
        headers=headers("reviewer"),
    )
    assert renewed.status == 200
    renewed_body = renewed.json()
    assert renewed_body["status"] == "renewed"
    assert renewed_body["claim_fence"] == 1
    assert renewed_body["replayed"] is False
    assert renewed_body["claim_expires_at"] >= claimed_body["claim_expires_at"]

    replayed = loopback.request(
        "POST",
        f"/controlled/s01/api/commands/review-work-items/{work_item_id}/renew",
        body={
            "expected_fence": 1,
            "expected_context": context,
            "idempotency_key": "t02-http-renew",
        },
        headers=headers("reviewer"),
    )
    assert replayed.status == 200
    replayed_body = replayed.json()
    assert replayed_body["status"] == "renewed"
    assert replayed_body["replayed"] is True
    assert replayed_body["claim_expires_at"] == renewed_body["claim_expires_at"]

    released = loopback.request(
        "POST",
        f"/controlled/s01/api/commands/review-work-items/{work_item_id}/release",
        body={
            "expected_fence": 1,
            "expected_context": context,
            "idempotency_key": "t02-http-release",
        },
        headers=headers("reviewer"),
    )
    assert released.status == 200
    released_body = released.json()
    assert released_body["status"] == "released"
    assert released_body["claim_fence"] == 1
    assert released_body["replayed"] is False
    assert released_body["released_at"] > 0

    reclaimed = loopback.request(
        "POST",
        f"/controlled/s01/api/commands/review-work-items/{work_item_id}/claim",
        body={"expected_context": context},
        headers=headers("reviewer"),
    )
    assert reclaimed.status == 200
    assert reclaimed.json()["status"] == "claimed"
    assert reclaimed.json()["claim_fence"] == 2

    stale_renew = loopback.request(
        "POST",
        f"/controlled/s01/api/commands/review-work-items/{work_item_id}/renew",
        body={
            "expected_fence": 1,
            "expected_context": context,
            "idempotency_key": "t02-http-stale-renew",
        },
        headers=headers("reviewer"),
    )
    assert stale_renew.status == 409
    assert stale_renew.json()["detail"] == {
        "error": "S03_STALE",
        "reason_code": "STALE_WORK_ITEM_CLAIM",
    }
    drifted = loopback.request(
        "POST",
        f"/controlled/s01/api/commands/review-work-items/{work_item_id}/renew",
        body={
            "expected_fence": 2,
            "expected_context": {**context, "current_context": "0" * 64},
            "idempotency_key": "t02-http-drifted",
        },
        headers=headers("reviewer"),
    )
    assert drifted.status == 409
    assert drifted.json()["detail"] == {
        "error": "S03_STALE",
        "reason_code": "STALE_REVIEW_CONTEXT",
    }
    partial_context = loopback.request(
        "POST",
        f"/controlled/s01/api/commands/review-work-items/{work_item_id}/renew",
        body={
            "expected_fence": 2,
            "expected_context": {"lifecycle_revision": 0},
            "idempotency_key": "t02-http-partial-context",
        },
        headers=headers("reviewer"),
    )
    # The migrated command contract is closed: a partial context cannot carry a
    # hidden revision, so it is rejected at the boundary as malformed.
    assert partial_context.status == 422
    assert partial_context.json()["detail"]["error"] == "S03_INVALID_COMMAND"
    invalid = loopback.request(
        "POST",
        f"/controlled/s01/api/commands/review-work-items/{work_item_id}/renew",
        body={
            "expected_fence": 2,
            "expected_context": context,
            "idempotency_key": "t02-http-invalid",
            "unexpected": "RAW-CORRECTION-SENTINEL",
        },
        headers=headers("reviewer"),
    )
    assert invalid.status == 422
    assert invalid.json()["detail"]["error"] == "S03_INVALID_COMMAND"
    assert "RAW-CORRECTION-SENTINEL" not in invalid.text

    verification = {
        "schema_version": "human-decision/1",
        "outcome": "confirmed",
        "reason_code": "HUMAN_REVIEW_COMPLETED",
        "finding_decisions": [
            {"finding_id": finding_id, "outcome": "confirmed"}
            for finding_id in finding_ids
        ],
    }
    submitted = loopback.request(
        "POST",
        f"/controlled/s01/api/commands/review-work-items/{work_item_id}/submit",
        body={
            "expected_fence": 2,
            "expected_context": context,
            "idempotency_key": "t02-http-submit",
            "verification": verification,
        },
        headers=headers("reviewer"),
    )
    assert submitted.status == 200
    submitted_body = submitted.json()
    assert submitted_body["status"] == "accepted"
    assert submitted_body["decision_id"]
    assert submitted_body["route"] == "human_complete"
    assert submitted_body["replayed"] is False
    decision_id = submitted_body["decision_id"]

    replay_submit = loopback.request(
        "POST",
        f"/controlled/s01/api/commands/review-work-items/{work_item_id}/submit",
        body={
            "expected_fence": 2,
            "expected_context": context,
            "idempotency_key": "t02-http-submit",
            "verification": verification,
        },
        headers=headers("reviewer"),
    )
    assert replay_submit.status == 200
    replay_body = replay_submit.json()
    assert replay_body["status"] == "accepted"
    assert replay_body["replayed"] is True
    assert replay_body["decision_id"] == decision_id

    work_after = loopback.request(
        "GET",
        f"/controlled/s01/api/queries/review-work-items/{work_item_id}",
        headers=headers("reviewer"),
    )
    assert work_after.status == 200
    work_after_body = work_after.json()
    assert work_after_body["status"] == "completed"
    assert work_after_body["automatic_findings"] == automatic
    assert work_after_body["run_authority"] == run_authority
    assert work_after_body["evidence_revision"] == evidence_revision
    assert [
        decision["decision_id"] for decision in work_after_body["decisions"]
    ] == [decision_id]

    route = loopback.request(
        "GET",
        f"/controlled/s01/api/queries/applications/{application_id}/current-route",
        headers=headers("reviewer"),
    )
    assert route.status == 200
    assert route.json()["phase"] == "Verification Completed"
    assert route.json()["route"] == "human_complete"

    history = loopback.request(
        "GET",
        f"/controlled/s01/api/queries/applications/{application_id}/history",
        headers=headers("reviewer"),
    )
    assert history.status == 200
    current_run = next(
        run for run in history.json()["runs"] if run["current"] is True
    )
    assert current_run["decision_ids"] == [decision_id]

    queue = loopback.request(
        "GET", "/controlled/s01/api/queries/queue", headers=headers("reviewer")
    )
    assert queue.status == 200
    assert all(
        candidate["work_item_id"] != work_item_id
        for candidate in queue.json()["items"]
    )

    public = json.dumps(
        [
            body,
            workspace_body,
            claimed_body,
            renewed_body,
            released_body,
            submitted_body,
            work_after_body,
            route.json(),
            history.json(),
            queue.json(),
        ],
        ensure_ascii=False,
    )
    for raw in ("S2ENG54A", "LSVAA4182N5000054"):
        assert raw not in public


def test_loopback_manual_review_audit_fault_returns_503_without_side_effects() -> None:
    state_path = (
        Path(tempfile.mkdtemp(prefix="xiaopeng-t02-http-")) / "target.sqlite3"
    )
    with s01_test_loopback(
        {
            "TASK4_S01_STATE_PATH": str(state_path),
            "TASK4_S01_TEST_STATE_PATH": str(state_path),
        }
    ) as server:
        admission = submit(server, "http-t02-fault-prep").json()
        item = wait_for_projected_queue_item(server, admission["application_id"])
        work_item_id = item["work_item_id"]
        before = server.request(
            "GET",
            f"/controlled/s01/api/queries/review-work-items/{work_item_id}",
            headers=headers("reviewer"),
        ).json()
        context = before["command_context"]
        queue_before = server.request(
            "GET", "/controlled/s01/api/queries/queue", headers=headers("reviewer")
        ).json()
        # S01 work is scoped to the issuing demo session; the fault server must
        # resolve the same session from the shared state to see it.
        session_cookie = server.session_cookie
        assert session_cookie is not None
        session_headers = {"Cookie": session_cookie}

    with UvicornLoopback(
        {
            "TASK4_S01_STATE_PATH": str(state_path),
            "TASK4_S01_TEST_STATE_PATH": str(state_path),
            "TASK4_S02_TEST_STATE_PATH": str(state_path),
            "TASK4_S03_TEST_FAULT_POINT": "review.audit",
        },
        app_target="task4_consistency.web.app:create_s02_test_app",
        app_factory=True,
    ) as server:
        failed = server.request(
            "POST",
            f"/controlled/s01/api/commands/review-work-items/{work_item_id}/claim",
            body={"expected_context": context},
            headers=session_headers,
            use_session=False,
        )
        after = server.request(
            "GET",
            f"/controlled/s01/api/queries/review-work-items/{work_item_id}",
            headers=session_headers,
            use_session=False,
        )
        queue_after = server.request(
            "GET",
            "/controlled/s01/api/queries/queue",
            headers=session_headers,
            use_session=False,
        )

    assert failed.status == 503
    assert failed.headers.get("cache-control") == "no-store"
    assert failed.json()["detail"] == {
        "error": "S03_UNAVAILABLE",
        "reason_code": "AUDIT_UNAVAILABLE",
    }
    assert after.status == 200
    assert after.json() == before
    assert queue_after.json() == queue_before


def test_s01_manual_review_openapi_contract_is_closed() -> None:
    """The migrated S01 manual-review command bodies, work-item response, and
    history response expose closed schemas: no generic ``additionalProperties``
    on the nested context/verification/decision/history shapes, and the live
    payload keeps its exact current shape."""
    with UvicornLoopback() as server:
        openapi = server.request("GET", "/openapi.json").json()
        schemas = openapi["components"]["schemas"]

        def assert_closed(schema_name: str) -> None:
            schema = schemas.get(schema_name)
            assert schema is not None, schema_name
            assert "additionalProperties" not in schema or schema[
                "additionalProperties"
            ] is not True, schema_name

        for name in (
            "S01ReviewCommandContext",
            "S01FindingDecision",
            "S01HumanDecision",
            "S01HumanDecisionCompatibility",
            "S01HumanDecisionCompatibilityTargetContext",
            "S01HumanDecisionCompatibilityFactCounts",
            "S01NoteMetadata",
            "S01HistoryReconciliation",
            "S01HistorySourceLocation",
            "S01HistoryCorrection",
            "S01HistoryBusinessException",
            "S01HistoryAttachmentVersion",
            "S01HistoryRun",
            "S01ApplicationHistoryResponse",
            "S01ReviewWorkItemResponse",
        ):
            assert_closed(name)

        assert (
            schemas["S01HistoryAttachmentVersion"]["properties"]["version"]["type"]
            == "integer"
        )

        # The migrated command request bodies are emitted inline by
        # ``openapi_extra`` (self-contained, with ``$defs`` inlined); walk the
        # four command paths and prove their nested schemas are closed (no
        # generic ``additionalProperties``).
        for method, path_template in (
            ("post", "/controlled/s01/api/commands/review-work-items/{work_item_id}/claim"),
            ("post", "/controlled/s01/api/commands/review-work-items/{work_item_id}/renew"),
            ("post", "/controlled/s01/api/commands/review-work-items/{work_item_id}/release"),
            ("post", "/controlled/s01/api/commands/review-work-items/{work_item_id}/submit"),
        ):
            operation = openapi["paths"][path_template][method]
            request_schema = operation["requestBody"]["content"]["application/json"]["schema"]
            assert "additionalProperties" not in request_schema or request_schema[
                "additionalProperties"
            ] is not True, path_template
            context = request_schema["properties"]["expected_context"]
            assert "additionalProperties" not in context or context[
                "additionalProperties"
            ] is not True, path_template
            assert set(context["required"]) == {
                "current_context",
                "evidence_revision",
                "lifecycle_revision",
                "projection_watermark",
                "run_id",
            }, path_template
            if path_template.endswith("/submit"):
                verification = request_schema["properties"]["verification"]
                assert "additionalProperties" not in verification or verification[
                    "additionalProperties"
                ] is not True
                assert set(verification["required"]) == {
                    "finding_decisions",
                    "outcome",
                    "reason_code",
                    "schema_version",
                }
                decisions = verification["properties"]["finding_decisions"]
                assert "additionalProperties" not in decisions or decisions[
                    "additionalProperties"
                ] is not True
                finding_decision = decisions["items"]
                assert set(finding_decision["required"]) == {"finding_id", "outcome"}
                assert "additionalProperties" not in finding_decision or finding_decision[
                    "additionalProperties"
                ] is not True

        admission = submit(server, "http-t02-openapi-closed").json()
        item = wait_for_projected_queue_item(server, admission["application_id"])
        work_item_id = item["work_item_id"]
        work = server.request(
            "GET",
            f"/controlled/s01/api/queries/review-work-items/{work_item_id}",
            headers=headers("reviewer"),
        ).json()
        assert set(work["command_context"]) == {
            "lifecycle_revision",
            "evidence_revision",
            "run_id",
            "projection_watermark",
            "current_context",
        }
        claimed = server.request(
            "POST",
            f"/controlled/s01/api/commands/review-work-items/{work_item_id}/claim",
            body={"expected_context": work["command_context"]},
            headers=headers("reviewer"),
        )
        assert claimed.status == 200
        verification = {
            "schema_version": "human-decision/1",
            "outcome": "confirmed",
            "reason_code": "HUMAN_REVIEW_COMPLETED",
            "finding_decisions": [
                {"finding_id": finding["finding_id"], "outcome": "confirmed"}
                for finding in work["automatic_findings"]
            ],
        }
        submitted = server.request(
            "POST",
            f"/controlled/s01/api/commands/review-work-items/{work_item_id}/submit",
            body={
                "expected_fence": 1,
                "expected_context": work["command_context"],
                "idempotency_key": "t02-http-openapi-submit",
                "verification": verification,
            },
            headers=headers("reviewer"),
        )
        assert submitted.status == 200
        work_after = server.request(
            "GET",
            f"/controlled/s01/api/queries/review-work-items/{work_item_id}",
            headers=headers("reviewer"),
        ).json()
        decisions = work_after["decisions"]
        assert len(decisions) == 1
        # The exposed human decision keeps its exact current closed shape: the
        # legacy-oracle compatibility summary (present in the frozen-admission
        # flow) is typed, and the absent note metadata is never synthesized.
        assert set(decisions[0]) == {
            "decision_id",
            "schema_version",
            "outcome",
            "reason_code",
            "finding_decisions",
            "reviewer_subject",
            "reviewer_role",
            "reviewer_source_id",
            "assigned_subject",
            "cycle",
            "finding_ids",
            "evidence_snapshot_id",
            "release_id",
            "fixed_context",
            "claim_fence",
            "submitted_at",
            "compatibility",
        }
        assert decisions[0]["outcome"] == "confirmed"
        assert set(decisions[0]["finding_decisions"][0]) == {"finding_id", "outcome"}
        assert set(decisions[0]["fixed_context"]) == set(work["command_context"])
        compatibility = decisions[0]["compatibility"]
        assert set(compatibility) == {
            "schema_version",
            "differential_source",
            "intent",
            "target_reason_code",
            "conformance",
            "target_context",
            "fact_counts",
            "semantic_differential_digest",
        }
        assert set(compatibility["target_context"]) == {
            "run_id",
            "evidence_snapshot_id",
            "release_id",
            "source_sha256",
        }
        assert set(compatibility["fact_counts"]) == {
            "legacy_checks",
            "target_findings",
            "checks_compared",
            "mismatches",
        }


@pytest.mark.parametrize(
    "cas_fault",
    (
        "cycle",
        "lifecycle_revision",
        "evidence_revision",
        "release_id",
        "release_digest",
        "checker_build",
        "fence",
    ),
)
def test_loopback_cas_fault_propagates_and_recovers(
    cas_fault: str,
) -> None:
    with s01_fault_test_loopback() as server:
        admission = submit(server, f"http-cas-{cas_fault}").json()

        stale = server.request(
            "POST",
            "/controlled/s01/api/_test/commands/process",
            body={"now": 0, "cas_fault": cas_fault},
        )
        assert stale.status == 200
        stale_body = stale.json()
        assert stale_body["status"] == "stale"
        assert stale_body["cas_mismatches"] == [cas_fault]
        assert stale_body["release_id"] == "auto_lease@1.9.0"
        assert stale_body["release_digest"] == (
                "a463ddb219bc90b9c444711c0921f61fb3fb9c7895b1ccb3b86cddf59938e122"
        )
        assert stale_body["checker_build"] == "s01-target-checker/6"
        assert stale_body["fence"] == 1

        recovered = server.request(
            "POST",
            "/controlled/s01/api/_test/commands/process",
            body={"now": 31},
        )
        assert recovered.status == 200
        recovered_body = recovered.json()
        assert recovered_body["status"] == "complete"
        assert recovered_body["run_id"] == stale_body["run_id"]
        assert recovered_body["application_id"] == admission["application_id"]


def test_loopback_worker_uses_constructed_release_after_rules_path_is_removed(
    tmp_path: Path,
) -> None:
    rules_source = ROOT / "configs" / "rules_auto_lease.yaml"
    frozen_rules = tmp_path / "rules.yaml"
    original = rules_source.read_bytes()
    modified = original.replace(b'version: "1.9.0"', b'version: "9.9.9"')
    frozen_rules.write_bytes(modified)
    rules_digest = hashlib.sha256(modified).hexdigest()
    from task4_consistency.controlled.s01_checker import TargetRelease
    from task4_consistency.kb.store import EntityKB
    from task4_consistency.rules.loader import load_rules

    expected_release = TargetRelease.compile(
        load_rules(frozen_rules), rules_digest, knowledge=EntityKB().to_dict()
    )

    with s01_fault_test_loopback(
        {"TASK4_S01_TEST_RULES_PATH": str(frozen_rules)}
    ) as server:
        frozen_rules.unlink()
        admission = submit(server, "http-frozen-release").json()
        processed = server.request(
            "POST",
            "/controlled/s01/api/_test/commands/process",
            body={},
        )

    assert admission["disposition"] == "accepted"
    assert processed.status == 200
    result = processed.json()
    assert result["status"] == "complete"
    assert result["release_id"] == "auto_lease@9.9.9"
    assert result["release_digest"] == expected_release.release_digest
    assert result["checker_build"] == "s01-target-checker/6"
    assert result["fence"] == 1


@pytest.mark.parametrize("rules_state", ("missing", "invalid"))
def test_s01_release_initialization_failure_preserves_existing_web_liveness(
    tmp_path: Path, rules_state: str
) -> None:
    rules_path = tmp_path / "s01-rules.yaml"
    if rules_state == "invalid":
        rules_path.write_text("rules: [", encoding="utf-8")

    with s01_test_loopback(
        {"TASK4_S01_TEST_RULES_PATH": str(rules_path)}
    ) as server:
        index = server.request("GET", "/")
        health = server.request("GET", "/api/health")
        unavailable = (
            server.request("GET", "/controlled/s01"),
            server.request(
                "POST",
                "/controlled/s01/api/session",
                body={},
                headers=demo_auth_headers(),
                use_session=False,
            ),
        )

    assert index.status == 200
    assert health.status == 200
    for response in unavailable:
        assert response.status == 503
        assert response.json() == {
            "detail": {
                "error": "S01_UNAVAILABLE",
                "message": "Controlled S01 is unavailable",
            }
        }
        assert str(rules_path) not in response.text
        assert len(response.text) < 200


def test_all_controlled_s01_success_and_error_responses_are_no_store(
    tmp_path: Path,
) -> None:
    def assert_no_store(response: LoopbackResponse, status: int) -> None:
        assert response.status == status
        assert response.headers.get("cache-control") == "no-store"
        assert response.headers.get("pragma") == "no-cache"

    with UvicornLoopback() as server:
        successful_page = server.request(
            "GET",
            "/controlled/s01",
            headers=demo_auth_headers(),
            use_session=False,
        )
        successful_submit = submit(server, "no-store-success")
        successful_query = server.request(
            "GET", "/controlled/s01/api/queries/queue", headers=headers("reviewer")
        )
        forbidden = server.request(
            "POST",
            "/controlled/s01/api/commands/process",
            body={},
            headers={"X-S01-Role": "worker"},
        )
        hidden = server.request(
            "GET",
            "/controlled/s01/api/queries/applications/not-present/workspace",
            headers=headers("reviewer"),
        )
        invalid = server.request(
            "POST",
            "/controlled/s01/api/commands/submit",
            body={},
            headers=headers("integrator"),
        )

    assert_no_store(successful_page, 200)
    assert_no_store(successful_submit, 200)
    assert_no_store(successful_query, 200)
    assert_no_store(forbidden, 404)
    assert_no_store(hidden, 404)
    assert_no_store(invalid, 422)

    missing_rules = tmp_path / "missing-rules.yaml"
    with s01_test_loopback(
        {"TASK4_S01_TEST_RULES_PATH": str(missing_rules)}
    ) as server:
        unavailable = server.request("GET", "/controlled/s01")
    assert_no_store(unavailable, 503)

    with UvicornLoopback(
        app_target="tests.test_s01_http:create_internal_failure_app",
        app_factory=True,
    ) as server:
        internal_error = server.request(
            "GET", "/controlled/s01/api/_test/internal-error"
        )
    assert_no_store(internal_error, 500)
    assert len(internal_error.text) < 200


def test_loopback_stop_new_cohort_replays_and_recovers_existing_job(
    tmp_path: Path,
) -> None:
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    source = ROOT / "fixtures" / "applications" / SCENARIO
    copied_source = fixture_root / SCENARIO
    shutil.copyfile(source, copied_source)

    with s01_fault_test_loopback(
        {"TASK4_S01_TEST_FIXTURE_ROOT": str(fixture_root)}
    ) as server:
        accepted = submit(server, "http-r-existing").json()
        stopped = server.request(
            "POST",
            "/controlled/s01/api/commands/stop-new-cohort",
            body={},
            headers=operator_auth_headers(),
        )
        changed_bytes = b"legacy adapter input is not a target store\n"
        copied_source.write_bytes(changed_bytes)

        rejected = submit(server, "http-r-new")
        replay = submit(server, "http-r-existing")
        stale = server.request(
            "POST",
            "/controlled/s01/api/_test/commands/process",
            body={"now": 0, "cas_fault": "fence"},
        )
        recovered = server.request(
            "POST",
            "/controlled/s01/api/_test/commands/process",
            body={"now": 31},
        )

    assert accepted["disposition"] == "accepted"
    assert stopped.status == 200
    assert stopped.json() == {
        "track": "C-DEMO",
        "admission": "stopped",
        "reason_code": "S01_NEW_COHORT_STOPPED",
    }
    assert "no-store" in stopped.headers["cache-control"]
    assert rejected.status == 200
    assert rejected.json()["disposition"] == "rejected"
    assert rejected.json()["reason_code"] == "S01_NEW_COHORT_STOPPED"
    assert rejected.json()["lifecycle_revision"] == 0
    assert rejected.json()["evidence_revision"] == 0
    assert replay.status == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["receipt_id"] == accepted["receipt_id"]
    assert stale.status == 200
    assert stale.json()["status"] == "stale"
    assert recovered.status == 200
    assert recovered.json()["status"] == "complete"
    assert recovered.json()["application_id"] == accepted["application_id"]
    assert copied_source.read_bytes() == changed_bytes


def test_loopback_faults_never_publish_old_or_partial_results(
    fault_loopback: UvicornLoopback,
) -> None:
    loopback = fault_loopback
    admission = submit(loopback, "http-faults").json()

    for now, control, expected_status in (
        (0, {"crash": True}, "crashed"),
        (31, {"partial": True}, "partial"),
        (62, {"stale": True}, "stale"),
    ):
        response = loopback.request(
            "POST",
            "/controlled/s01/api/_test/commands/process",
            body={"now": now, **control},
            headers=headers("worker"),
        )
        assert response.status == 200
        assert response.json()["status"] == expected_status
        queue = loopback.request(
            "GET", "/controlled/s01/api/queries/queue", headers=headers("reviewer")
        )
        assert queue.json()["items"] == []

    completed = loopback.request(
        "POST",
        "/controlled/s01/api/_test/commands/process",
        body={"now": 93},
        headers=headers("worker"),
    )
    assert completed.status == 200
    assert completed.json()["status"] == "complete"
    assert completed.json()["projection_pending"] is True

    lagged_queue = loopback.request(
        "GET", "/controlled/s01/api/queries/queue", headers=headers("reviewer")
    )
    assert lagged_queue.json()["items"] == []
    assert lagged_queue.json()["projection_watermark"] == 0

    refreshed = loopback.request(
        "POST",
        "/controlled/s01/api/_test/commands/project",
        body={},
        headers=headers("worker"),
    )
    assert refreshed.status == 200
    assert refreshed.json() == {"updated": 1, "projection_watermark": 1}
    projected_again = loopback.request(
        "POST",
        "/controlled/s01/api/_test/commands/project",
        body={},
    )
    assert projected_again.json() == {"updated": 0, "projection_watermark": 1}
    queue = loopback.request(
        "GET", "/controlled/s01/api/queries/queue", headers=headers("reviewer")
    )
    assert queue.json()["projection_watermark"] == 1
    assert queue.json()["items"][0]["application_id"] == admission["application_id"]

    duplicate = loopback.request(
        "POST",
        "/controlled/s01/api/_test/commands/process",
        body={"now": 94, "duplicate": True},
        headers=headers("worker"),
    )
    assert duplicate.status == 200
    assert duplicate.json()["status"] == "duplicate"
    assert duplicate.json()["run_id"] == completed.json()["run_id"]


def test_http_restart_takes_over_only_an_expired_worker_lease(tmp_path: Path) -> None:
    state_path = tmp_path / "expired-lease.sqlite3"
    state_env = {"TASK4_S01_TEST_STATE_PATH": str(state_path)}

    with s01_fault_test_loopback(state_env) as first:
        admission = submit(first, "http-expired-lease").json()
        first_cookie = first._session_cookie
        assert first_cookie is not None
        crashed = first.request(
            "POST",
            "/controlled/s01/api/_test/commands/process",
            body={"worker_id": "crashed-worker", "now": 0, "crash": True},
        ).json()
        assert crashed["status"] == "crashed"

    with s01_fault_test_loopback(state_env) as before_expiry:
        before_expiry.open_s01_session()
        idle = before_expiry.request(
            "POST",
            "/controlled/s01/api/_test/commands/process",
            body={"worker_id": "early-worker", "now": 29},
        ).json()
        assert idle == {
            "status": "idle",
            "application_id": None,
            "job_id": None,
            "attempt_id": None,
            "run_id": None,
            "reason_code": "NO_READY_JOB",
            "lifecycle_revision": 0,
            "evidence_revision": 0,
            "replayed": False,
            "projection_pending": False,
            "lifecycle_phases": [],
            "cas_mismatches": [],
            "release_id": None,
            "release_digest": None,
            "checker_build": None,
            "fence": 0,
                "evidence_snapshot_id": None,
                "evidence_snapshot_digest": None,
                "semantic_differential": None,
                "retry_after_seconds": 0,
            }

    with s01_test_loopback(state_env) as recovered:
        recovered._session_cookie = first_cookie
        item = wait_for_projected_queue_item(
            recovered, admission["application_id"]
        )
        workspace = recovered.request(
            "GET",
            f"/controlled/s01/api/queries/applications/{admission['application_id']}/workspace",
        ).json()

    assert item["application_id"] == admission["application_id"]
    assert workspace["current_run_id"] == workspace["selected_finding"]["run_id"]


def test_http_restart_recovers_one_pending_projection_without_duplication(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "pending-projection.sqlite3"
    state_env = {"TASK4_S01_TEST_STATE_PATH": str(state_path)}

    with s01_fault_test_loopback(state_env) as first:
        admission = submit(first, "http-pending-projection").json()
        first_cookie = first._session_cookie
        assert first_cookie is not None
        completed = first.request(
            "POST",
            "/controlled/s01/api/_test/commands/process",
            body={"worker_id": "first-worker", "now": 0},
        ).json()
        assert completed["status"] == "complete"
        assert completed["projection_pending"] is True
        assert first.request(
            "GET", "/controlled/s01/api/queries/queue"
        ).json() == {"items": [], "recovery_items": [], "projection_watermark": 0}

    with s01_test_loopback(state_env) as projected:
        projected._session_cookie = first_cookie
        wait_for_projected_queue_item(projected, admission["application_id"])

    with s01_test_loopback(state_env) as restarted:
        restarted._session_cookie = first_cookie
        queue = restarted.request(
            "GET", "/controlled/s01/api/queries/queue"
        ).json()
        workspace = restarted.request(
            "GET",
            f"/controlled/s01/api/queries/applications/{admission['application_id']}/workspace",
        ).json()

    assert queue["projection_watermark"] == 1
    assert [item["application_id"] for item in queue["items"]] == [
        admission["application_id"]
    ]
    assert workspace["current_run_id"] == completed["run_id"]
    assert workspace["selected_finding"]["run_id"] == completed["run_id"]


def test_default_entrypoint_refuses_start_without_explicit_state_configuration() -> None:
    with pytest.raises(AssertionError, match="did not become ready"):
        with UvicornLoopback({"TASK4_S01_STATE_PATH": ""}):
            pass


def test_background_runtime_stops_after_one_authority_exception() -> None:
    from task4_consistency.web.app import S01BackgroundRuntime

    raised = threading.Event()

    class ExplodingAuthority:
        calls = 0

        def process_next_job(self) -> None:
            self.calls += 1
            raised.set()
            raise RuntimeError("immutable S01 integrity failure")

        def refresh_projection(self) -> dict[str, int]:
            raise AssertionError("projection must not run after authority failure")

    authority = ExplodingAuthority()
    runtime = S01BackgroundRuntime(authority)  # type: ignore[arg-type]
    runtime.start()
    assert raised.wait(timeout=1)
    runtime.stop()

    assert authority.calls == 1
    assert runtime.health() == {
        "status": "unhealthy",
        "reason_code": "S01_BACKGROUND_RUNTIME_EXCEPTION",
    }


def test_background_runtime_stop_audit_uses_system_workload_identity(
    tmp_path: Path,
) -> None:
    from task4_consistency.controlled.s01 import (
        ControlledScenarioService,
        S01CommandPrincipal,
    )
    from task4_consistency.web.app import S01BackgroundRuntime

    owner = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "background-system-audit.sqlite3",
    )
    admission = owner.submit_demo(
        scenario_id=SCENARIO,
        idempotency_key="background-system-audit",
        principal=S01CommandPrincipal(
            subject="registered-test-integrator",
            role="integrator",
            scope="C-DEMO",
            source_id="s01-test-client",
        ),
    )
    raised = threading.Event()

    class ExplodingWorker:
        def process_next_job(self) -> None:
            raised.set()
            raise RuntimeError("controlled S01 authority failure")

        def refresh_projection(self) -> dict[str, int]:
            raise AssertionError("projection must not run after authority failure")

        def stop_new_cohort(self, **kwargs: Any) -> dict[str, str]:
            return owner.stop_new_cohort(**kwargs)

    runtime = S01BackgroundRuntime(ExplodingWorker())  # type: ignore[arg-type]
    runtime.start()
    assert raised.wait(timeout=1)
    runtime.stop()

    assert owner.cohort_status() == {
        "track": "C-DEMO",
        "admission": "stopped",
        "reason_code": "S01_RUNTIME_UNHEALTHY",
        "failure_reason_code": "S01_BACKGROUND_RUNTIME_EXCEPTION",
    }
    timeline = owner.audit_timeline(
        principal=S01CommandPrincipal(
            subject="registered-test-auditor",
            role="auditor",
            scope="C-DEMO",
            source_id="s01-test-audit-console",
        ),
        application_id=admission.application_id or "",
    )
    stop_actor = next(
        event["actor"]
        for event in timeline["events"]
        if event["action"] == "controlled_cohort_stop"
    )
    assert stop_actor == {
        "subject": "s01-background-runtime",
        "role": "operator",
        "scope": "C-DEMO",
        "source_id": "s01-target-worker",
    }


def test_background_authority_exception_stops_new_cohort_across_restart(
    tmp_path: Path,
) -> None:
    from task4_consistency.controlled.s01 import ControlledScenarioService
    from task4_consistency.web.app import S01BackgroundRuntime

    state_path = tmp_path / "background-authority-stop.sqlite3"
    owner = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    raised = threading.Event()

    class ExplodingWorker:
        def process_next_job(self) -> None:
            raised.set()
            raise RuntimeError("immutable S01 integrity failure")

        def refresh_projection(self) -> dict[str, int]:
            raise AssertionError("projection must not run after authority failure")

        def stop_new_cohort(self, **kwargs: Any) -> dict[str, str]:
            return owner.stop_new_cohort(**kwargs)

    runtime = S01BackgroundRuntime(ExplodingWorker())  # type: ignore[arg-type]
    runtime.start()
    assert raised.wait(timeout=1)
    runtime.stop()

    expected_stop = {
        "track": "C-DEMO",
        "admission": "stopped",
        "reason_code": "S01_RUNTIME_UNHEALTHY",
        "failure_reason_code": "S01_BACKGROUND_RUNTIME_EXCEPTION",
    }
    assert owner.cohort_status() == expected_stop
    rejected = owner.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id=SCENARIO,
        idempotency_key="post-background-authority-failure",
    )
    assert rejected.disposition.value == "rejected"
    assert rejected.reason_code == "S01_RUNTIME_UNHEALTHY"

    restarted = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=state_path,
    )
    assert restarted.cohort_status() == expected_stop
    rejected_after_restart = restarted.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id=SCENARIO,
        idempotency_key="post-background-authority-restart",
    )
    assert rejected_after_restart.disposition.value == "rejected"
    assert rejected_after_restart.reason_code == "S01_RUNTIME_UNHEALTHY"

    restarted_runtime = S01BackgroundRuntime(restarted)
    restarted_runtime.start()
    deadline = time.monotonic() + 1
    while (
        restarted_runtime.health()["status"] != "unhealthy"
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)
    restarted_runtime.stop()
    assert restarted_runtime.health() == {
        "status": "unhealthy",
        "reason_code": "S01_BACKGROUND_RUNTIME_EXCEPTION",
    }


def test_background_authority_exception_keeps_local_gate_when_stop_cannot_persist(
    tmp_path: Path,
) -> None:
    from task4_consistency.controlled.s01 import ControlledScenarioService
    from task4_consistency.web.app import S01BackgroundRuntime

    owner = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "background-local-stop.sqlite3",
    )
    raised = threading.Event()
    original_persist = owner._store.persist

    def unavailable_persist() -> None:
        raise RuntimeError("S01 authority is unavailable")

    owner._store.persist = unavailable_persist

    class ExplodingWorker:
        def process_next_job(self) -> None:
            raised.set()
            raise RuntimeError("immutable S01 integrity failure")

        def refresh_projection(self) -> dict[str, int]:
            raise AssertionError("projection must not run after authority failure")

        def stop_new_cohort(self, **kwargs: Any) -> dict[str, str]:
            return owner.stop_new_cohort(**kwargs)

    runtime = S01BackgroundRuntime(ExplodingWorker())  # type: ignore[arg-type]
    runtime.start()
    assert raised.wait(timeout=1)
    runtime.stop()
    owner._store.persist = original_persist

    assert owner.cohort_status() == {
        "track": "C-DEMO",
        "admission": "stopped",
        "reason_code": "S01_RUNTIME_UNHEALTHY",
        "failure_reason_code": "S01_BACKGROUND_RUNTIME_EXCEPTION",
    }
    rejected = owner.submit_demo(
        principal=TEST_INTEGRATOR,
        scenario_id=SCENARIO,
        idempotency_key="post-unpersisted-authority-failure",
    )
    assert rejected.disposition.value == "rejected"
    assert rejected.reason_code == "S01_RUNTIME_UNHEALTHY"
    assert owner.fact_counts()["applications"] == 0
    assert owner.fact_counts()["jobs"] == 0


def test_default_entrypoint_restart_reuses_one_authority_and_receipt(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "default-entrypoint.sqlite3"
    environment = {"TASK4_S01_STATE_PATH": str(state_path)}
    with UvicornLoopback(environment) as first:
        admission = submit(first, "default-entrypoint-restart").json()
        original_cookie = first._session_cookie
        assert original_cookie is not None

    with UvicornLoopback(environment) as restarted:
        restarted._session_cookie = original_cookie
        replay = restarted.request(
            "POST",
            "/controlled/s01/api/commands/submit",
            body={
                "scenario_id": SCENARIO,
                "idempotency_key": "default-entrypoint-restart",
            },
            headers=headers("integrator"),
        ).json()
        item = wait_for_projected_queue_item(
            restarted, admission["application_id"]
        )

    assert replay["disposition"] == "accepted"
    assert replay["replayed"] is True
    assert replay["application_id"] == admission["application_id"]
    assert replay["receipt_id"] == admission["receipt_id"]
    assert item["route"] == "manual_review"
    assert item["mandatory_blockers"][0]["rule_id"] == "R_ENGINE_CROSS"


def test_operator_recovers_exhausted_job_forward_through_default_entrypoint(
    tmp_path: Path,
) -> None:
    from task4_consistency.controlled.s01 import (
        ControlledScenarioService,
        ControlledScenarioTestDriver,
        S01CommandPrincipal,
    )

    state_path = tmp_path / "operator-runtime-recovery.sqlite3"

    def permanent_failure(_application: object) -> object:
        raise RuntimeError("injected permanent checker failure")

    failed_owner = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        checker_runner=permanent_failure,
        state_path=state_path,
    )
    session_token, session_principal = failed_owner.issue_session(
        now=time.time(),
        ttl_seconds=3600,
        subject="c-demo-test-user",
        roles=("integrator", "reviewer"),
    )
    admission = failed_owner.submit_demo(
        scenario_id=SCENARIO,
        idempotency_key="operator-runtime-recovery",
        principal=S01CommandPrincipal(
            subject="c-demo-test-user",
            role="integrator",
            scope=session_principal["scope"],
            source_id="c-demo-web-session",
        ),
    )
    driver = ControlledScenarioTestDriver(failed_owner)
    assert driver.process_next_job(now=0).status == "failed"
    assert driver.process_next_job(now=1).status == "failed"
    assert driver.process_next_job(now=3).status == "stopped"

    environment = {"TASK4_S01_STATE_PATH": str(state_path)}
    with UvicornLoopback(environment) as stopped:
        recovery_cookie = f"s01_session={session_token}"
        stopped._session_cookie = recovery_cookie
        rejected = submit(stopped, "new-cohort-during-runtime-stop")
        recovery = stopped.request(
            "POST",
            "/controlled/s01/api/commands/recover-runtime",
            body={
                "expected_failure_reason_code": "CHECKER_EXCEPTION_RETRY_EXHAUSTED"
            },
            headers=operator_auth_headers(),
        )

    assert rejected.status == 200
    assert rejected.json()["disposition"] == "rejected"
    assert rejected.json()["reason_code"] == "S01_RUNTIME_UNHEALTHY"
    assert recovery.status == 200
    assert recovery.json() == {
        "track": "C-DEMO",
        "recovery": "scheduled",
        "reason_code": "S01_RUNTIME_RECOVERY_SCHEDULED",
        "failure_reason_code": "CHECKER_EXCEPTION_RETRY_EXHAUSTED",
        "requeued_jobs": 1,
    }

    with UvicornLoopback(environment) as recovered:
        recovered._session_cookie = recovery_cookie
        item = wait_for_projected_queue_item(
            recovered, admission.application_id or ""
        )

    assert item["application_id"] == admission.application_id
    assert item["route"] == "manual_review"
    assert item["mandatory_blockers"][0]["rule_id"] == "R_ENGINE_CROSS"


def test_loopback_denies_commands_and_hides_cross_scope_or_unauthorized_reads(
    loopback: UvicornLoopback,
) -> None:
    admission = submit(loopback, "http-auth").json()
    wait_for_projected_queue_item(loopback, admission["application_id"])

    forbidden = loopback.request(
        "POST",
        "/controlled/s01/api/commands/process",
        body={"now": 1},
        headers=headers("reviewer"),
    )
    assert forbidden.status == 404

    for request_headers in ({}, headers("integrator"), headers("reviewer", "other-scope")):
        response = loopback.request(
            "GET",
            f"/controlled/s01/api/queries/applications/{admission['application_id']}/workspace",
            headers=request_headers,
            use_session=False,
        )
        assert response.status == 404
        assert admission["application_id"] not in response.text

    queue = loopback.request(
        "GET",
        "/controlled/s01/api/queries/queue",
        headers=headers("reviewer", "other-scope"),
        use_session=False,
    )
    assert queue.status == 200
    assert queue.json()["items"] == []


def test_loopback_missing_session_denies_commands_before_owner_and_hides_queries(
    loopback: UvicornLoopback,
) -> None:
    role_only = lambda role: {"X-S01-Role": role}
    authoritative_before = loopback.request(
        "GET",
        "/controlled/s01/api/queries/queue",
        headers=headers("reviewer"),
        use_session=False,
    ).json()

    command_responses = (
        loopback.request(
            "POST",
            "/controlled/s01/api/commands/submit",
            body={"scenario_id": SCENARIO, "idempotency_key": "missing-scope-submit"},
            headers=role_only("integrator"),
            use_session=False,
        ),
        loopback.request(
            "POST",
            "/controlled/s01/api/commands/process",
            body={},
            headers=role_only("worker"),
            use_session=False,
        ),
        loopback.request(
            "POST",
            "/controlled/s01/api/commands/project",
            headers=role_only("worker"),
            use_session=False,
        ),
        loopback.request(
            "POST",
            "/controlled/s01/api/commands/stop-new-cohort",
            headers=role_only("worker"),
            use_session=False,
        ),
    )

    assert [response.status for response in command_responses] == [403, 404, 404, 403]
    authoritative_after = loopback.request(
        "GET",
        "/controlled/s01/api/queries/queue",
        headers=headers("reviewer"),
        use_session=False,
    ).json()
    assert authoritative_after == authoritative_before == {
        "items": [],
        "recovery_items": [],
        "projection_watermark": 0,
    }

    admission = submit(loopback, "authorized-after-missing-scope").json()
    hidden_before = loopback.request(
        "GET",
        "/controlled/s01/api/queries/queue",
        headers=role_only("reviewer"),
        use_session=False,
    )
    assert hidden_before.status == 200
    assert hidden_before.json() == {"items": [], "recovery_items": [], "projection_watermark": 0}

    wait_for_projected_queue_item(loopback, admission["application_id"])

    hidden_after = loopback.request(
        "GET",
        "/controlled/s01/api/queries/queue",
        headers=role_only("reviewer"),
        use_session=False,
    )
    hidden_workspace = loopback.request(
        "GET",
        f"/controlled/s01/api/queries/applications/{admission['application_id']}/workspace",
        headers=role_only("reviewer"),
        use_session=False,
    )
    assert hidden_after.json() == hidden_before.json()
    assert hidden_workspace.status == 404
    assert admission["application_id"] not in hidden_workspace.text


def test_anonymous_caller_cannot_issue_a_demo_session(
    fault_loopback: UvicornLoopback,
) -> None:
    response = fault_loopback.request(
        "POST",
        "/controlled/s01/api/session",
        body={},
        use_session=False,
    )

    assert response.status == 403
    assert "set-cookie" not in response.headers


def test_registered_demo_session_cannot_stop_another_session(
    fault_loopback: UvicornLoopback,
) -> None:
    fault_loopback.open_s01_session()
    first_cookie = fault_loopback._session_cookie
    assert first_cookie is not None
    second_session = fault_loopback.request(
        "POST",
        "/controlled/s01/api/session",
        body={},
        headers=demo_auth_headers(),
        use_session=False,
    )
    second_cookie = fault_loopback._session_cookie
    assert second_session.status == 204
    assert second_cookie is not None and second_cookie != first_cookie

    stop = fault_loopback.request(
        "POST",
        "/controlled/s01/api/commands/stop-new-cohort",
        headers={"Cookie": first_cookie},
        use_session=False,
    )
    admission = fault_loopback.request(
        "POST",
        "/controlled/s01/api/commands/submit",
        body={"scenario_id": SCENARIO, "idempotency_key": "after-session-stop"},
        headers={"Cookie": second_cookie},
        use_session=False,
    )

    assert stop.status == 403
    assert admission.json()["disposition"] == "accepted"


def test_issued_session_expiry_denies_commands_hides_reads_and_changes_no_business_facts(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "expired-session.sqlite3"
    clock_path = tmp_path / "session-clock.txt"
    clock_path.write_text("100", encoding="ascii")
    environment = {
        "TASK4_S01_TEST_STATE_PATH": str(state_path),
        "TASK4_S01_TEST_SESSION_CLOCK_PATH": str(clock_path),
        "TASK4_S01_TEST_SESSION_TTL_SECONDS": "10",
    }
    with UvicornLoopback(
        environment,
        app_target="tests.test_s01_http:create_expiring_session_app",
        app_factory=True,
    ) as server:
        admission = submit(server, "issued-session-expiry").json()
        wait_for_projected_queue_item(server, admission["application_id"])
        issued_cookie = server._session_cookie
        assert issued_cookie is not None
        before = business_fact_counts(state_path)

        clock_path.write_text("110", encoding="ascii")
        expired_command = server.request(
            "POST",
            "/controlled/s01/api/workbench/commands/submit",
            body={
                "scenario_id": SCENARIO,
                "idempotency_key": "must-not-reach-owner-after-expiry",
            },
            headers={**headers("integrator"), "Cookie": issued_cookie},
            use_session=False,
        )
        hidden_queue = server.request(
            "GET",
            "/controlled/s01/api/queries/queue",
            headers={**headers("reviewer"), "Cookie": issued_cookie},
            use_session=False,
        )
        hidden_workspace = server.request(
            "GET",
            f"/controlled/s01/api/queries/applications/{admission['application_id']}/workspace",
            headers={**headers("reviewer"), "Cookie": issued_cookie},
            use_session=False,
        )
        after = business_fact_counts(state_path)

    assert expired_command.status == 403
    assert admission["application_id"] not in expired_command.text
    assert hidden_queue.status == 200
    assert hidden_queue.json() == {
        "items": [],
        "recovery_items": [],
        "projection_watermark": 0,
        "access_ended": True,
    }
    assert hidden_queue.headers["x-s01-access-ended"] == "1"
    assert hidden_workspace.status == 404
    assert admission["application_id"] not in hidden_workspace.text
    assert after == before


@pytest.mark.parametrize(
    ("failure_env", "reason_code"),
    (
        ({"TASK4_S01_TEST_AUDIT_AVAILABLE": "0"}, "AUDIT_UNAVAILABLE"),
        ({"TASK4_S01_TEST_STORAGE_AVAILABLE": "0"}, "STORAGE_UNAVAILABLE"),
    ),
)
def test_loopback_audit_or_storage_failure_creates_no_visible_target_revision(
    failure_env: dict[str, str], reason_code: str
) -> None:
    with s01_test_loopback(failure_env) as server:
        rejected = submit(server, f"http-{reason_code.lower()}")
        assert rejected.status == 200
        body = rejected.json()
        assert body["disposition"] == "rejected"
        assert body["reason_code"] == reason_code
        assert body["lifecycle_revision"] == 0
        assert body["evidence_revision"] == 0
        queue = server.request(
            "GET", "/controlled/s01/api/queries/queue", headers=headers("reviewer")
        )
        assert queue.status == 200
        assert queue.json() == {"items": [], "recovery_items": [], "projection_watermark": 0}
