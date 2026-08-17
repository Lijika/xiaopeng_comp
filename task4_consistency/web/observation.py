"""Closed observation telemetry core (Issue #54).

One self-contained module behind the FastAPI HTTP adapter.  It consumes the
contracted legacy catalog (``legacy_catalog``) and emits deterministic,
sealable evidence about the HTTP traffic a controlled window actually
served -- without becoming a security-audit, Lifecycle, or Policy Governance
owner (ADR-0004 / ADR-0006).  It writes zero lifecycle, evidence, run,
route, work-item, policy, audit, or business revisions.  Record failures
invalidate the window; they never create or rewrite security-audit facts.

Closed request record (exactly the twelve fields below; no attachments,
OCR/field values, credentials, free text, query strings, raw paths,
internal paths, or application IDs ever enter a record):

    sequence, timestamp_utc, artifact_sha256, process_id, window_id,
    correlation_id, traffic_class, method, normalized_path_family,
    matched_route_owner, legacy_surface_id, response_status

``sequence`` is a monotonic integer allocated under one ``fcntl.flock``
together with the JSONL append + ``fsync`` (global file order for the
serial one-worker cohort; a crash between allocation and append is detected
as a gap by the verifier).  Traffic classification is env-driven
(``TASK4_OBS_PROCESS_CLASS``) with an absolute ``/api/health`` override;
anything else becomes ``unknown`` and invalidates the window.

The module is pure standard library (json, hashlib, os, pathlib, fcntl,
argparse, socket, datetime) and never imports FastAPI/Starlette: the ASGI
middleware is a raw middleware the application registers as its outermost
custom middleware (in FastAPI the last ``add_middleware`` call wraps
everything else, so it sees all HTTP, static mounts, auth, and 404s, and it
receives lifespan scopes).  Ordinary runs with no observation environment
stay capture-free and cannot break normal operation.

The deterministic test adapter (``FixedClock`` + ``InMemorySink``) exercises
exactly the same classification/integrity behavior as the production
adapters.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import platform
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol

from task4_consistency.web.legacy_catalog import (
    CONTRACTED_LEGACY_ENTRIES,
    match_legacy_surface,
)

# --- Closed schemas and vocabulary ------------------------------------------

REQUEST_RECORD_FIELDS: tuple[str, ...] = (
    "sequence",
    "timestamp_utc",
    "artifact_sha256",
    "process_id",
    "window_id",
    "correlation_id",
    "traffic_class",
    "method",
    "normalized_path_family",
    "matched_route_owner",
    "legacy_surface_id",
    "response_status",
)

LIFECYCLE_RECORD_FIELDS: tuple[str, ...] = (
    "process_id",
    "window_id",
    "artifact_sha256",
    "event",
    "timestamp_utc",
)

TRAFFIC_CLASSES: tuple[str, ...] = (
    "operator-simulated",
    "release",
    "health",
    "playwright-probe",
    "rollback-probe",
)

LIFECYCLE_EVENTS: tuple[str, ...] = ("start", "end")

UNKNOWN_CLASS = "unknown"
HEALTH_PATH = "/api/health"
DYNAMIC_KB_FAMILY = "/api/kb/{section}/{key}"
BUNDLE_SCHEMA_VERSION = "1"

# --- Clock ------------------------------------------------------------------


class ObservationClock(Protocol):
    """The single time seam: every recorded timestamp comes from here."""

    def utc_now(self) -> datetime: ...


class SystemClock:
    """Production clock: the current UTC wall time."""

    def utc_now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    """Deterministic test adapter: a fixed, explicitly advanceable clock."""

    def __init__(self, fixed: datetime) -> None:
        self._now = fixed

    def utc_now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


def _iso(timestamp: datetime) -> str:
    return timestamp.astimezone(timezone.utc).isoformat()


# --- Sinks ------------------------------------------------------------------


class InMemorySink:
    """Deterministic test adapter: appends into plain lists, assigning the
    global append order as the sequence."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.lifecycle: list[dict] = []

    def append_request(self, record: dict) -> None:
        record["sequence"] = len(self.requests) + 1
        self.requests.append(record)

    def append_lifecycle(self, record: dict) -> None:
        self.lifecycle.append(record)


