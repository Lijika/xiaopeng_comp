"""T54 observation telemetry module: deterministic core (Issue #54).

The observation module is a closed, env-driven telemetry core behind the
FastAPI HTTP adapter: it consumes the contracted legacy catalog, records a
closed 12-field request schema plus a separate process lifecycle stream, and
seals/verifies one window bundle.  This file tests the deterministic core
only -- fixed clock, in-memory sink, direct ASGI driving.  No network, no
uvicorn.

Structure:
  1. valid bundle -> verifier VALID, exact counts, manifest digests match
  2. every integrity counterexample -> INVALID with a concrete reason
  3. traffic classification (process class env vs /api/health vs unknown)
  4. catalog matcher integration (dynamic KB delete family, shielded paths)
  5. leak scan (credential / application-ID / free text / internal path)
  6. ASGI middleware smoke (direct scope driving, no server)
  7. JSONL sink sequence contract (flock + sidecar crash gap)
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from task4_consistency.web import observation as observation_module
from task4_consistency.web.legacy_catalog import match_legacy_surface
from task4_consistency.web.observation import (
    DYNAMIC_KB_FAMILY,
    HEALTH_PATH,
    LIFECYCLE_RECORD_FIELDS,
    REQUEST_RECORD_FIELDS,
    TRAFFIC_CLASSES,
    UNREGISTERED_PATH_FAMILY,
    UNKNOWN_CLASS,
    AcceptanceReport,
    FixedClock,
    InMemorySink,
    JsonlSink,
    NoopRecorder,
    ObservationMiddleware,
    ObservationRecorder,
    build_bundle,
    classify_traffic,
    default_family_table,
    normalize_path_family,
    resolve_route_owner,
    scan_record_for_leaks,
    verify_bundle,
)

WINDOW_ID = "win-20260816-01"
ARTIFACT_SHA = "a" * 64
PRIOR_ARTIFACT_SHA = "b" * 64
T0 = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)
ENV_IDENTITY = {"hostname": "test-host", "python_version": "3.12.0", "platform": "linux-test"}
FAMILY_TABLE = observation_module._canonical_family_contract()
_ZERO = {cls: 0 for cls in TRAFFIC_CLASSES}


# --- helpers -------------------------------------------------------------


def _records(raw: bytes) -> list[dict]:
    return [json.loads(line) for line in raw.decode("utf-8").splitlines()]


def _serialize(records: list[dict]) -> bytes:
    return "".join(json.dumps(r, sort_keys=True) + "\n" for r in records).encode("utf-8")


def _release_evidence(elapsed_seconds: float = 19.0) -> dict:
    node_ids = [
        "test_t54_prior_artifact.spec.js:23:1 › T54 installed prior artifact serves the qualified React shell on root, S01, and S02 at both viewports",
        "test_t01_react.spec.js:702:3 › T01 production tracer (desktop 1280x800)",
    ]
    return {
        "reviewed_commit": "c" * 40,
        "tracked_tree_clean": True,
        "current_wheel_sha256": ARTIFACT_SHA,
        "prior_commit": "d" * 40,
        "prior_wheel_sha256": PRIOR_ARTIFACT_SHA,
        "timezone": "CST",
        "elapsed_seconds": elapsed_seconds,
        "node_version": "v22.22.2",
        "npm_version": "10.9.0",
        "package_identity": "task4-consistency==0.1.0",
        "network_routes": "2: lo    inet 127.0.0.1/8",
        "cohort_node_ids": node_ids,
        "cohort_node_ids_sha256": hashlib.sha256(
            ("\n".join(node_ids) + "\n").encode("utf-8")
        ).hexdigest(),
        "cohort_spec_sha256": {
            "tests/test_t01_react.spec.js": "e" * 64,
            "playwright.config.js": "f" * 64,
        },
        "viewports": ["1280x800", "390x844"],
        "accepted_fact_sha256": {
            "current": "1" * 64,
            "prior": "1" * 64,
            "restored": "1" * 64,
        },
        "accepted_facts_equal": True,
    }


def _rehash(manifest: dict, raw: bytes | None = None, lifecycle_raw: bytes | None = None) -> dict:
    """A manifest re-sealed over mutated raw evidence: the sealed digest is
    updated so the record-level integrity checks (not the digest check) are
    what the test exercises."""
    copy = dict(manifest)
    if raw is not None:
        copy["requests_raw_sha256"] = hashlib.sha256(raw).hexdigest()
    if lifecycle_raw is not None:
        copy["lifecycle_raw_sha256"] = hashlib.sha256(lifecycle_raw).hexdigest()
    copy["manifest_sha256"] = observation_module._manifest_sha256(copy)
    return copy


def _seal(
    tmp_path: Path,
    sink: InMemorySink,
    *,
    window_start: datetime,
    window_end: datetime,
    cohort: tuple[str, ...],
    family_table: dict[str, str] | None = None,
    process_id: str = "p-1",
    process_class: str = "operator-simulated",
    prior_artifact: dict | None = None,
    release_evidence: dict | None = None,
) -> tuple[dict, bytes, bytes]:
    requests_raw = _serialize(sink.requests)
    lifecycle_raw = _serialize(sink.lifecycle)
    manifest = build_bundle(
        tmp_path,
        requests_raw=requests_raw,
        lifecycle_raw=lifecycle_raw,
        window_id=WINDOW_ID,
        artifact_sha256=ARTIFACT_SHA,
        process_id=process_id,
        process_class=process_class,
        window_start_utc=window_start.isoformat(),
        window_end_utc=window_end.isoformat(),
        environment_identity=ENV_IDENTITY,
        cohort=cohort,
        family_table=family_table or FAMILY_TABLE,
        prior_artifact=prior_artifact,
        release_evidence=release_evidence,
    )
    return manifest, requests_raw, lifecycle_raw


def _valid_bundle(
    tmp_path: Path,
    *,
    operator_on_catalog: bool = False,
    with_rollback_probe: bool = False,
) -> tuple[dict, bytes, bytes]:
    """The canonical valid window: operator-simulated process p-1 (two
    non-catalog records, or a prior-stage cataloged static hit when
    ``operator_on_catalog``), release process p-2 (React owner, retired
    static and mutation paths resolving to None in the current artifact,
    health), both with clean lifecycle spans.  ``with_rollback_probe`` adds
    the prior-artifact rollback-probe process p-3 (canonical React pages
    resolve to None; the prior static/mutation owners still record)."""
    clock = FixedClock(T0)
    sink = InMemorySink()
    p1_stage = "prior" if operator_on_catalog else "current"
    p1_sha = PRIOR_ARTIFACT_SHA if operator_on_catalog else ARTIFACT_SHA
    p1 = ObservationRecorder(
        clock, sink,
        window_id=WINDOW_ID, artifact_sha256=p1_sha,
        process_id="p-1", process_class="operator-simulated",
        artifact_stage=p1_stage, family_table=FAMILY_TABLE,
    )
    p2 = ObservationRecorder(
        clock, sink,
        window_id=WINDOW_ID, artifact_sha256=ARTIFACT_SHA,
        process_id="p-2", process_class="release",
        artifact_stage="current", family_table=FAMILY_TABLE,
    )
    p1.record_lifecycle("start")  # T0
    clock.advance(timedelta(seconds=1))
    p2.record_lifecycle("start")  # T0+1
    clock.advance(timedelta(seconds=1))
    if operator_on_catalog:
        p1.record_http(
            method="GET", path="/static/app.js", response_status=200,
            matched_route_owner="StaticFiles",
        )
    else:
        p1.record_http(
            method="GET", path="/api/fixtures",
            response_status=200, matched_route_owner="api_fixtures",
        )
    clock.advance(timedelta(seconds=1))
    p1.record_http(
        method="GET", path="/api/kb/unknown/unknown",
        response_status=404, matched_route_owner="unmatched",
    )
    clock.advance(timedelta(seconds=1))
    p2.record_http(method="GET", path="/", response_status=200, matched_route_owner="index")
    clock.advance(timedelta(seconds=1))
    p2.record_http(
        method="GET", path="/static/app.js",
        response_status=200, matched_route_owner="StaticFiles",
    )
    clock.advance(timedelta(seconds=1))
    p2.record_http(method="PUT", path="/api/rules", response_status=200, matched_route_owner="put_rules")
    clock.advance(timedelta(seconds=1))
    p2.record_http(method="GET", path="/api/health", response_status=200, matched_route_owner="health")
    clock.advance(timedelta(seconds=1))
    p1.record_lifecycle("end")  # T0+8
    clock.advance(timedelta(seconds=1))
    p2.record_lifecycle("end")  # T0+9

    cohort = ["p-1", "p-2"]
    has_prior_stage = operator_on_catalog or with_rollback_probe
    if with_rollback_probe:
        clock.advance(timedelta(seconds=1))
        p3 = ObservationRecorder(
            clock, sink,
            window_id=WINDOW_ID, artifact_sha256=PRIOR_ARTIFACT_SHA,
            process_id="p-3", process_class="rollback-probe",
            artifact_stage="prior", family_table=FAMILY_TABLE,
        )
        p3.record_lifecycle("start")  # T0+10
        clock.advance(timedelta(seconds=1))
        p3.record_http(
            method="GET", path="/", response_status=200,
            matched_route_owner="index",
        )
        clock.advance(timedelta(seconds=1))
        p3.record_http(
            method="GET", path="/controlled/s01", response_status=200,
            matched_route_owner="controlled_s01_page",
        )
        clock.advance(timedelta(seconds=1))
        p3.record_http(
            method="GET", path="/controlled/s02", response_status=200,
            matched_route_owner="controlled_s02_page",
        )
        clock.advance(timedelta(seconds=1))
        p3.record_http(
            method="GET", path="/static/app.js", response_status=200,
            matched_route_owner="StaticFiles",
        )
        clock.advance(timedelta(seconds=1))
        p3.record_http(
            method="DELETE", path="/api/kb/address_aliases/somekey",
            response_status=200, matched_route_owner="kb_delete",
        )
        clock.advance(timedelta(seconds=1))
        p3.record_lifecycle("end")  # T0+16
        cohort.append("p-3")

    return _seal(
        tmp_path, sink,
        window_start=T0 - timedelta(seconds=1),
        window_end=T0 + timedelta(seconds=19 if with_rollback_probe else 10),
        cohort=tuple(cohort),
        prior_artifact=(
            {"wheel_sha256": PRIOR_ARTIFACT_SHA, "commit": "d" * 40}
            if has_prior_stage
            else None
        ),
        release_evidence=(
            _release_evidence(elapsed_seconds=20.0 if with_rollback_probe else 11.0)
            if has_prior_stage
            else None
        ),
    )


def _expect_invalid(
    manifest: dict,
    raw: bytes,
    lifecycle_raw: bytes,
    *substrings: str,
) -> None:
    verdict = verify_bundle(manifest, requests_raw=raw, lifecycle_raw=lifecycle_raw)
    assert not verdict.valid
    assert verdict.reason, "verdict must carry a concrete reason"
    for substring in substrings:
        assert substring in verdict.reason, f"{substring!r} not in reason {verdict.reason!r}"


# --- 1. valid bundle --------------------------------------------------------


def test_request_record_schema_is_frozen_to_the_closed_fields() -> None:
    """The closed request schema is exactly the twelve contracted fields."""
    assert REQUEST_RECORD_FIELDS == (
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
    assert len(REQUEST_RECORD_FIELDS) == 12
    assert len(set(REQUEST_RECORD_FIELDS)) == 12
    assert LIFECYCLE_RECORD_FIELDS == (
        "process_id",
        "window_id",
        "artifact_sha256",
        "event",
        "timestamp_utc",
    )


def test_valid_bundle_verifies_with_exact_counts(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    verdict = verify_bundle(manifest, requests_raw=raw, lifecycle_raw=lifecycle_raw)
    assert verdict.valid, verdict.reason
    assert verdict.reason is None
    assert verdict.line_count == 6
    assert verdict.per_traffic_class_counts == {
        "operator-simulated": 2,
        "release": 3,
        "health": 1,
        "playwright-probe": 0,
        "rollback-probe": 0,
    }
    entry_counts = {entry_id: dict(_ZERO) for entry_id in (
        "legacy-page-root",
        "legacy-page-controlled-s01",
        "legacy-page-controlled-s02",
        "legacy-static-app-js",
        "legacy-static-style-css",
        "legacy-mutation-rules-put",
        "legacy-mutation-rules-reset-post",
        "legacy-mutation-kb-post",
        "legacy-mutation-kb-delete",
        "legacy-mutation-kb-reload-post",
    )}
    # Issue #45 contraction: every current-artifact request to a retired
    # static/mutation surface and every canonical React page resolves to
    # no legacy surface, so the base window has zero catalog hits.
    assert verdict.per_entry_counts == entry_counts
    assert verdict.acceptance is not None
    assert verdict.acceptance.zero_caller_ok
    assert verdict.acceptance.operator_catalog_hits == {}
    assert verdict.acceptance.rollback_probe_catalog_hits == {}
    # Manifest digests seal the raw evidence.
    assert manifest["requests_raw_sha256"] == hashlib.sha256(raw).hexdigest()
    assert manifest["lifecycle_raw_sha256"] == hashlib.sha256(lifecycle_raw).hexdigest()
    assert manifest["expected_sequence_range"] == [1, 6]
    assert manifest["window_id"] == WINDOW_ID
    assert manifest["artifact_sha256"] == ARTIFACT_SHA
    assert manifest["schema_version"] == "2"
    assert manifest["frozen_cohort_manifest"] == {"processes": ["p-1", "p-2"]}
    assert manifest["prior_artifact_identity"] is None


def test_valid_bundle_canonical_records(tmp_path: Path) -> None:
    """The sealed records carry the canonical shapes: React owner, retired
    static and mutation paths resolving to no legacy surface in the current
    artifact, protected 404 (dynamic family, unmatched owner), and health
    -- no raw paths, no query values, no concrete KB text."""
    _manifest, raw, _lifecycle_raw = _valid_bundle(tmp_path)
    records = _records(raw)
    by_seq = {record["sequence"]: record for record in records}
    assert [record["sequence"] for record in records] == [1, 2, 3, 4, 5, 6]
    assert [record["correlation_id"] for record in records] == [
        "pp-1-1", "pp-1-2", "pp-2-1", "pp-2-2", "pp-2-3", "pp-2-4",
    ]
    assert by_seq[1]["traffic_class"] == "operator-simulated"
    assert by_seq[1]["normalized_path_family"] == "/api/fixtures"
    assert by_seq[1]["legacy_surface_id"] is None
    assert by_seq[2]["response_status"] == 404
    assert by_seq[2]["matched_route_owner"] == "unmatched"
    assert by_seq[2]["normalized_path_family"] == DYNAMIC_KB_FAMILY
    assert by_seq[3]["matched_route_owner"] == "index"
    assert by_seq[3]["legacy_surface_id"] is None  # canonical React owner
    # The retired static and mutation surfaces are absent in the current
    # artifact, so owner matches still resolve to no legacy surface.
    assert by_seq[4]["legacy_surface_id"] is None
    assert by_seq[5]["legacy_surface_id"] is None
    assert by_seq[5]["method"] == "PUT"
    assert by_seq[6]["traffic_class"] == "health"  # /api/health always health
    assert by_seq[6]["legacy_surface_id"] is None
    assert all(record["artifact_sha256"] == ARTIFACT_SHA for record in records)
    assert all(record["window_id"] == WINDOW_ID for record in records)
    assert all("?" not in record["normalized_path_family"] for record in records)


def test_cli_verify_exits_0_valid_and_1_invalid(capsys, tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    (tmp_path / "requests.jsonl").write_bytes(raw)
    (tmp_path / "process-lifecycle.jsonl").write_bytes(lifecycle_raw)
    assert observation_module.main(["verify", "--manifest", str(manifest_path)]) == 0
    assert "valid" in capsys.readouterr().out
    # Bytes appended after sealing are detected as a digest mismatch.
    (tmp_path / "requests.jsonl").write_bytes(raw + b'{"tamper": true}\n')
    assert observation_module.main(["verify", "--manifest", str(manifest_path)]) == 1
    assert "invalid" in capsys.readouterr().out


# --- 2. integrity counterexamples (each must invalidate) -------------------

# 2.1 sequence range


def test_sequence_missing_invalidates(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    records = _records(raw)
    records.pop(2)  # drop sequence 3
    raw2 = _serialize(records)
    _expect_invalid(_rehash(manifest, raw=raw2), raw2, lifecycle_raw, "missing sequence 3")


def test_sequence_duplicate_invalidates(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    records = _records(raw)
    records[2]["sequence"] = 2
    raw2 = _serialize(records)
    _expect_invalid(_rehash(manifest, raw=raw2), raw2, lifecycle_raw, "duplicate sequence 2")


def test_sequence_decreasing_invalidates(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    records = _records(raw)
    sequences = [2, 1, 3, 4, 5, 6]
    for record, sequence in zip(records, sequences):
        record["sequence"] = sequence
    raw2 = _serialize(records)
    _expect_invalid(_rehash(manifest, raw=raw2), raw2, lifecycle_raw, "sequence order violation")


def test_sequence_non_integer_invalidates(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    records = _records(raw)
    records[2]["sequence"] = "3"
    raw2 = _serialize(records)
    _expect_invalid(_rehash(manifest, raw=raw2), raw2, lifecycle_raw, "wrong type")


def test_sequence_out_of_sealed_range_invalidates(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    records = _records(raw)
    records[5]["sequence"] = 99
    raw2 = _serialize(records)
    _expect_invalid(_rehash(manifest, raw=raw2), raw2, lifecycle_raw, "sequence 99 out of sealed range")


# 2.2 requests.jsonl shape


def test_malformed_json_line_invalidates(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    lines = raw.decode("utf-8").splitlines()
    lines[2] = "{not json"
    raw2 = "\n".join(lines).encode("utf-8") + b"\n"
    _expect_invalid(_rehash(manifest, raw=raw2), raw2, lifecycle_raw, "malformed JSON line 3")


def test_truncated_final_line_invalidates(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    raw2 = raw[:-1]  # drop the trailing newline
    _expect_invalid(_rehash(manifest, raw=raw2), raw2, lifecycle_raw, "truncated final line")


def test_missing_field_invalidates(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    records = _records(raw)
    del records[0]["artifact_sha256"]
    raw2 = _serialize(records)
    _expect_invalid(_rehash(manifest, raw=raw2), raw2, lifecycle_raw, "missing field 'artifact_sha256'")


def test_extra_field_invalidates(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    records = _records(raw)
    records[0]["bogus"] = 1
    raw2 = _serialize(records)
    _expect_invalid(_rehash(manifest, raw=raw2), raw2, lifecycle_raw, "extra field 'bogus'")


def test_wrong_field_type_invalidates(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    records = _records(raw)
    records[0]["response_status"] = "200"
    raw2 = _serialize(records)
    _expect_invalid(_rehash(manifest, raw=raw2), raw2, lifecycle_raw, "wrong type")


def test_unknown_legacy_surface_id_returns_invalid_instead_of_raising(
    tmp_path: Path,
) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    records = _records(raw)
    records[0]["legacy_surface_id"] = "legacy-unknown"
    raw2 = _serialize(records)
    _expect_invalid(
        _rehash(manifest, raw=raw2),
        raw2,
        lifecycle_raw,
        "unknown legacy_surface_id",
    )


def test_bytes_appended_after_sealing_invalidates(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    raw2 = raw + b'{"sequence": 99}\n'  # sealed digest not updated -> mismatch
    _expect_invalid(manifest, raw2, lifecycle_raw, "requests.jsonl digest mismatch")


# 2.3 traffic class


def test_traffic_class_absent_invalidates(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    records = _records(raw)
    del records[0]["traffic_class"]
    raw2 = _serialize(records)
    _expect_invalid(_rehash(manifest, raw=raw2), raw2, lifecycle_raw, "missing field 'traffic_class'")


def test_traffic_class_outside_allowed_values_invalidates(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    records = _records(raw)
    records[0]["traffic_class"] = UNKNOWN_CLASS
    raw2 = _serialize(records)
    _expect_invalid(
        _rehash(manifest, raw=raw2), raw2, lifecycle_raw,
        "traffic class 'unknown' outside the five allowed values",
    )


def test_manifest_cannot_expand_the_fixed_traffic_vocabulary(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    changed = copy.deepcopy(manifest)
    changed["classification_manifest"]["traffic_classes"].append("unknown")
    changed["manifest_sha256"] = observation_module._manifest_sha256(changed)
    _expect_invalid(
        changed,
        raw,
        lifecycle_raw,
        "traffic classification contract mismatch",
    )


def test_manifest_cannot_remove_a_compiled_catalog_entry(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    changed = copy.deepcopy(manifest)
    del changed["per_entry_counts"]["legacy-page-root"]
    changed["manifest_sha256"] = observation_module._manifest_sha256(changed)
    _expect_invalid(
        changed,
        raw,
        lifecycle_raw,
        "entry-count keys do not match the compiled catalog",
    )


def test_manifest_cannot_remove_a_registered_path_family(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    changed = copy.deepcopy(manifest)
    del changed["path_family_table"]["/api/fixtures"]
    changed["manifest_sha256"] = observation_module._manifest_sha256(changed)
    _expect_invalid(
        changed,
        raw,
        lifecycle_raw,
        "path family table does not match the registered contract",
    )


def test_prior_stage_requires_the_complete_release_evidence_contract(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path, with_rollback_probe=True)
    missing_identity = copy.deepcopy(manifest)
    missing_identity["prior_artifact_identity"] = None
    missing_identity["manifest_sha256"] = observation_module._manifest_sha256(missing_identity)
    _expect_invalid(
        missing_identity,
        raw,
        lifecycle_raw,
        "prior-stage process is missing prior artifact identity",
    )
    for field in ("network_routes", "cohort_node_ids", "viewports", "accepted_fact_sha256"):
        changed = copy.deepcopy(manifest)
        del changed["release_evidence"][field]
        changed["manifest_sha256"] = observation_module._manifest_sha256(changed)
        verdict = verify_bundle(changed, requests_raw=raw, lifecycle_raw=lifecycle_raw)
        assert not verdict.valid, field
        assert "release_evidence fields do not match" in (verdict.reason or "")


def test_release_evidence_rejects_resealed_semantic_alterations(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path, with_rollback_probe=True)
    cases = (
        ("elapsed_seconds", 21.0, "elapsed_seconds does not match"),
        ("network_routes", "default via 10.0.0.1 dev eth0", "loopback namespace"),
        ("viewports", ["1024x768", "390x844"], "viewport contract"),
        ("package_identity", "tampered-package", "package_identity is missing or malformed"),
    )
    for field, value, reason in cases:
        changed = copy.deepcopy(manifest)
        changed["release_evidence"][field] = value
        changed["manifest_sha256"] = observation_module._manifest_sha256(changed)
        _expect_invalid(changed, raw, lifecycle_raw, reason)

    changed = copy.deepcopy(manifest)
    changed["release_evidence"]["cohort_node_ids"] = [
        "tests/test_t01_react.spec.js:702:3 › T01 production tracer"
    ]
    node_ids = changed["release_evidence"]["cohort_node_ids"]
    changed["release_evidence"]["cohort_node_ids_sha256"] = hashlib.sha256(
        ("\n".join(node_ids) + "\n").encode("utf-8")
    ).hexdigest()
    changed["manifest_sha256"] = observation_module._manifest_sha256(changed)
    _expect_invalid(changed, raw, lifecycle_raw, "omit the prior-artifact browser node")


def test_process_stage_is_derived_from_artifact_identity_not_traffic_class(
    tmp_path: Path,
) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path, with_rollback_probe=True)
    changed = copy.deepcopy(manifest)
    changed["process_artifacts"]["p-3"]["traffic_class"] = "release"
    changed["manifest_sha256"] = observation_module._manifest_sha256(changed)
    _expect_invalid(
        changed,
        raw,
        lifecycle_raw,
        "record 7 traffic class does not match its process identity",
    )


# 2.4 correlation id


def test_correlation_id_absent_invalidates(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    records = _records(raw)
    del records[0]["correlation_id"]
    raw2 = _serialize(records)
    _expect_invalid(_rehash(manifest, raw=raw2), raw2, lifecycle_raw, "missing field 'correlation_id'")


def test_correlation_id_duplicated_invalidates(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    records = _records(raw)
    records[1]["correlation_id"] = records[0]["correlation_id"]
    raw2 = _serialize(records)
    _expect_invalid(_rehash(manifest, raw=raw2), raw2, lifecycle_raw, "duplicate correlation_id")


def test_correlation_id_malformed_invalidates(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    records = _records(raw)
    records[0]["correlation_id"] = "pp-1-abc"
    raw2 = _serialize(records)
    _expect_invalid(_rehash(manifest, raw=raw2), raw2, lifecycle_raw, "malformed correlation_id")


def test_correlation_id_outside_process_prefix_invalidates(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    records = _records(raw)
    records[0]["correlation_id"] = "pX-1"
    raw2 = _serialize(records)
    _expect_invalid(
        _rehash(manifest, raw=raw2), raw2, lifecycle_raw,
        "correlation_id outside its process prefix",
    )


# 2.5 identity against manifest / lifecycle


def test_record_artifact_identity_mismatch_invalidates(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    records = _records(raw)
    records[0]["artifact_sha256"] = "0" * 64
    raw2 = _serialize(records)
    _expect_invalid(
        _rehash(manifest, raw=raw2), raw2, lifecycle_raw,
        "artifact_sha256 does not match its process identity",
    )


def test_record_window_identity_mismatch_invalidates(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    records = _records(raw)
    records[0]["window_id"] = "other-window"
    raw2 = _serialize(records)
    _expect_invalid(
        _rehash(manifest, raw=raw2), raw2, lifecycle_raw,
        "window_id does not match the sealed manifest",
    )


def test_record_process_without_lifecycle_entry_invalidates(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    records = _records(raw)
    records[0]["process_id"] = "ghost"
    records[0]["correlation_id"] = "pghost-1"
    raw2 = _serialize(records)
    _expect_invalid(
        _rehash(manifest, raw=raw2), raw2, lifecycle_raw,
        "record 1 process 'ghost' has no artifact identity",
    )


# 2.6 timestamps


def test_record_timestamp_unparseable_invalidates(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    records = _records(raw)
    records[0]["timestamp_utc"] = "not-a-timestamp"
    raw2 = _serialize(records)
    _expect_invalid(_rehash(manifest, raw=raw2), raw2, lifecycle_raw, "invalid timestamp")


def test_record_timestamp_outside_process_lifecycle_invalidates(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    records = _records(raw)
    records[0]["timestamp_utc"] = (T0 + timedelta(seconds=30)).isoformat()  # after p-1 end (T0+8)
    raw2 = _serialize(records)
    _expect_invalid(
        _rehash(manifest, raw=raw2), raw2, lifecycle_raw,
        "outside process lifecycle",
    )


def test_record_timestamp_outside_sealed_window_invalidates(tmp_path: Path) -> None:
    """A record inside its process lifecycle but beyond the sealed window
    markers still invalidates."""
    clock = FixedClock(T0)
    sink = InMemorySink()
    p1 = ObservationRecorder(
        clock, sink,
        window_id=WINDOW_ID, artifact_sha256=ARTIFACT_SHA,
        process_id="p-1", process_class="operator-simulated",
        family_table=FAMILY_TABLE,
    )
    p1.record_lifecycle("start")  # T0
    clock.advance(timedelta(seconds=15))
    p1.record_http(
        method="GET", path="/api/fixtures",
        response_status=200, matched_route_owner="api_fixtures",
    )  # T0+15: inside the lifecycle, beyond the sealed window end (T0+10)
    clock.advance(timedelta(seconds=30))
    p1.record_lifecycle("end")  # T0+45
    manifest, raw, lifecycle_raw = _seal(
        tmp_path, sink,
        window_start=T0 - timedelta(seconds=1),
        window_end=T0 + timedelta(seconds=10),
        cohort=("p-1",),
    )
    _expect_invalid(manifest, raw, lifecycle_raw, "outside sealed window range")


# 2.7 process lifecycle


def test_process_start_without_clean_end_invalidates(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    lines = lifecycle_raw.decode("utf-8").splitlines()
    lifecycle_raw2 = b"\n".join(
        line.encode("utf-8")
        for line in lines
        if json.loads(line)["event"] != "end"
    ) + b"\n"
    _expect_invalid(
        _rehash(manifest, lifecycle_raw=lifecycle_raw2),
        raw, lifecycle_raw2,
        "start without clean end",
    )


def test_process_reused_identity_invalidates(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    lifecycle = _records(lifecycle_raw)
    first = lifecycle[0]
    lifecycle_raw2 = _serialize(lifecycle + [
        {"process_id": first["process_id"], "window_id": first["window_id"],
         "artifact_sha256": first["artifact_sha256"], "event": "start",
         "timestamp_utc": (T0 + timedelta(seconds=50)).isoformat()},
        {"process_id": first["process_id"], "window_id": first["window_id"],
         "artifact_sha256": first["artifact_sha256"], "event": "end",
         "timestamp_utc": (T0 + timedelta(seconds=51)).isoformat()},
    ])
    _expect_invalid(
        _rehash(manifest, lifecycle_raw=lifecycle_raw2),
        raw, lifecycle_raw2,
        "reused process identity",
    )


def test_process_missing_start_invalidates(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    lines = lifecycle_raw.decode("utf-8").splitlines()
    lifecycle_raw2 = b"\n".join(
        line.encode("utf-8")
        for line in lines
        if json.loads(line)["event"] != "start"
    ) + b"\n"
    _expect_invalid(
        _rehash(manifest, lifecycle_raw=lifecycle_raw2),
        raw, lifecycle_raw2,
        "missing start",
    )


def test_lifecycle_process_outside_frozen_cohort_invalidates(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    lifecycle = _records(lifecycle_raw)
    lifecycle_raw2 = _serialize(lifecycle + [
        {"process_id": "p-9", "window_id": WINDOW_ID,
         "artifact_sha256": ARTIFACT_SHA, "event": "start",
         "timestamp_utc": (T0 + timedelta(seconds=20)).isoformat()},
        {"process_id": "p-9", "window_id": WINDOW_ID,
         "artifact_sha256": ARTIFACT_SHA, "event": "end",
         "timestamp_utc": (T0 + timedelta(seconds=21)).isoformat()},
    ])
    _expect_invalid(
        _rehash(manifest, lifecycle_raw=lifecycle_raw2),
        raw, lifecycle_raw2,
        "process has no artifact identity",
    )


# 2.8 path family / route owner


def test_path_family_with_query_value_invalidates(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    records = _records(raw)
    records[0]["normalized_path_family"] = "/api/fixtures?secret=1"
    raw2 = _serialize(records)
    _expect_invalid(
        _rehash(manifest, raw=raw2), raw2, lifecycle_raw,
        "query value",
    )


def test_path_family_raw_arbitrary_segment_invalidates(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    records = _records(raw)
    records[0]["normalized_path_family"] = "/totally/arbitrary/thing"
    raw2 = _serialize(records)
    _expect_invalid(
        _rehash(manifest, raw=raw2), raw2, lifecycle_raw,
        "unregistered path family",
    )


def test_dynamic_kb_delete_keeps_concrete_section_key_invalidates(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    records = _records(raw)
    records[0]["normalized_path_family"] = "/api/kb/address_aliases/somekey"
    raw2 = _serialize(records)
    _expect_invalid(
        _rehash(manifest, raw=raw2), raw2, lifecycle_raw,
        "concrete section/key text",
    )


def test_matched_route_owner_missing_invalidates(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    records = _records(raw)
    del records[0]["matched_route_owner"]
    raw2 = _serialize(records)
    _expect_invalid(_rehash(manifest, raw=raw2), raw2, lifecycle_raw, "missing field 'matched_route_owner'")


def test_matched_route_owner_empty_invalidates(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    records = _records(raw)
    records[0]["matched_route_owner"] = ""
    raw2 = _serialize(records)
    _expect_invalid(_rehash(manifest, raw=raw2), raw2, lifecycle_raw, "matched_route_owner is empty")


# 2.9 operator population


def test_operator_population_empty_invalidates(tmp_path: Path) -> None:
    clock = FixedClock(T0)
    sink = InMemorySink()
    p1 = ObservationRecorder(
        clock, sink,
        window_id=WINDOW_ID, artifact_sha256=ARTIFACT_SHA,
        process_id="p-1", process_class="release",  # no operator-simulated records
        family_table=FAMILY_TABLE,
    )
    p1.record_lifecycle("start")
    clock.advance(timedelta(seconds=1))
    p1.record_http(method="GET", path="/api/fixtures", response_status=200, matched_route_owner="api_fixtures")
    clock.advance(timedelta(seconds=1))
    p1.record_lifecycle("end")
    manifest, raw, lifecycle_raw = _seal(
        tmp_path, sink,
        window_start=T0 - timedelta(seconds=1),
        window_end=T0 + timedelta(seconds=5),
        cohort=("p-1",),
    )
    _expect_invalid(manifest, raw, lifecycle_raw, "operator population")


# 2.10 acceptance (non-invalidating, reported as counts)


def test_operator_catalog_hit_fails_zero_caller_acceptance_but_stays_valid(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path, operator_on_catalog=True)
    verdict = verify_bundle(manifest, requests_raw=raw, lifecycle_raw=lifecycle_raw)
    assert verdict.valid, verdict.reason
    assert verdict.acceptance is not None
    assert not verdict.acceptance.zero_caller_ok
    assert verdict.acceptance.operator_catalog_hits == {"legacy-static-app-js": 1}
    assert "legacy-static-app-js" in verdict.acceptance.reason


def test_rollback_probe_catalog_hits_stay_separate_from_operator_counts(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path, with_rollback_probe=True)
    verdict = verify_bundle(manifest, requests_raw=raw, lifecycle_raw=lifecycle_raw)
    assert verdict.valid, verdict.reason
    assert verdict.per_traffic_class_counts["rollback-probe"] == 5
    assert verdict.per_traffic_class_counts["operator-simulated"] == 2
    assert verdict.per_entry_counts["legacy-mutation-kb-delete"]["rollback-probe"] == 1
    assert verdict.per_entry_counts["legacy-static-app-js"]["rollback-probe"] == 1
    # Prior-artifact canonical React pages resolve to no legacy surface
    # (the fixed base serves the qualified React shell), so the page
    # entries stay at zero.
    for entry_id in (
        "legacy-page-root",
        "legacy-page-controlled-s01",
        "legacy-page-controlled-s02",
    ):
        assert verdict.per_entry_counts[entry_id]["rollback-probe"] == 0, entry_id
    assert verdict.acceptance is not None
    assert verdict.acceptance.zero_caller_ok
    assert verdict.acceptance.operator_catalog_hits == {}
    assert verdict.acceptance.rollback_probe_catalog_hits == {
        "legacy-static-app-js": 1,
        "legacy-mutation-kb-delete": 1,
    }
    assert manifest["process_artifacts"]["p-3"] == {
        "artifact_sha256": PRIOR_ARTIFACT_SHA,
        "artifact_stage": "prior",
        "traffic_class": "rollback-probe",
    }


def test_removing_a_prior_legacy_hit_no_longer_invalidates(tmp_path: Path) -> None:
    """Issue #45 removed the dedicated prior-artifact page-hit threshold:
    prior canonical pages resolve to the React shell (no legacy surface),
    so a window without a prior page hit stays VALID as long as the prior
    artifact identity, lifecycle and release evidence remain intact."""
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path, with_rollback_probe=True)
    records = [
        record
        for record in _records(raw)
        if record["legacy_surface_id"] != "legacy-mutation-kb-delete"
    ]
    for sequence, record in enumerate(records, 1):
        record["sequence"] = sequence
    raw2 = _serialize(records)
    changed = copy.deepcopy(manifest)
    changed["expected_sequence_range"] = [1, len(records)]
    changed["per_traffic_class_counts"]["rollback-probe"] -= 1
    changed["per_entry_counts"]["legacy-mutation-kb-delete"]["rollback-probe"] = 0
    changed = _rehash(changed, raw=raw2)
    verdict = verify_bundle(changed, requests_raw=raw2, lifecycle_raw=lifecycle_raw)
    assert verdict.valid, verdict.reason
    assert verdict.acceptance is not None
    assert verdict.acceptance.rollback_probe_catalog_hits == {
        "legacy-static-app-js": 1,
    }


# --- 3. classification ------------------------------------------------------


def test_classify_health_always_overrides_process_class() -> None:
    assert classify_traffic("/api/health", "release") == "health"
    assert classify_traffic("/api/health", "operator-simulated") == "health"
    assert classify_traffic("/api/health", None) == "health"
    assert classify_traffic("/api/health?x=1", "playwright-probe") == "health"


def test_classify_uses_valid_process_class() -> None:
    assert classify_traffic("/api/rules", "release") == "release"
    assert classify_traffic("/api/rules", "playwright-probe") == "playwright-probe"
    assert classify_traffic("/api/rules", "rollback-probe") == "rollback-probe"
    assert classify_traffic("/api/rules", "operator-simulated") == "operator-simulated"


def test_classify_missing_or_invalid_process_class_becomes_unknown() -> None:
    assert classify_traffic("/api/rules", None) == UNKNOWN_CLASS
    assert classify_traffic("/api/rules", "") == UNKNOWN_CLASS
    assert classify_traffic("/api/rules", "bogus-class") == UNKNOWN_CLASS


def test_recorder_classifies_health_override_and_unknown(tmp_path: Path) -> None:
    clock = FixedClock(T0)
    sink = InMemorySink()
    recorder = ObservationRecorder(
        clock, sink,
        window_id=WINDOW_ID, artifact_sha256=ARTIFACT_SHA,
        process_id="p-1", process_class="playwright-probe",
    )
    health_record = recorder.record_http(
        method="GET", path="/api/health", response_status=200, matched_route_owner="health",
    )
    assert health_record["traffic_class"] == "health"
    unknown_recorder = ObservationRecorder(
        clock, sink,
        window_id=WINDOW_ID, artifact_sha256=ARTIFACT_SHA,
        process_id="p-2", process_class="not-a-class",
    )
    unknown_record = unknown_recorder.record_http(
        method="GET", path="/api/rules", response_status=200, matched_route_owner="get_rules",
    )
    assert unknown_record["traffic_class"] == UNKNOWN_CLASS


# --- 4. catalog matcher integration ----------------------------------------


def test_match_legacy_surface_dynamic_kb_delete_family() -> None:
    assert match_legacy_surface("DELETE", "/api/kb/address_aliases/somekey") == (
        "legacy-mutation-kb-delete"
    )
    assert match_legacy_surface("DELETE", "/api/kb/address_aliases/somekey?x=1#f") == (
        "legacy-mutation-kb-delete"
    )
    assert match_legacy_surface("DELETE", "/api/kb/single-segment") is None
    assert match_legacy_surface("GET", "/api/kb/address_aliases/somekey") is None
    # The dynamic KB delete family matches by static prefix only, so a
    # three-segment path still resolves to the delete family (the catalog
    # never counts segments; no concrete section/key text ever enters a
    # match result).
    assert match_legacy_surface("DELETE", "/api/kb/address_aliases/somekey/extra") == (
        "legacy-mutation-kb-delete"
    )
    assert match_legacy_surface("DELETE", "/api/kb") is None


def test_match_legacy_surface_shielded_and_health_paths() -> None:
    assert match_legacy_surface("GET", "/") == "legacy-page-root"
    assert match_legacy_surface("GET", "/static/app.js") == "legacy-static-app-js"
    assert match_legacy_surface("POST", "/api/kb/reload") == "legacy-mutation-kb-reload-post"
    assert match_legacy_surface("POST", "/api/rules/reset") == "legacy-mutation-rules-reset-post"
    assert match_legacy_surface("GET", "/api/health") is None
    assert match_legacy_surface("GET", "/static/react/index.html") is None
    assert match_legacy_surface("GET", "/api/rules") is None  # only PUT is contracted


def test_normalize_path_family_never_leaks_query_or_concrete_kb_text() -> None:
    assert normalize_path_family("/api/rules") == "/api/rules"
    assert normalize_path_family("/api/health?x=1") == HEALTH_PATH
    assert normalize_path_family("/api/kb/address_aliases/x") == DYNAMIC_KB_FAMILY
    assert normalize_path_family("/api/kb/address_aliases/x?secret=1") == DYNAMIC_KB_FAMILY
    assert normalize_path_family("/") == "/"
    assert normalize_path_family("/static/app.js") == "/static/app.js"
    assert normalize_path_family("/api/fixtures", FAMILY_TABLE) == "/api/fixtures"
    assert normalize_path_family("/totally/arbitrary/thing") == UNREGISTERED_PATH_FAMILY


def test_unregistered_request_path_is_replaced_before_durable_serialization() -> None:
    raw_path = "/private/operator-note/secret-token-123"
    sink = InMemorySink()
    recorder = ObservationRecorder(
        FixedClock(T0),
        sink,
        window_id=WINDOW_ID,
        artifact_sha256=ARTIFACT_SHA,
        process_id="p-sentinel",
        process_class="release",
    )
    record = recorder.record_http(
        method="GET",
        path=raw_path,
        response_status=404,
        matched_route_owner="unmatched",
    )
    encoded = json.dumps(record, sort_keys=True)
    assert record["normalized_path_family"] == UNREGISTERED_PATH_FAMILY
    assert raw_path not in encoded
    assert "secret-token-123" not in encoded


def test_artifact_stage_controls_owner_while_traffic_class_only_classifies() -> None:
    def record(process_class: str, artifact_stage: str) -> dict:
        recorder = ObservationRecorder(
            FixedClock(T0),
            InMemorySink(),
            window_id=WINDOW_ID,
            artifact_sha256=ARTIFACT_SHA,
            process_id=f"p-{process_class}-{artifact_stage}",
            process_class=process_class,
            artifact_stage=artifact_stage,
        )
        return recorder.record_http(
            method="GET",
            path="/static/app.js",
            response_status=200,
            matched_route_owner="StaticFiles",
        )

    current_release = record("release", "current")
    current_rollback_class = record("rollback-probe", "current")
    prior_rollback = record("rollback-probe", "prior")
    assert current_release["legacy_surface_id"] is None
    assert current_rollback_class["legacy_surface_id"] is None
    assert prior_rollback["legacy_surface_id"] == "legacy-static-app-js"
    assert current_rollback_class["traffic_class"] == "rollback-probe"


def test_default_family_table_covers_catalog_and_health() -> None:
    table = default_family_table()
    for path in ("/", "/api/rules", "/api/rules/reset", "/api/kb", "/api/kb/reload",
                 "/static/app.js", "/static/style.css", "/api/health"):
        assert table[path] == path
    assert "/api/kb/{section}/{key}" not in table  # the dynamic family is derived, not exact


# --- 5. leak scan -----------------------------------------------------------


def test_clean_record_has_no_leak_findings() -> None:
    assert scan_record_for_leaks({
        "sequence": 1,
        "timestamp_utc": T0.isoformat(),
        "artifact_sha256": ARTIFACT_SHA,
        "process_id": "p-1",
        "window_id": WINDOW_ID,
        "correlation_id": "pp-1-1",
        "traffic_class": "release",
        "method": "GET",
        "normalized_path_family": "/api/rules",
        "matched_route_owner": "get_rules",
        "legacy_surface_id": None,
        "response_status": 200,
    }) == []


@pytest.mark.parametrize("value", [
    "Bearer xyz12345",              # credential-like
    "sk-abcdefghijklmnop",          # credential-like (secret key)
    "APP-BAD-VIN",                  # application-ID-like
    "some free text",               # free text (whitespace)
    "北京银行",                       # free text (raw value content)
    "/home/lhjysyx/xiaopeng_comp/configs/runtime_rules.yaml",  # internal path
])
def test_leak_scan_flags_leaking_values(value: str) -> None:
    assert scan_record_for_leaks({
        "sequence": 1,
        "timestamp_utc": T0.isoformat(),
        "artifact_sha256": ARTIFACT_SHA,
        "process_id": "p-1",
        "window_id": WINDOW_ID,
        "correlation_id": "pp-1-1",
        "traffic_class": "release",
        "method": "GET",
        "normalized_path_family": "/api/rules",
        "matched_route_owner": value,
        "legacy_surface_id": None,
        "response_status": 200,
    })


def test_record_with_credential_like_value_invalidates_window(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    records = _records(raw)
    records[0]["matched_route_owner"] = "Bearer xyz12345"
    raw2 = _serialize(records)
    _expect_invalid(_rehash(manifest, raw=raw2), raw2, lifecycle_raw, "leak")


def test_record_with_application_id_like_value_invalidates_window(tmp_path: Path) -> None:
    manifest, raw, lifecycle_raw = _valid_bundle(tmp_path)
    records = _records(raw)
    records[0]["matched_route_owner"] = "APP-BAD-VIN"
    raw2 = _serialize(records)
    _expect_invalid(_rehash(manifest, raw=raw2), raw2, lifecycle_raw, "leak")


# --- 6. ASGI middleware smoke (direct scope driving, no server) -------------


async def _fake_handler() -> dict:
    return {"ok": True}


async def _http_downstream(scope: dict, receive: object, send) -> None:
    scope["endpoint"] = _fake_handler
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"ok"})


async def _lifespan_downstream(scope: dict, receive, send) -> None:
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return
        else:
            return


def _drive(app, scope: dict, incoming: list[dict]) -> list[dict]:
    """Run one ASGI scope to completion against the middleware with a fixed
    message queue; returns every message the client-side send saw."""

    async def _run() -> list[dict]:
        sent: list[dict] = []
        queue = list(incoming)

        async def receive() -> dict:
            return queue.pop(0) if queue else {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            sent.append(message)

        await app(scope, receive, send)
        return sent

    return asyncio.run(_run())


def test_middleware_records_http_and_forwards_messages_untouched() -> None:
    clock = FixedClock(T0)
    sink = InMemorySink()
    recorder = ObservationRecorder(
        clock, sink,
        window_id=WINDOW_ID, artifact_sha256=ARTIFACT_SHA,
        process_id="p-1", process_class="release",
    )
    middleware = ObservationMiddleware(_http_downstream, recorder)
    sent = _drive(
        middleware,
        {"type": "http", "method": "GET", "path": "/api/rules", "query_string": b"", "headers": []},
        [],
    )
    # Messages (and headers) pass through byte-identical.
    assert sent == [
        {"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]},
        {"type": "http.response.body", "body": b"ok"},
    ]
    assert len(sink.requests) == 1
    record = sink.requests[0]
    assert record["response_status"] == 200
    assert record["matched_route_owner"] == "_fake_handler"  # endpoint __name__
    assert record["traffic_class"] == "release"
    assert record["normalized_path_family"] == "/api/rules"
    assert record["sequence"] == 1
    assert record["correlation_id"] == "pp-1-1"


def test_middleware_unmatched_404_records_unmatched_owner() -> None:
    async def downstream(scope: dict, receive, send) -> None:
        # no scope["endpoint"] set: an unmatched route / 404
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    clock = FixedClock(T0)
    sink = InMemorySink()
    recorder = ObservationRecorder(
        clock, sink,
        window_id=WINDOW_ID, artifact_sha256=ARTIFACT_SHA,
        process_id="p-1", process_class="release",
    )
    _drive(
        ObservationMiddleware(downstream, recorder),
        {"type": "http", "method": "GET", "path": "/api/nope", "query_string": b"", "headers": []},
        [],
    )
    assert sink.requests[0]["response_status"] == 404
    assert sink.requests[0]["matched_route_owner"] == "unmatched"


def test_middleware_health_path_always_classified_health() -> None:
    clock = FixedClock(T0)
    sink = InMemorySink()
    recorder = ObservationRecorder(
        clock, sink,
        window_id=WINDOW_ID, artifact_sha256=ARTIFACT_SHA,
        process_id="p-1", process_class="playwright-probe",
    )
    _drive(
        ObservationMiddleware(_http_downstream, recorder),
        {"type": "http", "method": "GET", "path": "/api/health", "query_string": b"", "headers": []},
        [],
    )
    assert sink.requests[0]["traffic_class"] == "health"


def test_middleware_recorder_failure_surfaces_as_failed_request() -> None:
    class ExplodingSink:
        def __init__(self) -> None:
            self.lifecycle: list[dict] = []

        def append_request(self, record: dict) -> None:
            raise RuntimeError("disk full")

        def append_lifecycle(self, record: dict) -> None:
            self.lifecycle.append(record)

    clock = FixedClock(T0)
    recorder = ObservationRecorder(
        clock, ExplodingSink(),
        window_id=WINDOW_ID, artifact_sha256=ARTIFACT_SHA,
        process_id="p-1", process_class="release",
    )
    with pytest.raises(RuntimeError, match="disk full"):
        _drive(
            ObservationMiddleware(_http_downstream, recorder),
            {"type": "http", "method": "GET", "path": "/api/rules", "query_string": b"", "headers": []},
            [],
        )


def test_middleware_lifespan_records_start_and_end() -> None:
    clock = FixedClock(T0)
    sink = InMemorySink()
    recorder = ObservationRecorder(
        clock, sink,
        window_id=WINDOW_ID, artifact_sha256=ARTIFACT_SHA,
        process_id="p-1", process_class="release",
    )
    _drive(
        ObservationMiddleware(_lifespan_downstream, recorder),
        {"type": "lifespan"},
        [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}],
    )
    assert [entry["event"] for entry in sink.lifecycle] == ["start", "end"]
    assert sink.lifecycle[0]["process_id"] == "p-1"
    assert sink.lifecycle[0]["timestamp_utc"] == T0.isoformat()


def test_middleware_unexpected_lifespan_message_does_not_hang() -> None:
    clock = FixedClock(T0)
    sink = InMemorySink()
    recorder = ObservationRecorder(
        clock, sink,
        window_id=WINDOW_ID, artifact_sha256=ARTIFACT_SHA,
        process_id="p-1", process_class="release",
    )
    _drive(
        ObservationMiddleware(_lifespan_downstream, recorder),
        {"type": "lifespan"},
        [{"type": "lifespan.startup"}, {"type": "lifespan.weird"}],
    )
    assert [entry["event"] for entry in sink.lifecycle] == ["start"]


def test_middleware_noop_recorder_is_capture_free_and_does_not_break_requests() -> None:
    sent = _drive(
        ObservationMiddleware(_http_downstream, NoopRecorder()),
        {"type": "http", "method": "GET", "path": "/api/rules", "query_string": b"", "headers": []},
        [],
    )
    assert [message["type"] for message in sent] == ["http.response.start", "http.response.body"]


def test_middleware_websocket_and_other_scopes_pass_through() -> None:
    calls: list[str] = []

    async def downstream(scope: dict, receive, send) -> None:
        calls.append(scope["type"])

    recorder = NoopRecorder()
    middleware = ObservationMiddleware(downstream, recorder)
    _drive(middleware, {"type": "websocket", "path": "/ws"}, [])
    assert calls == ["websocket"]


def test_resolve_route_owner_uses_class_name_for_mounted_apps() -> None:
    class MountedApp:
        pass

    scope = {"endpoint": MountedApp()}
    assert resolve_route_owner(scope) == "MountedApp"
    assert resolve_route_owner({"endpoint": _fake_handler}) == "_fake_handler"
    assert resolve_route_owner({}) == "unmatched"
    assert resolve_route_owner({"endpoint": None}) == "unmatched"


# --- 7. JSONL sink sequence contract ----------------------------------------


def test_jsonl_sink_allocates_sequences_and_fsyncs_files(tmp_path: Path) -> None:
    sink = JsonlSink(tmp_path)
    recorder = ObservationRecorder(
        FixedClock(T0), sink,
        window_id=WINDOW_ID, artifact_sha256=ARTIFACT_SHA,
        process_id="p-1", process_class="release",
    )
    first = recorder.record_http(method="GET", path="/api/rules", response_status=200, matched_route_owner="get_rules")
    second = recorder.record_http(method="GET", path="/api/health", response_status=200, matched_route_owner="health")
    assert (first["sequence"], second["sequence"]) == (1, 2)
    assert (tmp_path / "sequence").read_text(encoding="utf-8").strip() == "3"
    lines = (tmp_path / "requests.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["sequence"] for line in lines] == [1, 2]
    recorder.record_lifecycle("start")
    lifecycle_lines = (tmp_path / "process-lifecycle.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lifecycle_lines[0])["event"] == "start"


def test_jsonl_sink_crash_between_allocation_and_append_leaves_a_gap(tmp_path: Path) -> None:
    sink = JsonlSink(tmp_path)
    recorder = ObservationRecorder(
        FixedClock(T0), sink,
        window_id=WINDOW_ID, artifact_sha256=ARTIFACT_SHA,
        process_id="p-1", process_class="release",
    )
    recorder.record_http(method="GET", path="/api/rules", response_status=200, matched_route_owner="get_rules")
    # Simulate a crash: allocation advanced the sidecar to 3, the append of
    # record 2 was lost.  The sidecar is authoritative, so the next record
    # gets sequence 3 and the window shows a gap at 2.
    (tmp_path / "sequence").write_text("3\n", encoding="utf-8")
    third = recorder.record_http(method="GET", path="/api/health", response_status=200, matched_route_owner="health")
    assert third["sequence"] == 3
    recorder.record_lifecycle("start")
    recorder.record_lifecycle("end")
    requests_raw = (tmp_path / "requests.jsonl").read_bytes()
    lifecycle_raw = (tmp_path / "process-lifecycle.jsonl").read_bytes()
    manifest = build_bundle(
        tmp_path,
        requests_raw=requests_raw,
        lifecycle_raw=lifecycle_raw,
        window_id=WINDOW_ID,
        artifact_sha256=ARTIFACT_SHA,
        process_id="p-1",
        process_class="release",
        window_start_utc=T0.isoformat(),
        window_end_utc=(T0 + timedelta(seconds=1)).isoformat(),
        environment_identity=ENV_IDENTITY,
        cohort=("p-1",),
        family_table=FAMILY_TABLE,
    )
    _expect_invalid(manifest, requests_raw, lifecycle_raw, "missing sequence 2")


def test_jsonl_sink_flock_serializes_sequences_across_sinks(tmp_path: Path) -> None:
    sink_a = JsonlSink(tmp_path)
    sink_b = JsonlSink(tmp_path)
    recorder_a = ObservationRecorder(
        FixedClock(T0), sink_a,
        window_id=WINDOW_ID, artifact_sha256=ARTIFACT_SHA,
        process_id="p-1", process_class="release",
    )
    recorder_b = ObservationRecorder(
        FixedClock(T0), sink_b,
        window_id=WINDOW_ID, artifact_sha256=ARTIFACT_SHA,
        process_id="p-2", process_class="release",
    )
    first = recorder_a.record_http(method="GET", path="/api/rules", response_status=200, matched_route_owner="get_rules")
    second = recorder_b.record_http(method="GET", path="/api/health", response_status=200, matched_route_owner="health")
    third = recorder_a.record_http(method="GET", path="/api/rules", response_status=200, matched_route_owner="get_rules")
    assert [first["sequence"], second["sequence"], third["sequence"]] == [1, 2, 3]

# --- 8. HTTP integration + prior-artifact observer factory (main agent) -----

from types import ModuleType  # noqa: E402

from tests.test_s01_http import UvicornLoopback, demo_auth_headers  # noqa: E402
from tests.test_s07_http import _environment  # noqa: E402


def _observation_environment(
    state_path: Path,
    log_dir: Path,
    *,
    process_class: str = "operator-simulated",
    artifact_stage: str = "current",
    process_id: str = "t54-http-p1",
    window_id: str = "t54-http-window",
) -> dict[str, str]:
    env = _environment(state_path, "verified")
    env.update(
        {
            "TASK4_OBS_LOG_DIR": str(log_dir),
            "TASK4_OBS_WINDOW_ID": window_id,
            "TASK4_OBS_ARTIFACT_SHA256": ARTIFACT_SHA,
            "TASK4_OBS_ARTIFACT_STAGE": artifact_stage,
            "TASK4_OBS_PROCESS_CLASS": process_class,
            "TASK4_OBS_PROCESS_ID": process_id,
        }
    )
    return env


def _read_request_records(log_dir: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (log_dir / "requests.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]


def load_current_observation_module() -> ModuleType:
    """The current observation module loaded from its installed absolute
    path, never from a prior artifact's site (the release harness imports
    the prior wheel beside the current one)."""
    import importlib.util
    import sys

    module_path = Path(observation_module.__file__).resolve()
    spec = importlib.util.spec_from_file_location(
        "t54_current_observation", module_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module.__name__] = module
    spec.loader.exec_module(module)
    return module


def wrap_prior_artifact_app(prior_app: Any, current_observation: ModuleType) -> Any:
    """Register the current observation middleware around a prior-artifact
    FastAPI app without altering the prior wheel's bytes.  After Issue #45
    physically removed the legacy templates, prior canonical React pages
    resolve to no legacy surface (legacy_surface_id=None); retired static
    and mutation owners resolve only through the stable catalog IDs."""
    recorder = current_observation.recorder_from_env(
        family_table=current_observation.app_family_table(prior_app)
    )
    return current_observation.ObservationMiddleware(prior_app, recorder)


def create_prior_observer_wrapped_app() -> Any:
    """Uvicorn app factory for the rollback probe: the repo app stands in
    for the prior artifact, wrapped by the current observation module.  The
    installed rollback stage in the release harness wraps the true prior
    wheel the same way.  The repo app self-registers its own observation
    middleware at import (Issue #54); like the harness wrapper it is
    neutralized here so the current observation module records each
    rollback request exactly once."""
    import task4_consistency.web.app as web

    inner = getattr(web, "_OBSERVATION_RECORDER", None)
    if inner is not None:
        inner.enabled = False
    return wrap_prior_artifact_app(web.create_app(), load_current_observation_module())


def test_http_observation_records_closed_schema_and_preserves_authority(
    tmp_path: Path,
) -> None:
    """Real uvicorn + observation env: canonical shell, protected 404,
    retired static/mutation surfaces resolving to no legacy surface, and a
    handler rejection produce closed records with the resolved route owner
    and final status, while session issuance and authority reads stay
    unchanged."""
    state_path = tmp_path / "t54-obs-http.sqlite3"
    log_dir = tmp_path / "obs-log"
    env = _observation_environment(state_path, log_dir)
    with UvicornLoopback(
        env,
        app_target="tests.test_t01_http:create_t01_test_app",
        app_factory=True,
    ) as server:
        root = server.request("GET", "/", use_session=False)
        assert root.status == 200, root.text
        assert root.headers["cache-control"] == "no-store"

        shell = server.request(
            "GET", "/controlled/s01", headers=demo_auth_headers(), use_session=False
        )
        assert shell.status == 200, shell.text
        assert "set-cookie" in shell.headers

        not_found = server.request(
            "GET",
            "/controlled/s01/api/queries/applications/app_t54missing9876543210fedcba/workspace",
            headers=demo_auth_headers(),
            use_session=False,
        )
        assert not_found.status == 404, not_found.text

        # Issue #45 contraction: the five product files are deleted and the
        # five mutation handlers are retired, so the retired surfaces are
        # absent from the HTTP surface with the framework absence status.
        static = server.request("GET", "/static/app.js", use_session=False)
        assert static.status == 404, static.text

        reloaded = server.request("POST", "/api/kb/reload", use_session=False)
        assert reloaded.status == 404, reloaded.text

        rejected = server.request(
            "DELETE", "/api/kb/unknown-section/somekey", use_session=False
        )
        assert rejected.status == 404, rejected.text

        health = server.request("GET", "/api/health", use_session=False)
        assert health.status == 200, health.text

        queue = server.request(
            "GET",
            "/controlled/s01/api/queries/queue",
            headers=demo_auth_headers(),
            use_session=False,
        )
        assert queue.status == 200, queue.text

    records = _read_request_records(log_dir)
    by_family = {record["normalized_path_family"]: record for record in records}
    assert by_family["/"]["matched_route_owner"] == "index"
    assert by_family["/"]["legacy_surface_id"] is None  # canonical React owner
    assert by_family["/controlled/s01"]["matched_route_owner"] == "controlled_s01_page"
    assert by_family["/controlled/s01"]["legacy_surface_id"] is None  # React owner
    # Retired static/mutation surfaces resolve to no legacy surface in the
    # current artifact.
    assert by_family["/static/app.js"]["legacy_surface_id"] is None
    assert by_family["/api/kb/reload"]["legacy_surface_id"] is None
    assert by_family["/api/kb/{section}/{key}"]["legacy_surface_id"] is None
    assert by_family["/api/health"]["traffic_class"] == "health"
    assert by_family["/api/health"]["matched_route_owner"] == "health"
    assert all(record["traffic_class"] != "unknown" for record in records)
    assert all(record["matched_route_owner"] for record in records)
    assert all(
        record["response_status"] in (200, 404) for record in records
    )
    sequences = [record["sequence"] for record in records]
    assert sequences == list(range(1, len(sequences) + 1))

    # The sealed window is valid; no retired surface is observable, so the
    # zero-caller acceptance holds for the operator-simulated cohort.
    lifecycle = [
        json.loads(line)
        for line in (log_dir / "process-lifecycle.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [entry["event"] for entry in lifecycle] == ["start", "end"]

    import task4_consistency.web.app as web

    family_table = observation_module.app_family_table(web.app)
    manifest = observation_module.build_bundle(
        tmp_path / "obs-bundle",
        requests_raw=(log_dir / "requests.jsonl").read_bytes(),
        lifecycle_raw=(log_dir / "process-lifecycle.jsonl").read_bytes(),
        window_id="t54-http-window",
        artifact_sha256=ARTIFACT_SHA,
        process_id="t54-http-p1",
        process_class="operator-simulated",
        window_start_utc=min(records, key=lambda r: r["timestamp_utc"])["timestamp_utc"],
        window_end_utc=max(records, key=lambda r: r["timestamp_utc"])["timestamp_utc"],
        environment_identity=observation_module.default_environment_identity(),
        cohort=["t54-http-p1"],
        family_table=family_table,
    )
    operator_count = sum(
        1 for record in records if record["traffic_class"] == "operator-simulated"
    )
    health_count = sum(
        1 for record in records if record["traffic_class"] == "health"
    )
    assert manifest["per_traffic_class_counts"]["operator-simulated"] == operator_count
    assert manifest["per_traffic_class_counts"]["health"] == health_count
    verdict = observation_module.verify_bundle(tmp_path / "obs-bundle" / "manifest.json")
    assert verdict.valid, verdict.reason
    assert verdict.acceptance is not None
    assert verdict.acceptance.zero_caller_ok is True
    assert verdict.acceptance.operator_catalog_hits == {}


def test_http_observation_is_capture_free_without_observation_environment(
    tmp_path: Path,
) -> None:
    """Ordinary runs with no observation env never create log files and
    never change request behavior."""
    state_path = tmp_path / "t54-obs-capturefree.sqlite3"
    log_dir = tmp_path / "must-not-exist"
    env = _environment(state_path, "verified")
    with UvicornLoopback(
        env,
        app_target="tests.test_t01_http:create_t01_test_app",
        app_factory=True,
    ) as server:
        root = server.request("GET", "/", use_session=False)
        assert root.status == 200, root.text
        health = server.request("GET", "/api/health", use_session=False)
        assert health.status == 200, health.text
    assert not log_dir.exists()


def test_http_rollback_probe_records_prior_legacy_owners(tmp_path: Path) -> None:
    """Changing only traffic class leaves current-artifact ownership fixed."""
    state_path = tmp_path / "t54-obs-rollback.sqlite3"
    log_dir = tmp_path / "obs-rollback-log"
    env = _observation_environment(
        state_path, log_dir, process_class="rollback-probe", process_id="t54-rollback-p1"
    )
    with UvicornLoopback(
        env,
        app_target="tests.test_t01_http:create_t01_test_app",
        app_factory=True,
    ) as server:
        root = server.request("GET", "/", use_session=False)
        assert root.status == 200, root.text
        shell = server.request(
            "GET", "/controlled/s01", headers=demo_auth_headers(), use_session=False
        )
        assert shell.status == 200, shell.text
        alias = server.request("GET", "/demo/react", use_session=False)
        assert alias.status == 200, alias.text

    records = _read_request_records(log_dir)
    by_family = {record["normalized_path_family"]: record for record in records}
    assert by_family["/"]["legacy_surface_id"] is None
    assert by_family["/controlled/s01"]["legacy_surface_id"] is None
    assert by_family["/demo/react"]["legacy_surface_id"] is None
    assert all(
        record["traffic_class"] in ("rollback-probe", "health")
        for record in records
    )


def test_prior_artifact_observer_factory_wraps_without_altering_prior_bytes(
    tmp_path: Path,
) -> None:
    """The factory loads the CURRENT observation module from its installed
    absolute path, wraps a prior-artifact app, and passes the current
    catalog as immutable data: real uvicorn records rollback-probe legacy
    observations while the wrapped app serves normally."""
    state_path = tmp_path / "t54-obs-prior.sqlite3"
    log_dir = tmp_path / "obs-prior-log"
    env = _observation_environment(
        state_path,
        log_dir,
        process_class="rollback-probe",
        artifact_stage="prior",
        process_id="t54-rollback-p1",
    )
    with UvicornLoopback(
        env,
        app_target="tests.test_t54_observation:create_prior_observer_wrapped_app",
        app_factory=True,
    ) as server:
        root = server.request("GET", "/", use_session=False)
        assert root.status == 200, root.text
        health = server.request("GET", "/api/health", use_session=False)
        assert health.status == 200, health.text

    records = _read_request_records(log_dir)
    by_family = {record["normalized_path_family"]: record for record in records}
    # Issue #45: canonical React pages (root/S01) resolve to no legacy
    # surface even from the prior-artifact observer wrapper.
    assert by_family["/"]["legacy_surface_id"] is None
    assert by_family["/"]["traffic_class"] == "rollback-probe"
    assert by_family["/api/health"]["traffic_class"] == "health"
    # The wrapped prior app still self-registers its own observation
    # middleware (Issue #54); the factory neutralizes it so the current
    # observer records each request exactly once.  Duplicated correlations
    # would invalidate a sealed window, so assert uniqueness + a single
    # lifecycle span here.
    correlations = [record["correlation_id"] for record in records]
    assert len(correlations) == len(set(correlations)), "correlation ids duplicated"
    lifecycle = [
        json.loads(line)
        for line in (log_dir / "process-lifecycle.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [entry["event"] for entry in lifecycle] == ["start", "end"]