class JsonlSink:
    """Production adapter: append-only JSONL with ``fsync`` and one
    ``fcntl.flock`` per file.  The ``sequence`` sidecar holds the next
    sequence; it is written (and fsynced) under the same lock before the
    line is appended, so a crash between allocation and append leaves a
    verifier-detectable gap."""

    def __init__(self, log_dir: Path | str) -> None:
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)

    def append_request(self, record: dict) -> None:
        path = self._log_dir / "requests.jsonl"
        with open(path, "ab") as log:
            fcntl.flock(log.fileno(), fcntl.LOCK_EX)
            try:
                # ponytail: one host-wide append lock fits the controlled window; shard logs only after measured contention.
                sequence = self._next_sequence()
                self._write_sidecar(sequence + 1)
                record["sequence"] = sequence
                line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                log.write(line.encode("utf-8"))
                log.flush()
                os.fsync(log.fileno())
            finally:
                fcntl.flock(log.fileno(), fcntl.LOCK_UN)

    def append_lifecycle(self, record: dict) -> None:
        path = self._log_dir / "process-lifecycle.jsonl"
        with open(path, "ab") as log:
            fcntl.flock(log.fileno(), fcntl.LOCK_EX)
            try:
                line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                log.write(line.encode("utf-8"))
                log.flush()
                os.fsync(log.fileno())
            finally:
                fcntl.flock(log.fileno(), fcntl.LOCK_UN)

    def _next_sequence(self) -> int:
        sidecar = self._log_dir / "sequence"
        if sidecar.exists():
            text = sidecar.read_text(encoding="utf-8").strip()
            try:
                return int(text)
            except ValueError as exc:
                raise ValueError(f"corrupt observation sequence sidecar: {text!r}") from exc
        max_sequence = 0
        path = self._log_dir / "requests.jsonl"
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    max_sequence = max(max_sequence, int(json.loads(line)["sequence"]))
        return max_sequence + 1

    def _write_sidecar(self, next_sequence: int) -> None:
        sidecar = self._log_dir / "sequence"
        with open(sidecar, "w", encoding="utf-8") as handle:
            handle.write(f"{next_sequence}\n")
            handle.flush()
            os.fsync(handle.fileno())


# --- Classification and path families ---------------------------------------


def classify_traffic(path: str, process_class: str | None) -> str:
    """The traffic class of one request.

    Requests whose path is exactly ``/api/health`` are always ``health``;
    otherwise the process class from ``TASK4_OBS_PROCESS_CLASS`` applies.
    A missing or invalid process class becomes ``unknown`` (which
    invalidates the window at verification time)."""
    clean = path.split("?", 1)[0].split("#", 1)[0]
    if clean == HEALTH_PATH:
        return "health"
    if process_class in TRAFFIC_CLASSES:
        return process_class
    return UNKNOWN_CLASS


def default_family_table() -> dict[str, str]:
    """Exact contracted families: every catalog surface path plus the
    health path and the collapsed non-catalog static family.  The dynamic
    KB delete family is derived, never exact."""
    table: dict[str, str] = {}
    for entry in CONTRACTED_LEGACY_ENTRIES:
        if "/{" in entry.path:
            continue
        table[entry.path] = entry.path
    table[HEALTH_PATH] = HEALTH_PATH
    table["/static/*"] = "/static/*"
    return table


def _pattern_family_regex(pattern: str) -> re.Pattern[str]:
    """One registered route pattern (``{param}`` placeholders) as an
    anchored segment regex; parameters never capture across ``/``."""
    parts = []
    for segment in pattern.split("/"):
        if segment.startswith("{") and segment.endswith("}"):
            parts.append("[^/]+")
        else:
            parts.append(re.escape(segment))
    return re.compile("^" + "/".join(parts) + "$")


def normalize_path_family(
    path: str, family_table: Mapping[str, str] | None = None
) -> str:
    """The normalized path family of one raw request path.

    The raw path never enters a record: query/fragment are stripped, exact
    catalog surfaces keep their canonical spelling, registered ``{param}``
    route patterns match their family (no concrete path parameter text),
    the dynamic KB delete family becomes ``/api/kb/{section}/{key}``,
    non-catalog static requests collapse to ``/static/*``, and anything
    unregistered normalizes to itself so the verifier can reject it as a
    raw arbitrary segment."""
    table = default_family_table() if family_table is None else family_table
    clean = path.split("?", 1)[0].split("#", 1)[0]
    if clean in table:
        return table[clean]
    for pattern, family in table.items():
        if "{" not in pattern:
            continue
        if _pattern_family_regex(pattern).match(clean):
            return family
    if clean.startswith("/api/kb/") and "/" in clean[len("/api/kb/") :]:
        return DYNAMIC_KB_FAMILY
    if clean.startswith("/static/") and "/static/*" in table:
        return "/static/*"
    return clean


def _iter_route_paths(routes: Iterable[Any]) -> Iterator[str]:
    """Every route path, descending into lazily included routers.

    FastAPI 0.139 (the installed Starlette line) wraps ``include_router`` in a
    ``_IncludedRouter`` object instead of copying routes into ``app.routes``;
    its ``original_router.routes`` may itself nest further included
    routers, so the walk is recursive."""
    for route in routes:
        path = getattr(route, "path", None)
        if isinstance(path, str) and path:
            yield path
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from _iter_route_paths(getattr(original, "routes", ()) or ())


def app_family_table(app: Any) -> dict[str, str]:
    """Route-pattern family table of one FastAPI application: every
    registered route path (with ``{param}`` placeholders kept as patterns,
    including lazily included routers), the catalog surfaces, the health
    path and the collapsed static family."""
    table = default_family_table()
    for path in _iter_route_paths(getattr(app, "routes", ()) or ()):
        table[path] = path
    return table


def legacy_surface_id_for(
    method: str,
    path: str,
    process_class: str | None,
    react_owned_paths: Mapping[str, object] | None = None,
) -> str | None:
    """The catalog surface of one request, or None.

    On the current artifact a request resolving to a React-owned route
    belongs to the React owner, so no catalog match is reported; under
    ``rollback-probe`` the prior artifact still resolves those paths to
    their legacy owners, so suppression never applies there.  Without a
    ``react_owned_paths`` mapping the pure catalog match applies."""
    if (
        react_owned_paths
        and process_class != "rollback-probe"
        and path in react_owned_paths
    ):
        return None
    return match_legacy_surface(method, path)


def resolve_route_owner(scope: Mapping[str, Any]) -> str:
    """The matched route handler symbol from a post-dispatch ASGI scope.

    Starlette mutates ``scope["endpoint"]`` on matched routes; mounted
    static/ASGI apps are class instances, so their type name is used.
    Unmatched requests (e.g. 404s) never set it and resolve to
    ``"unmatched"``.  Never empty."""
    endpoint = scope.get("endpoint")
    if endpoint is None:
        return "unmatched"
    name = getattr(endpoint, "__name__", None)
    if name is None:
        name = getattr(type(endpoint), "__name__", None)
    return name or "unknown"


# --- Leak scan --------------------------------------------------------------

# Conservative value markers for the closed schema: no credential, raw
# value, free text, internal path, application ID, or unregistered key may
# ever enter a record.
_LEAK_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?i)\b(password|passwd|secret|credential|authorization|"
            r"api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret)\b"
        ),
        "credential-like value",
    ),
    (re.compile(r"(?i)\bbearer\s+[a-z0-9._-]{4,}"), "credential-like value"),
    (re.compile(r"\bsk-[a-zA-Z0-9]{16,}\b"), "credential-like value"),
    (re.compile(r"-----BEGIN [A-Z ]+-----"), "credential-like value"),
    (
        re.compile(r"(?i)\bapp-[a-z0-9][a-z0-9_-]{3,}\b"),
        "application-ID-like value",
    ),
    (
        re.compile(
            r"(?i)(task4_consistency[/\\]|fixtures[/\\]|/home/|/root/|/tmp/|/var/|"
            r"node_modules[/\\]|\.venv[/\\]|configs[/\\]|docs[/\\]|out[/\\]|scripts[/\\])"
        ),
        "internal path",
    ),
)
_FREE_TEXT_RE = re.compile(r"[\u4e00-\u9fff]|\s")


def scan_record_for_leaks(record: Mapping[str, Any]) -> list[str]:
    """Leak scan of one record against the closed schema.

    Returns every concrete finding (empty for a clean record): credential-
    or application-ID-like values, free text (whitespace/CJK content) and
    internal repository paths are all rejected."""
    findings: list[str] = []
    for key, value in record.items():
        if not isinstance(value, str):
            continue
        for pattern, label in _LEAK_PATTERNS:
            if pattern.search(value):
                findings.append(f"{label} in field {key!r}")
                break
        if _FREE_TEXT_RE.search(value):
            findings.append(f"free-text value in field {key!r}")
    return findings


# --- Recorder ---------------------------------------------------------------


class NoopRecorder:
    """Capture-free recorder for ordinary runs with no observation
    environment: never touches disk and can never break normal operation."""

    enabled = False

    def record_http(self, **kwargs: Any) -> None:
        return None

    def record_lifecycle(self, event: str) -> None:
        return None


class ObservationRecorder:
    """Core recorder: builds closed request/lifecycle records through one
    clock and one sink, exercising the same classification and correlation
    behavior in production and in the deterministic test adapter."""

    enabled = True

    def __init__(
        self,
        clock: ObservationClock,
        sink: Any,
        *,
        window_id: str,
        artifact_sha256: str,
        process_id: str,
        process_class: str | None,
        family_table: Mapping[str, str] | None = None,
        react_owned_paths: Mapping[str, object] | None = None,
    ) -> None:
        self._clock = clock
        self._sink = sink
        self._window_id = window_id
        self._artifact_sha256 = artifact_sha256
        self._process_id = process_id
        self._process_class = process_class
        self._family_table = (
            dict(family_table) if family_table is not None else default_family_table()
        )
        self._react_owned_paths = react_owned_paths
        self._correlation_counter = 0

    def classify(self, path: str) -> str:
        return classify_traffic(path, self._process_class)

    def new_correlation_id(self) -> str:
        """Process-bound prefix plus a per-process counter; a record whose
        correlation prefix does not match its process is invalid."""
        self._correlation_counter += 1
        return f"p{self._process_id}-{self._correlation_counter}"

    def record_http(
        self,
        *,
        method: str,
        path: str,
        response_status: int,
        matched_route_owner: str,
        correlation_id: str | None = None,
    ) -> dict:
        """Build, append (via the sink, which allocates the sequence under
        the JSONL append lock) and return one closed request record."""
        if correlation_id is None:
            correlation_id = self.new_correlation_id()
        record = {
            "timestamp_utc": _iso(self._clock.utc_now()),
            "artifact_sha256": self._artifact_sha256,
            "process_id": self._process_id,
            "window_id": self._window_id,
            "correlation_id": correlation_id,
            "traffic_class": self.classify(path),
            "method": method,
            "normalized_path_family": normalize_path_family(path, self._family_table),
            "matched_route_owner": matched_route_owner,
            "legacy_surface_id": legacy_surface_id_for(
                method,
                path,
                self._process_class,
                react_owned_paths=self._react_owned_paths,
            ),
            "response_status": response_status,
        }
        self._sink.append_request(record)
        return record

    def record_lifecycle(self, event: str) -> dict:
        if event not in LIFECYCLE_EVENTS:
            raise ValueError(f"unknown lifecycle event {event!r}")
        record = {
            "process_id": self._process_id,
            "window_id": self._window_id,
            "artifact_sha256": self._artifact_sha256,
            "event": event,
            "timestamp_utc": _iso(self._clock.utc_now()),
        }
        self._sink.append_lifecycle(record)
        return record


# --- ASGI middleware --------------------------------------------------------


class ObservationMiddleware:
    """Raw ASGI observation middleware (no FastAPI/Starlette imports).

    Registered as the OUTERMOST custom middleware, it sees all HTTP,
    static mounts, auth and 404s, plus lifespan scopes.  It observes only:
    on ``http.response.start`` it records the closed request record (status
    from the message, route owner from the post-dispatch scope) and then
    forwards the message; lifespan start/end are recorded only after the
    downstream's ``startup.complete`` / ``shutdown.complete`` are forwarded.
    Auth headers, cookies, sessions and static cache headers pass through
    byte-identical.  A recorder failure surfaces as a failed request --
    it never silently drops evidence."""

    def __init__(self, downstream: Any, recorder: Any) -> None:
        self._downstream = downstream
        self._recorder = recorder

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            await self._http(scope, receive, send)
        elif scope.get("type") == "lifespan":
            await self._lifespan(scope, receive, send)
        else:
            await self._downstream(scope, receive, send)

    async def _http(self, scope: dict, receive: Any, send: Any) -> None:
        if not self._recorder.enabled:
            await self._downstream(scope, receive, send)
            return
        correlation_id = self._recorder.new_correlation_id()
        recorded = False

        async def send_wrapper(message: dict) -> None:
            nonlocal recorded
            if message.get("type") == "http.response.start":
                self._recorder.record_http(
                    method=scope.get("method", ""),
                    path=scope.get("path", ""),
                    response_status=message.get("status", 0),
                    matched_route_owner=resolve_route_owner(scope),
                    correlation_id=correlation_id,
                )
                recorded = True
            await send(message)

        try:
            await self._downstream(scope, receive, send_wrapper)
        except Exception:
            if not recorded:
                self._recorder.record_http(
                    method=scope.get("method", ""),
                    path=scope.get("path", ""),
                    response_status=500,
                    matched_route_owner=resolve_route_owner(scope),
                    correlation_id=correlation_id,
                )
            raise

    async def _lifespan(self, scope: dict, receive: Any, send: Any) -> None:
        if not self._recorder.enabled:
            await self._downstream(scope, receive, send)
            return

        async def send_wrapper(message: dict) -> None:
            kind = message.get("type")
            if kind == "lifespan.startup.complete":
                await send(message)
                self._recorder.record_lifecycle("start")
            elif kind == "lifespan.shutdown.complete":
                await send(message)
                self._recorder.record_lifecycle("end")
            else:
                await send(message)

        await self._downstream(scope, receive, send_wrapper)


# --- Environment-driven configuration ---------------------------------------


@dataclass(frozen=True)
class ObservationConfig:
    """Env-derived observation configuration; every field is optional and
    an absent observation environment yields a capture-free no-op."""

    log_dir: Path | None
    window_id: str
    artifact_sha256: str
    process_class: str | None
    process_id: str

    @property
    def enabled(self) -> bool:
        return (
            self.log_dir is not None
            and bool(self.window_id)
            and bool(self.artifact_sha256)
        )


def config_from_env(environ: Mapping[str, str] | None = None) -> ObservationConfig:
    env = os.environ if environ is None else environ
    log_dir = env.get("TASK4_OBS_LOG_DIR", "").strip()
    return ObservationConfig(
        log_dir=Path(log_dir) if log_dir else None,
        window_id=env.get("TASK4_OBS_WINDOW_ID", "").strip(),
        artifact_sha256=env.get("TASK4_OBS_ARTIFACT_SHA256", "").strip(),
        process_class=env.get("TASK4_OBS_PROCESS_CLASS", "").strip() or None,
        process_id=env.get("TASK4_OBS_PROCESS_ID", "").strip() or str(os.getpid()),
    )


def recorder_from_env(
    environ: Mapping[str, str] | None = None,
    *,
    family_table: Mapping[str, str] | None = None,
    react_owned_paths: Mapping[str, object] | None = None,
) -> ObservationRecorder | NoopRecorder:
    """The env-driven recorder factory: absent or incomplete observation
    configuration yields the capture-free no-op recorder."""
    config = config_from_env(environ)
    if not config.enabled:
        return NoopRecorder()
    return ObservationRecorder(
        SystemClock(),
        JsonlSink(config.log_dir),
        window_id=config.window_id,
        artifact_sha256=config.artifact_sha256,
        process_id=config.process_id,
        process_class=config.process_class,
        family_table=family_table,
        react_owned_paths=react_owned_paths,
    )


def default_environment_identity() -> dict[str, str]:
    """Environment identity sealed into the bundle manifest: hostname,
    interpreter version and platform fingerprint."""
    return {
        "hostname": socket.gethostname(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
    }


# --- Bundle builder and verifier --------------------------------------------


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_request_lines(raw: bytes) -> tuple[list[dict] | None, str | None]:
    """Every line of requests.jsonl as a JSON object, or the concrete
    reason the file is not a well-formed JSONL."""
    if raw and not raw.endswith(b"\n"):
        return None, "truncated final line"
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines = lines[:-1]
    records: list[dict] = []
    for index, line in enumerate(lines, 1):
        if not line.strip():
            return None, f"malformed JSON line {index}"
        try:
            record = json.loads(line)
        except ValueError:
            return None, f"malformed JSON line {index}"
        if not isinstance(record, dict):
            return None, f"malformed JSON line {index}"
        records.append(record)
    return records, None


def _parse_lifecycle_lines(raw: bytes) -> tuple[list[dict] | None, str | None]:
    """Every line of process-lifecycle.jsonl as a valid lifecycle record,
    or the concrete reason it is not."""
    if raw and not raw.endswith(b"\n"):
        return None, "process-lifecycle.jsonl truncated final line"
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines = lines[:-1]
    records: list[dict] = []
    for index, line in enumerate(lines, 1):
        if not line.strip():
            return None, f"malformed lifecycle record {index}"
        try:
            record = json.loads(line)
        except ValueError:
            return None, f"malformed lifecycle record {index}"
        if not isinstance(record, dict):
            return None, f"malformed lifecycle record {index}"
        missing = [field for field in LIFECYCLE_RECORD_FIELDS if field not in record]
        if missing:
            return None, f"malformed lifecycle record {index}: missing field {missing[0]!r}"
        extra = [key for key in record if key not in LIFECYCLE_RECORD_FIELDS]
        if extra:
            return None, f"malformed lifecycle record {index}: extra field {extra[0]!r}"
        if record["event"] not in LIFECYCLE_EVENTS:
            return None, f"malformed lifecycle record {index}: unknown event {record['event']!r}"
        if _parse_utc(record["timestamp_utc"]) is None:
            return None, f"malformed lifecycle record {index}: invalid timestamp"
        records.append(record)
    return records, None


def build_bundle(
    output_dir: Path | str,
    *,
    requests_raw: str | bytes,
    lifecycle_raw: str | bytes,
    window_id: str,
    artifact_sha256: str,
    process_id: str,
    process_class: str | None,
    window_start_utc: str,
    window_end_utc: str,
    environment_identity: Mapping[str, str],
    cohort: tuple[str, ...] | list[str],
    family_table: Mapping[str, str] | None = None,
    prior_artifact: Mapping[str, str] | None = None,
) -> dict:
    """Seal one window bundle: the raw JSONL evidence, its SHA256 digests,
    window markers, expected sequence range, lifecycle, environment and
    artifact identities, classification/frozen-cohort manifests and the
    exact per-traffic-class / per-entry counts.

    Writes ``requests.jsonl``, ``process-lifecycle.jsonl`` and
    ``manifest.json`` under ``output_dir`` and returns the manifest dict.
    The raw must already be sealable (every line parseable, sequences
    integers, traffic classes inside the closed vocabulary); any other
    tampering is the verifier's judgment, not the builder's."""
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    if isinstance(requests_raw, str):
        requests_raw = requests_raw.encode("utf-8")
    if isinstance(lifecycle_raw, str):
        lifecycle_raw = lifecycle_raw.encode("utf-8")

    records, reason = _parse_request_lines(requests_raw)
    if reason:
        raise ValueError(f"cannot seal bundle: {reason}")
    assert records is not None
    for index, record in enumerate(records, 1):
        if not isinstance(record["sequence"], int) or isinstance(record["sequence"], bool):
            raise ValueError(f"cannot seal bundle: record {index} sequence wrong type")
        if record["traffic_class"] not in TRAFFIC_CLASSES:
            raise ValueError(
                f"cannot seal bundle: record {index} traffic class {record['traffic_class']!r}"
            )
        if record["legacy_surface_id"] is not None and not isinstance(
            record["legacy_surface_id"], str
        ):
            raise ValueError(f"cannot seal bundle: record {index} legacy_surface_id wrong type")
    _lifecycle, lifecycle_reason = _parse_lifecycle_lines(lifecycle_raw)
    if lifecycle_reason:
        raise ValueError(f"cannot seal bundle: {lifecycle_reason}")

    table = dict(family_table) if family_table is not None else default_family_table()
    if records:
        sequence_start = min(record["sequence"] for record in records)
        sequence_end = max(record["sequence"] for record in records)
    else:
        sequence_start = sequence_end = 0
    per_class = {cls: 0 for cls in TRAFFIC_CLASSES}
    per_entry = {
        entry.id: {cls: 0 for cls in TRAFFIC_CLASSES}
        for entry in CONTRACTED_LEGACY_ENTRIES
    }
    for record in records:
        per_class[record["traffic_class"]] += 1
        surface = record["legacy_surface_id"]
        if surface is not None:
            per_entry[surface][record["traffic_class"]] += 1

    manifest: dict[str, Any] = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "bundle_id": f"{window_id}-{process_id}",
        "window_id": window_id,
        "window_start_utc": window_start_utc,
        "window_end_utc": window_end_utc,
        "artifact_sha256": artifact_sha256,
        "process_id": process_id,
        "process_class": process_class,
        "environment_identity": dict(environment_identity),
        "requests_raw_sha256": hashlib.sha256(requests_raw).hexdigest(),
        "lifecycle_raw_sha256": hashlib.sha256(lifecycle_raw).hexdigest(),
        "expected_sequence_range": [sequence_start, sequence_end],
        "path_family_table": {path: table[path] for path in sorted(table)},
        "dynamic_path_family": DYNAMIC_KB_FAMILY,
        "frozen_cohort_manifest": {"processes": list(cohort)},
        "classification_manifest": {
            "traffic_classes": list(TRAFFIC_CLASSES),
            "health_path": HEALTH_PATH,
            "unknown_invalidates": True,
        },
        "per_traffic_class_counts": per_class,
        "per_entry_counts": per_entry,
        "prior_artifact_identity": dict(prior_artifact) if prior_artifact else None,
    }
    (output / "requests.jsonl").write_bytes(requests_raw)
    (output / "process-lifecycle.jsonl").write_bytes(lifecycle_raw)
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


@dataclass(frozen=True)
class AcceptanceReport:
    """Non-invalidating acceptance facts: whether any operator-simulated
    record resolved to a catalog entry (zero-caller acceptance fails then),
    reported as per-entry counts.  rollback-probe hits are reported
    separately and never move operator counts."""

    zero_caller_ok: bool
    operator_catalog_hits: dict[str, int]
    rollback_probe_catalog_hits: dict[str, int]
    reason: str | None


@dataclass(frozen=True)
class ObservationVerdict:
    """The public verification result: integrity verdict plus the sealed
    counts.  An invalid window carries the first concrete reason; acceptance
    is only reported for a valid window."""

    valid: bool
    reason: str | None
    line_count: int
    per_traffic_class_counts: dict[str, int]
    per_entry_counts: dict[str, dict[str, int]]
    acceptance: AcceptanceReport | None


def _invalid(reason: str) -> ObservationVerdict:
    return ObservationVerdict(
        valid=False,
        reason=reason,
        line_count=0,
        per_traffic_class_counts={},
        per_entry_counts={},
        acceptance=None,
    )


def _check_request_records(
    records: list[dict], data: Mapping[str, Any]
) -> str | None:
    """Per-record closed-schema, sequence, traffic, correlation, path
    family, route owner, leak and identity checks; the first violation
    with its concrete reason."""
    allowed = tuple(
        data.get("classification_manifest", {}).get("traffic_classes", TRAFFIC_CLASSES)
    )
    table = data.get("path_family_table") or {}
    dynamic_family = data.get("dynamic_path_family", DYNAMIC_KB_FAMILY)
    allowed_families = set(table.values()) | {dynamic_family}
    string_fields = (
        "timestamp_utc",
        "artifact_sha256",
        "process_id",
        "window_id",
        "correlation_id",
        "traffic_class",
        "method",
        "normalized_path_family",
        "matched_route_owner",
    )
    seen_correlations: set[str] = set()
    for index, record in enumerate(records, 1):
        missing = [field for field in REQUEST_RECORD_FIELDS if field not in record]
        if missing:
            return f"record {index} missing field {missing[0]!r}"
        extra = [key for key in record if key not in REQUEST_RECORD_FIELDS]
        if extra:
            return f"record {index} extra field {extra[0]!r}"
        for field in string_fields:
            if not isinstance(record[field], str):
                return f"record {index} field {field!r} wrong type"
        if not isinstance(record["sequence"], int) or isinstance(record["sequence"], bool):
            return f"record {index} field 'sequence' wrong type"
        if not isinstance(record["response_status"], int) or isinstance(
            record["response_status"], bool
        ):
            return f"record {index} field 'response_status' wrong type"
        if record["legacy_surface_id"] is not None and not isinstance(
            record["legacy_surface_id"], str
        ):
            return f"record {index} field 'legacy_surface_id' wrong type"
        if _parse_utc(record["timestamp_utc"]) is None:
            return f"record {index} invalid timestamp"
        if record["traffic_class"] not in allowed:
            return (
                f"record {index} traffic class {record['traffic_class']!r} "
                "outside the five allowed values"
            )
        correlation = record["correlation_id"]
        prefix = f"p{record['process_id']}-"
        if not correlation.startswith(prefix):
            return f"record {index} correlation_id outside its process prefix"
        if not correlation[len(prefix) :].isdigit():
            return f"record {index} malformed correlation_id"
        if correlation in seen_correlations:
            return f"record {index} duplicate correlation_id"
        seen_correlations.add(correlation)
        family = record["normalized_path_family"]
        if "?" in family or "#" in family or "=" in family:
            return f"record {index} path family contains a query value"
        if (
            family.startswith("/api/kb/")
            and family != dynamic_family
            and family not in table.values()
        ):
            return f"record {index} path family keeps concrete section/key text"
        if family not in allowed_families:
            return f"record {index} unregistered path family (raw arbitrary segment)"
        if not record["matched_route_owner"]:
            return f"record {index} matched_route_owner is empty"
        findings = scan_record_for_leaks(record)
        if findings:
            return f"record {index} leak: {findings[0]}"
        if record["artifact_sha256"] != data.get("artifact_sha256"):
            return f"record {index} artifact_sha256 does not match the sealed manifest"
        if record["window_id"] != data.get("window_id"):
            return f"record {index} window_id does not match the sealed manifest"
    sequences = [record["sequence"] for record in records]
    counts: dict[int, int] = {}
    for sequence in sequences:
        counts[sequence] = counts.get(sequence, 0) + 1
    duplicate = next(
        (sequence for sequence, count in counts.items() if count > 1), None
    )
    if duplicate is not None:
        return f"duplicate sequence {duplicate}"
    for index in range(1, len(sequences)):
        if sequences[index] <= sequences[index - 1]:
            return f"sequence order violation at record {index + 1}"
    sealed_range = data.get("expected_sequence_range")
    if (
        not isinstance(sealed_range, list)
        or len(sealed_range) != 2
        or not all(isinstance(value, int) for value in sealed_range)
    ):
        return "manifest expected_sequence_range is missing or malformed"
    range_start, range_end = sealed_range
    for sequence in sequences:
        if not (range_start <= sequence <= range_end):
            return f"sequence {sequence} out of sealed range"
    missing = sorted(set(range(range_start, range_end + 1)) - set(sequences))
    if missing:
        return f"missing sequence {missing[0]}"
    return None


def _check_lifecycle(
    lifecycle: list[dict], data: Mapping[str, Any]
) -> tuple[dict[str, dict[str, list[datetime]]] | None, str | None]:
    """Per-process lifecycle integrity: clean start/end, unique identity
    per window, and membership in the frozen cohort."""
    processes: dict[str, dict[str, list[datetime]]] = {}
    for index, entry in enumerate(lifecycle, 1):
        if entry["window_id"] != data.get("window_id"):
            return None, f"lifecycle record {index} window_id does not match the sealed manifest"
        if entry["artifact_sha256"] != data.get("artifact_sha256"):
            return None, f"lifecycle record {index} artifact_sha256 does not match the sealed manifest"
        bucket = processes.setdefault(entry["process_id"], {"start": [], "end": []})
        bucket[entry["event"]].append(_parse_utc(entry["timestamp_utc"]))
    for process_id in sorted(processes):
        bucket = processes[process_id]
        if not bucket["start"]:
            return None, f"process {process_id} lifecycle missing start"
        if not bucket["end"]:
            return None, f"process {process_id} lifecycle missing end (start without clean end)"
        if len(bucket["start"]) > 1:
            return None, f"process {process_id} reused process identity (duplicate start)"
        if len(bucket["end"]) > 1:
            return None, f"process {process_id} reused process identity (duplicate end)"
        if bucket["end"][0] <= bucket["start"][0]:
            return None, f"process {process_id} lifecycle end before start"
    cohort = set(data.get("frozen_cohort_manifest", {}).get("processes", ()))
    for process_id in processes:
        if process_id not in cohort:
            return None, f"process {process_id} not in the frozen cohort"
    for process_id in sorted(cohort):
        if process_id not in processes:
            return None, f"cohort process {process_id} missing lifecycle"
    return processes, None


def verify_bundle(
    manifest: Path | Mapping[str, Any],
    *,
    requests_raw: str | bytes | None = None,
    lifecycle_raw: str | bytes | None = None,
) -> ObservationVerdict:
    """The public verifier: INVALID for every integrity counterexample with
    a concrete reason; VALID reports the exact sealed counts plus the
    non-invalidating zero-caller acceptance facts.

    ``manifest`` is either the path to ``manifest.json`` (raw evidence is
    read from its sibling files) or the manifest mapping itself (raw
    evidence must then be passed explicitly)."""
    data: dict[str, Any]
    if isinstance(manifest, (str, os.PathLike)):
        manifest_path = Path(manifest)
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return _invalid(f"cannot read manifest: {exc}")
        if not isinstance(loaded, dict):
            return _invalid("manifest is not a JSON object")
        data = loaded
        if requests_raw is None:
            try:
                requests_raw = (manifest_path.parent / "requests.jsonl").read_bytes()
            except OSError as exc:
                return _invalid(f"cannot read requests.jsonl: {exc}")
        if lifecycle_raw is None:
            try:
                lifecycle_raw = (manifest_path.parent / "process-lifecycle.jsonl").read_bytes()
            except OSError as exc:
                return _invalid(f"cannot read process-lifecycle.jsonl: {exc}")
    else:
        if not isinstance(manifest, Mapping):
            return _invalid("manifest is not a JSON object")
        data = dict(manifest)
        if requests_raw is None or lifecycle_raw is None:
            return _invalid(
                "raw evidence must be supplied when the manifest is passed as a mapping"
            )
    if isinstance(requests_raw, str):
        requests_raw = requests_raw.encode("utf-8")
    if isinstance(lifecycle_raw, str):
        lifecycle_raw = lifecycle_raw.encode("utf-8")
    if data.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        return _invalid(f"unsupported bundle schema_version {data.get('schema_version')!r}")
    if data.get("requests_raw_sha256") != hashlib.sha256(requests_raw).hexdigest():
        return _invalid("requests.jsonl digest mismatch (bytes changed after sealing)")
    if data.get("lifecycle_raw_sha256") != hashlib.sha256(lifecycle_raw).hexdigest():
        return _invalid("process-lifecycle.jsonl digest mismatch (bytes changed after sealing)")

    records, reason = _parse_request_lines(requests_raw)
    if reason:
        return _invalid(reason)
    lifecycle, lifecycle_reason = _parse_lifecycle_lines(lifecycle_raw)
    if lifecycle_reason:
        return _invalid(lifecycle_reason)
    assert records is not None
    assert lifecycle is not None

    record_reason = _check_request_records(records, data)
    if record_reason is not None:
        return _invalid(record_reason)
    processes, lifecycle_reason = _check_lifecycle(lifecycle, data)
    if lifecycle_reason is not None:
        return _invalid(lifecycle_reason)
    assert processes is not None
    window_start = _parse_utc(data.get("window_start_utc"))
    window_end = _parse_utc(data.get("window_end_utc"))
    if window_start is None or window_end is None:
        return _invalid("manifest window markers are not valid UTC timestamps")

    for index, record in enumerate(records, 1):
        timestamp = _parse_utc(record["timestamp_utc"])
        assert timestamp is not None
        bucket = processes.get(record["process_id"])
        if bucket is None:
            return _invalid(f"record {index} process {record['process_id']!r} has no lifecycle entry")
        if not (bucket["start"][0] <= timestamp <= bucket["end"][0]):
            return _invalid(f"record {index} timestamp outside process lifecycle")
        if not (window_start <= timestamp <= window_end):
            return _invalid(f"record {index} timestamp outside sealed window range")

    allowed = tuple(
        data.get("classification_manifest", {}).get("traffic_classes", TRAFFIC_CLASSES)
    )
    per_class = {cls: 0 for cls in allowed}
    per_entry: dict[str, dict[str, int]] = {
        entry_id: {cls: 0 for cls in allowed}
        for entry_id in (data.get("per_entry_counts") or {})
    }
    for record in records:
        per_class[record["traffic_class"]] += 1
        surface = record["legacy_surface_id"]
        if surface is not None:
            per_entry[surface][record["traffic_class"]] += 1
    if per_class != data.get("per_traffic_class_counts"):
        return _invalid("sealed per-traffic-class counts mismatch")
    if per_entry != data.get("per_entry_counts"):
        return _invalid("sealed per-entry counts mismatch")
    if per_class.get("operator-simulated", 0) == 0:
        return _invalid("operator population (operator-simulated denominator) is empty")

    operator_hits: dict[str, int] = {}
    rollback_hits: dict[str, int] = {}
    for record in records:
        surface = record["legacy_surface_id"]
        if surface is None:
            continue
        if record["traffic_class"] == "operator-simulated":
            operator_hits[surface] = operator_hits.get(surface, 0) + 1
        elif record["traffic_class"] == "rollback-probe":
            rollback_hits[surface] = rollback_hits.get(surface, 0) + 1
    zero_caller_ok = not operator_hits
    acceptance_reason = None
    if not zero_caller_ok:
        acceptance_reason = (
            "operator-simulated traffic resolves to catalog entries: "
            + ", ".join(sorted(operator_hits))
        )
    acceptance = AcceptanceReport(
        zero_caller_ok=zero_caller_ok,
        operator_catalog_hits=operator_hits,
        rollback_probe_catalog_hits=rollback_hits,
        reason=acceptance_reason,
    )
    return ObservationVerdict(
        valid=True,
        reason=None,
        line_count=len(records),
        per_traffic_class_counts=per_class,
        per_entry_counts=per_entry,
        acceptance=acceptance,
    )


# --- CLI --------------------------------------------------------------------


def _cli_verify(args: argparse.Namespace) -> int:
    verdict = verify_bundle(Path(args.manifest))
    if verdict.valid:
        acceptance = (
            "ok"
            if verdict.acceptance is not None and verdict.acceptance.zero_caller_ok
            else "fail"
        )
        print(
            f"verdict=valid requests={verdict.line_count} "
            f"classes={json.dumps(verdict.per_traffic_class_counts, sort_keys=True)} "
            f"acceptance={acceptance}"
        )
        return 0
    print(f"verdict=invalid reason={verdict.reason}")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m task4_consistency.web.observation",
        description="Closed observation telemetry core: window bundle verification.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify", help="verify a sealed observation bundle")
    verify.add_argument("--manifest", required=True, help="path to manifest.json")
    verify.set_defaults(handler=_cli_verify)
    args = parser.parse_args(argv)
    return int(args.handler(args))


# --- CLI ---------------------------------------------------------------------

if __name__ == "__main__":
    raise SystemExit(main())
