from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import threading
import zlib

import pytest

from task4_consistency.controlled.s01 import (
    AdmissionDisposition,
    ControlledScenarioService,
    QueryNotFound,
    S01CommandPrincipal,
)
from task4_consistency.controlled.s02 import ControlledObject, RegisteredSource
from task4_consistency.rules.engine import RuleEngine
from task4_consistency.rules.loader import load_rules


ROOT = Path(__file__).resolve().parents[1]
REVIEWER = S01CommandPrincipal(
    subject="s06-reviewer",
    role="reviewer",
    scope="C-DEMO",
    source_id="s06-review-console",
)
INTAKE = S01CommandPrincipal(
    subject=REVIEWER.subject,
    role="integrator",
    scope="C-DEMO",
    source_id="s06-demo-intake",
)
SUPPLEMENT_INTEGRATOR = S01CommandPrincipal(
    subject="s06-integrator",
    role="integrator",
    scope="R-OBSERVED/c-demo",
    source_id="s06-material-source",
)


def _png() -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    scanline = b"\x00\x00\x00\x00"
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(scanline))
        + chunk(b"IEND", b"")
    )


def _descriptor(ref: str, media_type: str, content: bytes) -> dict[str, object]:
    return {
        "controlled_object_ref": ref,
        "media_type": media_type,
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _supplement_service(
    tmp_path: Path,
    source: dict[str, object],
    *,
    fault_injector: Callable[[str], None] | None = None,
    checker_runner: Callable[[object], object] | None = None,
    worker_identity: str = "s06-worker",
    clock: Callable[[], int] | None = None,
) -> ControlledScenarioService:
    return ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "target.sqlite3",
        scenario_id="app_missing_vin_docs.json",
        fault_injector=fault_injector,
        checker_runner=checker_runner,
        worker_identity=worker_identity,
        clock=clock,
        registered_sources=(
            RegisteredSource(
                tenant_id="c-demo",
                source_system_id="s06-material-source",
                workload_identity_id="s06-material-workload",
                adapter_id="s06-detection-adapter",
                adapter_version="1",
                source_shape="ocr-detection/unversioned",
                producer_family="s06-ocr",
            ),
        ),
        controlled_objects=(
            ControlledObject(
                tenant_id="c-demo",
                source_system_id="s06-material-source",
                object_ref="s06-result-object",
                media_type="application/json",
                content=source["result"],  # type: ignore[arg-type]
            ),
            ControlledObject(
                tenant_id="c-demo",
                source_system_id="s06-material-source",
                object_ref="s06-page-object",
                media_type="image/png",
                content=source["page"],  # type: ignore[arg-type]
            ),
        ),
    )


def _supplement_source() -> dict[str, object]:
    page = _png()
    producer_result = {
        "per_image_results": [
            {
                "image_path": "lease-page.png",
                "image_size": {"width": 1, "height": 1},
                "detections": [
                    {
                        "bbox": [0, 0, 1, 1],
                        "class_id": 1,
                        "class_name": "vehicle_identifier",
                        "confidence": 0.99,
                        "field_key": "vin",
                        "ocr_text": "LSVAA4182N2444555",
                        "value": "LSVAA4182N2444555",
                    }
                ],
            }
        ]
    }
    result = json.dumps(
        producer_result,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {"result": result, "page": page}


def _claimed_manual_review(
    tmp_path: Path,
    *,
    fault_injector: Callable[[str], None] | None = None,
) -> tuple[
    ControlledScenarioService,
    str,
    str,
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    source = _supplement_source()
    service = _supplement_service(
        tmp_path,
        source,
        fault_injector=fault_injector,
    )
    admitted = service.submit_demo(
        scenario_id="app_missing_vin_docs.json",
        idempotency_key="s06-missing-vin-intake",
        principal=INTAKE,
    )
    assert admitted.disposition is AdmissionDisposition.ACCEPTED
    assert admitted.application_id is not None
    assert service.process_next_job().status == "complete"
    service.refresh_projection()
    queue = service.queue_view(
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=100,
    )
    work_item_id = queue["items"][0]["work_item_id"]
    review = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=100,
    )
    finding = next(
        item for item in review["automatic_findings"] if item["rule_id"] == "R_VIN_CROSS"
    )
    claim = service.claim_review_work_item(
        principal=REVIEWER,
        work_item_id=work_item_id,
        expected_context=review["command_context"],
        now=100,
    )
    return (
        service,
        admitted.application_id,
        work_item_id,
        review,
        claim,
        finding,
        source,
    )


def _ready_supplement_request(
    tmp_path: Path,
    *,
    fault_injector: Callable[[str], None] | None = None,
    before_request: Callable[
        [ControlledScenarioService, str, dict[str, object], dict[str, object], dict[str, object]],
        None,
    ] | None = None,
) -> tuple[
    ControlledScenarioService,
    str,
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    service, application_id, work_item_id, review, claim, finding, source = (
        _claimed_manual_review(tmp_path, fault_injector=fault_injector)
    )
    if before_request is not None:
        before_request(service, work_item_id, review, claim, finding)
    created = service.request_supplement(
        principal=REVIEWER,
        work_item_id=work_item_id,
        finding_id=finding["finding_id"],
        reason_code="MISSING_REQUIRED_MATERIAL",
        expected_fence=claim["claim_fence"],
        expected_context=review["command_context"],
        idempotency_key="s06-request-1",
        now=101,
    )
    request = service.supplement_request_view(
        principal=REVIEWER,
        request_id=created["request_id"],
        now=102,
    )
    return service, application_id, review, request, source


def _attachment_submission(
    request: dict[str, object],
    source: dict[str, object],
    *,
    closed: bool,
) -> dict[str, object]:
    item_sequence = 2 if closed else 1
    batch = {
        "batch_id": "s06-batch-1",
        "item_sequence": item_sequence,
        "item_count": 2,
        "final_sequence": 2,
        "scope_mode": "full",
        "closed": closed,
    }
    manifest = {
        "batch_id": batch["batch_id"],
        "final_sequence": batch["final_sequence"],
        "item_count": batch["item_count"],
        "scope_mode": batch["scope_mode"],
        "stream_id": "s06-supplement-stream",
        "supplement_request_id": request["request_id"],
    }
    batch["manifest_digest"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "envelope_id": f"s06-attachment-envelope-{item_sequence}",
        "schema_version": "1.0.0",
        "semantic_version": "1.0.0",
        "command_type": "submit_attachment_version",
        "upstream_application_ref": "APP-MISS-VINDOC",
        "stream_id": "s06-supplement-stream",
        "source_revision": item_sequence,
        "predecessor_revision": 1 if closed else None,
        "must_understand": [],
        "workload_identity_id": "s06-material-workload",
        "request_binding": {
            "supplement_request_id": request["request_id"],
            "request_context_digest": request["context_digest"],
            "material_requirement_id": "c-demo-financing-lease-vin/1",
            "request_progress_revision": item_sequence,
        },
        "document_binding": {
            "source_document_ref": "s06-lease-replacement",
            "document_type": "financing_lease_contract",
            "document_role": "financing_lease_contract",
        },
        "attachment_lineage": {
            "operation": "replacement",
            "predecessor_attachment_id": request[
                "expected_predecessor_attachment_id"
            ],
            "predecessor_attachment_version": request[
                "expected_predecessor_attachment_version"
            ],
            "attachment_version": 2,
        },
        "batch": batch,
        "result_object": _descriptor(
            "s06-result-object", "application/json", source["result"]
        ),
        "attachments": [
            {
                "source_attachment_ref": "s06-source-attachment-2",
                "page_ref": "s06-source-page-2",
                "page_ordinal": 1,
                "source_name_sha256": hashlib.sha256(
                    b"lease-page.png"
                ).hexdigest(),
                "object": _descriptor(
                    "s06-page-object", "image/png", source["page"]
                ),
            }
        ],
        "producer": {
            "producer_id": "s06-producer",
            "producer_family": "s06-ocr",
            "task_id": "s06-lease-field-extraction",
            "task_version": "1",
            "run_id": "s06-producer-run-1",
            "model_id": "s06-model",
            "model_version": "1",
            "coordinate_system": {
                "name": "pixel",
                "unit": "pixel",
                "origin": "top_left",
            },
            "confidence_semantics": {
                "minimum": 0.0,
                "maximum": 1.0,
                "higher_is": "stronger_detection",
                "meaning": "producer_detection_score",
                "granularity": "observation",
                "calibration": "unknown",
            },
        },
    }


def _request_supplement(
    service: ControlledScenarioService,
    work_item_id: str,
    review: dict[str, object],
    claim: dict[str, object],
    finding: dict[str, object],
    *,
    idempotency_key: str = "s06-request-1",
    now: int = 101,
) -> dict[str, object]:
    return service.request_supplement(
        principal=REVIEWER,
        work_item_id=work_item_id,
        finding_id=finding["finding_id"],  # type: ignore[arg-type]
        reason_code="MISSING_REQUIRED_MATERIAL",
        expected_fence=claim["claim_fence"],  # type: ignore[arg-type]
        expected_context=review["command_context"],  # type: ignore[arg-type]
        idempotency_key=idempotency_key,
        now=now,
    )


def _source_backed_correction(
    service: ControlledScenarioService,
    application_id: str,
    finding_id: str,
) -> dict[str, object]:
    workspace = service.workspace_view(
        application_id,
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=100,
    )
    finding = next(
        item
        for item in workspace["mandatory_blockers"]
        if item["finding_id"] == finding_id
    )
    source = next(
        link for link in finding["evidence_links"] if link["evidence_eligible"]
    )
    return {
        "schema_version": "field-observation-correction/1",
        "finding_id": finding_id,
        "observation_id": source["observation_id"],
        "document_id": source["document_id"],
        "document_role": source["document_role"],
        "field": source["field"],
        "raw": "LSVAA4182N2444555",
        "source_location": {
            key: source[key]
            for key in ("source_sha256", "source_page", "source_region")
        },
        "reason_code": "SOURCE_VALUE_MISREAD",
    }


def _generic_observation_submission(
    request: dict[str, object],
    source: dict[str, object],
) -> dict[str, object]:
    submission = _attachment_submission(request, source, closed=False)
    submission.update(
        {
            "envelope_id": "s06-generic-envelope",
            "command_type": "submit_observation_result",
            "upstream_application_ref": "APP-UNRELATED-S06",
            "stream_id": "s06-generic-stream",
        }
    )
    for key in ("request_binding", "attachment_lineage", "batch"):
        submission.pop(key)
    return submission


def test_reviewer_creates_server_pinned_supplement_request_and_fences_review(
    tmp_path: Path,
) -> None:
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "target.sqlite3",
        scenario_id="app_missing_vin_docs.json",
    )
    admitted = service.submit_demo(
        scenario_id="app_missing_vin_docs.json",
        idempotency_key="s06-missing-vin-intake",
        principal=INTAKE,
    )
    assert admitted.disposition is AdmissionDisposition.ACCEPTED
    assert admitted.application_id is not None
    assert service.process_next_job().status == "complete"
    service.refresh_projection()

    queue = service.queue_view(
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=100,
    )
    assert len(queue["items"]) == 1
    work_item_id = queue["items"][0]["work_item_id"]
    review = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=100,
    )
    finding = next(
        item for item in review["automatic_findings"] if item["rule_id"] == "R_VIN_CROSS"
    )
    assert (finding["verdict"], finding["reason_code"]) == (
        "uncertain",
        "MISSING_DOCS",
    )
    claim = service.claim_review_work_item(
        principal=REVIEWER,
        work_item_id=work_item_id,
        expected_context=review["command_context"],
        now=100,
    )

    with pytest.raises(ValueError, match="supplement request is invalid"):
        service.request_supplement(
            principal=REVIEWER,
            work_item_id=work_item_id,
            finding_id=finding["finding_id"],
            reason_code="MISSING_REQUIRED_MATERIAL",
            expected_fence=claim["claim_fence"],
            expected_context=review["command_context"],
            idempotency_key="s06-caller-selected-predecessor",
            predecessor_request_id="caller-selected-request",
            now=101,
        )

    created = service.request_supplement(
        principal=REVIEWER,
        work_item_id=work_item_id,
        finding_id=finding["finding_id"],
        reason_code="MISSING_REQUIRED_MATERIAL",
        expected_fence=claim["claim_fence"],
        expected_context=review["command_context"],
        idempotency_key="s06-request-1",
        now=101,
    )

    assert created == {
        "status": "accepted",
        "replayed": False,
        "application_id": admitted.application_id,
        "request_id": created["request_id"],
        "work_item_id": created["work_item_id"],
        "finding_id": finding["finding_id"],
        "material_requirement_id": "c-demo-financing-lease-vin/1",
        "phase": "Supplement",
        "route": "supplement_pending",
        "due_at": 3701,
        "lifecycle_revision": review["lifecycle_revision"] + 1,
        "evidence_revision": review["evidence_revision"],
    }
    request = service.supplement_request_view(
        principal=REVIEWER,
        request_id=created["request_id"],
        now=102,
    )
    assert request["schema_version"] == "supplement-request/1"
    assert request["status"] == "open"
    assert request["current"] is True
    assert request["source_work_item_id"] == work_item_id
    assert request["requester_claim_fence"] == claim["claim_fence"]
    assert request["cycle"] == 1
    assert request["run_id"] == review["run_authority"]["run_id"]
    assert request["finding_id"] == finding["finding_id"]
    assert request["finding_reason_code"] == "MISSING_DOCS"
    assert request["finding_verdict"] == "uncertain"
    assert request["material_requirement"] == {
        "material_requirement_id": "c-demo-financing-lease-vin/1",
        "document_role": "financing_lease_contract",
        "material_kind": "financing_lease_contract",
        "operation": "replacement",
        "required_fact_kinds": ["attachment", "page", "producer", "vin_observation"],
        "responsible_party": "application_material_provider",
        "allowed_tenant_id": "c-demo",
        "allowed_source_system_ids": ["s06-material-source"],
        "allowed_workload_identity_ids": ["s06-material-workload"],
        "satisfaction_policy_id": "c-demo-supplement/1",
        "batch_item_count": 2,
        "batch_closure_required": True,
        "integrity_required": True,
        "provenance_required": True,
        "evidence_eligibility_required": True,
    }
    assert request["requested_at"] == 101
    assert request["due_at"] == 3701
    assert request["fixed_context"] == review["command_context"]

    fenced = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=102,
    )
    assert fenced["status"] == "invalidated"
    assert fenced["phase"] == "Supplement"
    assert fenced["route"] == "supplement_pending"


def test_open_registered_batch_appends_request_progress_without_fulfillment(
    tmp_path: Path,
) -> None:
    service, application_id, review, request, source = _ready_supplement_request(
        tmp_path
    )
    submission = _attachment_submission(request, source, closed=False)

    receipt = service.submit_attachment_version(
        submission=submission,
        idempotency_key="s06-progress-1",
        principal=SUPPLEMENT_INTEGRATOR,
        now=200,
    )

    assert receipt.disposition is AdmissionDisposition.ACCEPTED
    assert receipt.reason_code == "request_progress_accepted"
    assert receipt.replayed is False
    assert receipt.application_id == application_id
    assert receipt.request_id == request["request_id"]
    assert receipt.receipt_id is not None
    assert receipt.envelope_id == submission["envelope_id"]
    assert receipt.stream_id == submission["stream_id"]
    assert receipt.source_revision == 1
    assert receipt.batch_id == submission["batch"]["batch_id"]
    assert receipt.batch_closed is False
    assert receipt.request_progress_revision == 1
    assert receipt.attachment_id is not None
    assert receipt.attachment_version == 2
    assert receipt.supersedes_attachment_id == request[
        "expected_predecessor_attachment_id"
    ]
    assert receipt.request_status == "open"
    assert receipt.fulfilled is False
    assert receipt.phase == "Awaiting Evidence"
    assert receipt.route == "awaiting_evidence"
    assert receipt.lifecycle_revision == review["lifecycle_revision"] + 3
    assert receipt.evidence_revision == review["evidence_revision"] + 1
    assert receipt.job_id is None
    assert receipt.attachment_id != request[
        "expected_predecessor_attachment_id"
    ]
    current = service.supplement_request_view(
        principal=REVIEWER,
        request_id=request["request_id"],
        now=201,
    )
    assert current["status"] == "open"
    assert current["current"] is True
    assert current["phase"] == "Awaiting Evidence"
    assert current["route"] == "awaiting_evidence"
    assert current["evidence_revision"] == review["evidence_revision"] + 1

    history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    assert [item["version"] for item in history["attachment_versions"]] == [1, 2]
    assert history["attachment_versions"][0]["current"] is False
    assert history["attachment_versions"][1]["current"] is True
    assert history["attachment_versions"][1]["supersedes_attachment_id"] == request[
        "expected_predecessor_attachment_id"
    ]
    assert history["runs"][0]["current"] is False
    assert service.process_next_job().status == "idle"


def test_request_rejects_unpinned_batch_cardinality_without_business_effect(
    tmp_path: Path,
) -> None:
    service, application_id, _, request, source = _ready_supplement_request(
        tmp_path
    )
    submission = _attachment_submission(request, source, closed=False)
    submission["batch"]["item_count"] = 3
    submission["batch"]["final_sequence"] = 3
    manifest = {
        "batch_id": submission["batch"]["batch_id"],
        "final_sequence": 3,
        "item_count": 3,
        "scope_mode": submission["batch"]["scope_mode"],
        "stream_id": submission["stream_id"],
        "supplement_request_id": request["request_id"],
    }
    submission["batch"]["manifest_digest"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    before_request = service.supplement_request_view(
        principal=REVIEWER,
        request_id=request["request_id"],
        now=150,
    )
    before_history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )

    result = service.submit_attachment_version(
        submission=submission,
        idempotency_key="s06-unpinned-batch-cardinality",
        principal=SUPPLEMENT_INTEGRATOR,
        now=200,
    )

    assert result.disposition is AdmissionDisposition.REJECTED
    assert result.reason_code == "intake.request_context_mismatch"
    assert service.supplement_request_view(
        principal=REVIEWER,
        request_id=request["request_id"],
        now=150,
    ) == before_request
    assert service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    ) == before_history


def test_final_batch_closure_atomically_fulfills_and_enqueues_assembly(
    tmp_path: Path,
) -> None:
    service, application_id, review, request, source = _ready_supplement_request(
        tmp_path
    )
    progress = service.submit_attachment_version(
        submission=_attachment_submission(request, source, closed=False),
        idempotency_key="s06-progress-1",
        principal=SUPPLEMENT_INTEGRATOR,
        now=200,
    )
    before = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    closure = _attachment_submission(request, source, closed=True)

    fulfilled = service.submit_attachment_version(
        submission=closure,
        idempotency_key="s06-closure-2",
        principal=SUPPLEMENT_INTEGRATOR,
        now=201,
    )

    assert fulfilled.disposition is AdmissionDisposition.ACCEPTED
    assert fulfilled.reason_code == "request_fulfilled"
    assert fulfilled.request_id == request["request_id"]
    assert fulfilled.request_status == "fulfilled"
    assert fulfilled.fulfilled is True
    assert fulfilled.batch_closed is True
    assert fulfilled.request_progress_revision == 2
    assert fulfilled.attachment_id == progress.attachment_id
    assert fulfilled.attachment_version == 2
    assert fulfilled.phase == "Assembly"
    assert fulfilled.route == "pending_check"
    assert fulfilled.lifecycle_revision == review["lifecycle_revision"] + 4
    assert fulfilled.evidence_revision == review["evidence_revision"] + 1
    assert fulfilled.job_id is not None
    final_request = service.supplement_request_view(
        principal=REVIEWER,
        request_id=request["request_id"],
        now=202,
    )
    assert final_request["status"] == "fulfilled"
    assert final_request["current"] is False
    assert final_request["phase"] == "Assembly"
    assert final_request["route"] == "pending_check"
    after = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    assert after["attachment_versions"] == before["attachment_versions"]
    assert after["current_run_id"] is None


def test_fulfilled_request_runs_asynchronously_to_a_new_current_route(
    tmp_path: Path,
) -> None:
    service, application_id, _, request, source = _ready_supplement_request(tmp_path)
    old_history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    old_run = old_history["runs"][0]
    service.submit_attachment_version(
        submission=_attachment_submission(request, source, closed=False),
        idempotency_key="s06-progress-1",
        principal=SUPPLEMENT_INTEGRATOR,
        now=200,
    )
    fulfilled = service.submit_attachment_version(
        submission=_attachment_submission(request, source, closed=True),
        idempotency_key="s06-closure-2",
        principal=SUPPLEMENT_INTEGRATOR,
        now=201,
    )

    completed = service.process_next_job()

    assert completed.status == "complete"
    assert completed.job_id == fulfilled.job_id
    assert completed.run_id is not None
    assert completed.run_id != old_run["run_id"]
    assert completed.evidence_revision == fulfilled.evidence_revision
    assert completed.evidence_snapshot_id != old_run["evidence_snapshot_id"]
    assert completed.semantic_differential is not None
    assert completed.semantic_differential["status"] == "mismatch"
    assert {
        item["rule_id"] for item in completed.semantic_differential["mismatches"]
    } == {
        "R_VIN_CROSS",
        "R_ID_EXACT",
        "R_AMOUNT_TOL",
        "R_DATE_CROSS",
        "R_REG_CERT_CROSS",
    }
    assert completed.lifecycle_phases[-4:] == (
        "Evidence Ready",
        "Checking",
        "Routing Determination",
        "Manual Review",
    )
    service.refresh_projection()
    current = service.current_route_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    assert current["current_run_id"] == completed.run_id
    assert current["phase"] == "Manual Review"
    assert current["route"] == "manual_review"
    history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    assert [run["current"] for run in history["runs"]] == [False, True]
    assert history["runs"][0]["authority_digest"] == old_run["authority_digest"]
    queue = service.queue_view(
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=300,
    )
    assert len(queue["items"]) == 1
    assert queue["items"][0]["work_item_id"] != request["source_work_item_id"]
    successor_review = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=queue["items"][0]["work_item_id"],
        now=300,
    )
    assert "R_VIN_CROSS" not in {
        finding["rule_id"] for finding in successor_review["automatic_findings"]
    }


def test_deadline_equality_expires_fail_closed_and_preserves_progress(
    tmp_path: Path,
) -> None:
    service, application_id, _, request, source = _ready_supplement_request(tmp_path)
    progress = service.submit_attachment_version(
        submission=_attachment_submission(request, source, closed=False),
        idempotency_key="s06-progress-1",
        principal=SUPPLEMENT_INTEGRATOR,
        now=200,
    )
    before = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    closure = _attachment_submission(request, source, closed=True)

    expired = service.submit_attachment_version(
        submission=closure,
        idempotency_key="s06-deadline-closure",
        principal=SUPPLEMENT_INTEGRATOR,
        now=request["due_at"],
    )

    assert expired.disposition is AdmissionDisposition.REJECTED
    assert expired.reason_code == "supplement.deadline_reached"
    assert expired.application_id == application_id
    assert expired.request_id == request["request_id"]
    assert expired.request_status == "expired"
    assert expired.phase == "Unprocessable"
    assert expired.route == "unprocessable"
    assert expired.responsible_party == "application_material_provider"
    assert expired.recovery_action == "create_a_new_current_supplement_request"
    assert expired.recovery_target == {
        "kind": "supplement_request",
        "request_id": request["request_id"],
        "cycle": 1,
        "due_at": request["due_at"],
    }
    assert expired.job_id is None
    final_request = service.supplement_request_view(
        principal=REVIEWER,
        request_id=request["request_id"],
        now=request["due_at"],
    )
    assert final_request["status"] == "expired"
    assert final_request["failure"] == {
        "reason_code": "supplement.deadline_reached",
        "responsible_party": "application_material_provider",
        "recovery_action": "create_a_new_current_supplement_request",
        "recovery_target": expired.recovery_target,
    }
    route = service.current_route_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    assert route["phase"] == "Unprocessable"
    assert route["route"] == "unprocessable"
    assert route["failure"] == final_request["failure"]
    after = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    assert after["attachment_versions"] == before["attachment_versions"]
    assert after["attachment_versions"][-1]["attachment_id"] == progress.attachment_id
    assert service.process_next_job().status == "idle"

    replay = service.submit_attachment_version(
        submission=closure,
        idempotency_key="s06-deadline-closure",
        principal=SUPPLEMENT_INTEGRATOR,
        now=request["due_at"] + 10,
    )
    assert replay.receipt_id == expired.receipt_id
    assert replay.replayed is True


def test_deadline_sweep_expires_without_a_late_submission(tmp_path: Path) -> None:
    service, application_id, _, request, _ = _ready_supplement_request(tmp_path)
    operator = S01CommandPrincipal(
        subject="s06-deadline-runtime",
        role="operator",
        scope="C-DEMO",
        source_id="s06-target-runtime",
    )
    before = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )

    not_due = service.expire_due_supplement_requests(
        principal=operator,
        now=request["due_at"] - 1,  # type: ignore[operator]
    )
    expired = service.expire_due_supplement_requests(
        principal=operator,
        now=request["due_at"],  # type: ignore[arg-type]
    )
    replay = service.expire_due_supplement_requests(
        principal=operator,
        now=request["due_at"] + 1,  # type: ignore[operator]
    )

    assert not_due == {
        "status": "accepted",
        "expired_request_ids": [],
        "expired_count": 0,
    }
    assert expired == {
        "status": "accepted",
        "expired_request_ids": [request["request_id"]],
        "expired_count": 1,
    }
    assert replay == not_due
    final_request = service.supplement_request_view(
        principal=REVIEWER,
        request_id=request["request_id"],  # type: ignore[arg-type]
        now=request["due_at"],  # type: ignore[arg-type]
    )
    assert final_request["status"] == "expired"
    assert final_request["phase"] == "Unprocessable"
    assert final_request["route"] == "unprocessable"
    assert final_request["failure"] == {
        "reason_code": "supplement.deadline_reached",
        "responsible_party": "application_material_provider",
        "recovery_action": "create_a_new_current_supplement_request",
        "recovery_target": {
            "kind": "supplement_request",
            "request_id": request["request_id"],
            "cycle": 1,
            "due_at": request["due_at"],
        },
    }
    after = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    assert after["attachment_versions"] == before["attachment_versions"]
    assert service.process_next_job().status == "idle"


def test_unrecoverable_source_dependency_invalidates_request_fail_closed(
    tmp_path: Path,
) -> None:
    active_faults: set[str] = set()

    def fail(write_point: str) -> None:
        if write_point in active_faults:
            raise OSError("injected source authority failure")

    service, application_id, _, request, source = _ready_supplement_request(
        tmp_path,
        fault_injector=fail,
    )
    progress = service.submit_attachment_version(
        submission=_attachment_submission(request, source, closed=False),
        idempotency_key="s06-progress-1",
        principal=SUPPLEMENT_INTEGRATOR,
        now=200,
    )
    before = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    active_faults.add("review.source_read")

    blocked = service.submit_attachment_version(
        submission=_attachment_submission(request, source, closed=True),
        idempotency_key="s06-unrecoverable-source",
        principal=SUPPLEMENT_INTEGRATOR,
        now=201,
    )

    assert blocked.disposition is AdmissionDisposition.REJECTED
    assert blocked.reason_code == "supplement.source_evidence_unavailable"
    assert blocked.request_status == "invalidated"
    assert blocked.phase == "Unprocessable"
    assert blocked.route == "unprocessable"
    assert blocked.responsible_party == "platform_owner"
    assert blocked.recovery_action == "restore_source_evidence_and_create_a_new_request"
    assert blocked.recovery_target == {
        "kind": "source_evidence",
        "application_id": application_id,
        "request_id": request["request_id"],
        "cycle": 1,
    }
    assert blocked.job_id is None
    final_request = service.supplement_request_view(
        principal=REVIEWER,
        request_id=request["request_id"],
        now=202,
    )
    assert final_request["status"] == "invalidated"
    assert final_request["failure"] == {
        "reason_code": blocked.reason_code,
        "responsible_party": blocked.responsible_party,
        "recovery_action": blocked.recovery_action,
        "recovery_target": blocked.recovery_target,
    }
    after = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    assert after["attachment_versions"] == before["attachment_versions"]
    assert after["attachment_versions"][-1]["attachment_id"] == progress.attachment_id
    assert service.process_next_job().status == "stopped"


def test_s06_operations_stop_drain_fence_restart_and_resume_are_append_only(
    tmp_path: Path,
) -> None:
    operator = S01CommandPrincipal(
        subject="s06-operator",
        role="operator",
        scope="C-DEMO",
        source_id="s06-operations-console",
    )
    blocked: dict[str, object] = {}

    def stop_before_request(
        service: ControlledScenarioService,
        work_item_id: str,
        review: dict[str, object],
        claim: dict[str, object],
        finding: dict[str, object],
    ) -> None:
        blocked["stop"] = service.stop_new_supplement_requests(
            principal=operator,
            idempotency_key="s06-stop-requests",
            now=101,
        )
        blocked["request"] = service.request_supplement(
            principal=REVIEWER,
            work_item_id=work_item_id,
            finding_id=finding["finding_id"],
            reason_code="MISSING_REQUIRED_MATERIAL",
            expected_fence=claim["claim_fence"],
            expected_context=review["command_context"],
            idempotency_key="s06-blocked-request",
            now=101,
        )
        blocked["resume"] = service.resume_supplement_operations(
            principal=operator,
            idempotency_key="s06-resume-requests",
            now=102,
        )

    service, application_id, review, request, source = _ready_supplement_request(
        tmp_path,
        before_request=stop_before_request,
    )
    assert blocked["stop"]["requests"] == "closed"  # type: ignore[index]
    assert blocked["request"]["status"] == "stopped"  # type: ignore[index]
    assert blocked["request"]["reason_code"] == "supplement.requests_stopped"  # type: ignore[index]
    assert blocked["resume"]["requests"] == "open"  # type: ignore[index]

    closed_intake = service.stop_supplement_intake(
        principal=operator,
        idempotency_key="s06-stop-intake",
        now=200,
    )
    before_stopped_intake = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    stopped = service.submit_attachment_version(
        submission=_attachment_submission(request, source, closed=False),
        idempotency_key="s06-stopped-intake",
        principal=SUPPLEMENT_INTEGRATOR,
        now=201,
    )
    assert closed_intake["intake"] == "closed"
    assert stopped.reason_code == "supplement.intake_stopped"
    assert stopped.lifecycle_revision is None
    assert stopped.evidence_revision is None
    assert service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    ) == before_stopped_intake

    resumed_intake = service.resume_supplement_operations(
        principal=operator,
        idempotency_key="s06-resume-intake",
        now=202,
    )
    assert resumed_intake["intake"] == "open"
    draining = service.drain_supplement_operations(
        principal=operator,
        idempotency_key="s06-drain",
        now=202,
    )
    assert draining["drain"] == "draining"
    assert draining["requests"] == "closed"
    assert draining["intake"] == "open"
    assert draining["open_request_count"] == 1
    progress = service.submit_attachment_version(
        submission=_attachment_submission(request, source, closed=False),
        idempotency_key="s06-operation-progress",
        principal=SUPPLEMENT_INTEGRATOR,
        now=203,
    )
    assert progress.disposition is AdmissionDisposition.ACCEPTED
    fulfilled = service.submit_attachment_version(
        submission=_attachment_submission(request, source, closed=True),
        idempotency_key="s06-operation-closure",
        principal=SUPPLEMENT_INTEGRATOR,
        now=204,
    )
    stopped_after_drain = service.stop_supplement_intake(
        principal=operator,
        idempotency_key="s06-stop-intake-after-drain",
        now=205,
    )
    fenced = service.fence_supplement_workers(
        principal=operator,
        idempotency_key="s06-fence-workers",
        now=206,
    )
    worker = service.process_next_job()
    assert stopped_after_drain["requests"] == "closed"
    assert stopped_after_drain["intake"] == "closed"
    assert fenced["workers"] == "fenced"
    assert worker.status == "stopped"
    assert worker.reason_code == "supplement.workers_fenced"

    restarted = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "target.sqlite3",
        scenario_id="app_missing_vin_docs.json",
        registered_sources=(
            RegisteredSource(
                tenant_id="c-demo",
                source_system_id="s06-material-source",
                workload_identity_id="s06-material-workload",
                adapter_id="s06-detection-adapter",
                adapter_version="1",
                source_shape="ocr-detection/unversioned",
                producer_family="s06-ocr",
            ),
        ),
        controlled_objects=(
            ControlledObject(
                tenant_id="c-demo",
                source_system_id="s06-material-source",
                object_ref="s06-result-object",
                media_type="application/json",
                content=source["result"],
            ),
            ControlledObject(
                tenant_id="c-demo",
                source_system_id="s06-material-source",
                object_ref="s06-page-object",
                media_type="image/png",
                content=source["page"],
            ),
        ),
    )
    restarted_status = restarted.supplement_operations_status(principal=operator)
    assert restarted_status["requests"] == "closed"
    assert restarted_status["intake"] == "closed"
    assert restarted_status["workers"] == "fenced"
    assert restarted_status["open_request_count"] == 0
    assert restarted_status["queued_job_count"] == 1
    resumed_workers = restarted.resume_supplement_operations(
        principal=operator,
        idempotency_key="s06-resume-workers",
        now=207,
    )
    assert resumed_workers["workers"] == "open"
    completed = restarted.process_next_job()
    assert completed.status == "complete"
    assert completed.job_id == fulfilled.job_id
    history = restarted.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    assert [item["version"] for item in history["attachment_versions"]] == [1, 2]


@pytest.mark.parametrize(
    ("dependency", "reason_code"),
    (
        ("audit_available", "AUDIT_UNAVAILABLE"),
        ("storage_available", "STORAGE_UNAVAILABLE"),
    ),
)
def test_supplement_operations_fail_closed_when_dependency_is_unavailable(
    tmp_path: Path,
    dependency: str,
    reason_code: str,
) -> None:
    operator = S01CommandPrincipal(
        subject="s06-operator",
        role="operator",
        scope="C-DEMO",
        source_id="s06-operations-console",
    )
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "target.sqlite3",
        scenario_id="app_missing_vin_docs.json",
    )
    before = service.supplement_operations_status(principal=operator, now=100)
    setattr(service, dependency, False)

    result = service.stop_new_supplement_requests(
        principal=operator,
        idempotency_key=f"s06-operations-{dependency}",
        now=101,
    )

    assert result == {
        "status": "unavailable",
        "replayed": False,
        "reason_code": reason_code,
    }
    setattr(service, dependency, True)
    assert service.supplement_operations_status(principal=operator, now=102) == before


@pytest.mark.parametrize(
    "fault_point",
    (
        "supplement_operations.record",
        "supplement_operations.audit",
        "supplement_operations.idempotency",
        "supplement_operations.publish",
    ),
)
def test_each_supplement_operations_write_fault_is_atomic(
    tmp_path: Path,
    fault_point: str,
) -> None:
    active_faults = {fault_point}

    def fail(write_point: str) -> None:
        if write_point in active_faults:
            raise OSError("injected supplement operations write failure")

    operator = S01CommandPrincipal(
        subject="s06-operator",
        role="operator",
        scope="C-DEMO",
        source_id="s06-operations-console",
    )
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "target.sqlite3",
        scenario_id="app_missing_vin_docs.json",
        fault_injector=fail,
    )
    before = service.supplement_operations_status(principal=operator, now=100)

    failed = service.stop_new_supplement_requests(
        principal=operator,
        idempotency_key=f"s06-operations-fault-{fault_point}",
        now=101,
    )

    assert failed["status"] == "unavailable"
    assert failed["reason_code"] == (
        "AUDIT_UNAVAILABLE"
        if fault_point == "supplement_operations.audit"
        else "STORAGE_UNAVAILABLE"
    )
    assert service.supplement_operations_status(principal=operator, now=102) == before

    active_faults.clear()
    retried = service.stop_new_supplement_requests(
        principal=operator,
        idempotency_key=f"s06-operations-fault-{fault_point}",
        now=103,
    )
    assert retried["status"] == "accepted"
    assert retried["requests"] == "closed"


@pytest.mark.parametrize(
    ("case", "expected_disposition", "expected_reason"),
    (
        ("wrong_tenant", "rejected", "intake.source_disabled"),
        ("wrong_source", "rejected", "intake.source_disabled"),
        ("wrong_workload", "rejected", "intake.source_disabled"),
        ("wrong_application", "rejected", "intake.request_context_mismatch"),
        ("wrong_role", "rejected", "intake.request_context_mismatch"),
        ("wrong_material", "rejected", "intake.request_context_mismatch"),
        ("wrong_request_context", "rejected", "intake.request_context_mismatch"),
        ("wrong_hash", "quarantined", "evidence.integrity_failed"),
        ("wrong_media", "quarantined", "evidence.content_type_mismatch"),
        ("wrong_provenance", "quarantined", "adapter.mapping_ambiguous"),
    ),
)
def test_request_bound_intake_rejects_mismatches_without_business_effect(
    tmp_path: Path,
    case: str,
    expected_disposition: str,
    expected_reason: str,
) -> None:
    service, application_id, _, request, source = _ready_supplement_request(tmp_path)
    submission = copy.deepcopy(_attachment_submission(request, source, closed=False))
    principal = SUPPLEMENT_INTEGRATOR
    if case == "wrong_tenant":
        principal = S01CommandPrincipal(
            subject=SUPPLEMENT_INTEGRATOR.subject,
            role="integrator",
            scope="R-OBSERVED/other-tenant",
            source_id=SUPPLEMENT_INTEGRATOR.source_id,
        )
    elif case == "wrong_source":
        principal = S01CommandPrincipal(
            subject=SUPPLEMENT_INTEGRATOR.subject,
            role="integrator",
            scope=SUPPLEMENT_INTEGRATOR.scope,
            source_id="other-source",
        )
    elif case == "wrong_workload":
        submission["workload_identity_id"] = "other-workload"
    elif case == "wrong_application":
        submission["upstream_application_ref"] = "OTHER-APPLICATION"
    elif case == "wrong_role":
        submission["document_binding"]["document_role"] = "vehicle_invoice"
    elif case == "wrong_material":
        submission["document_binding"]["document_type"] = "vehicle_invoice"
    elif case == "wrong_request_context":
        submission["request_binding"]["request_context_digest"] = "0" * 64
    elif case == "wrong_hash":
        submission["attachments"][0]["object"]["sha256"] = "0" * 64
    elif case == "wrong_media":
        submission["attachments"][0]["object"]["media_type"] = "image/jpeg"
    elif case == "wrong_provenance":
        submission["producer"]["producer_family"] = "other-producer"
    before_request = service.supplement_request_view(
        principal=REVIEWER,
        request_id=request["request_id"],
        now=150,
    )
    before_history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )

    result = service.submit_attachment_version(
        submission=submission,
        idempotency_key=f"s06-invalid-{case}",
        principal=principal,
        now=200,
    )

    assert result.disposition.value == expected_disposition
    assert result.reason_code == expected_reason
    after_request = service.supplement_request_view(
        principal=REVIEWER,
        request_id=request["request_id"],
        now=201,
    )
    after_history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    assert after_request == before_request
    assert after_history == before_history
    assert service.process_next_job().status == "idle"


def test_closure_before_progress_waits_without_business_effect(tmp_path: Path) -> None:
    service, application_id, _, request, source = _ready_supplement_request(tmp_path)
    before = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )

    waiting = service.submit_attachment_version(
        submission=_attachment_submission(request, source, closed=True),
        idempotency_key="s06-closure-before-progress",
        principal=SUPPLEMENT_INTEGRATOR,
        now=200,
    )

    assert waiting.disposition is AdmissionDisposition.AWAITING_PREDECESSOR
    assert waiting.reason_code == "intake.sequence_gap"
    assert service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    ) == before
    assert service.supplement_request_view(
        principal=REVIEWER,
        request_id=request["request_id"],
        now=201,
    )["status"] == "open"


def test_response_loss_and_semantic_duplicate_replay_one_progress_effect(
    tmp_path: Path,
) -> None:
    service, application_id, _, request, source = _ready_supplement_request(tmp_path)
    submission = _attachment_submission(request, source, closed=False)
    accepted = service.submit_attachment_version(
        submission=submission,
        idempotency_key="s06-response-loss",
        principal=SUPPLEMENT_INTEGRATOR,
        now=200,
    )
    after_accept = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )

    replay = service.submit_attachment_version(
        submission=submission,
        idempotency_key="s06-response-loss",
        principal=SUPPLEMENT_INTEGRATOR,
        now=201,
    )
    duplicate = service.submit_attachment_version(
        submission=submission,
        idempotency_key="s06-semantic-duplicate",
        principal=SUPPLEMENT_INTEGRATOR,
        now=201,
    )
    changed = copy.deepcopy(submission)
    changed["envelope_id"] = "s06-changed-after-response-loss"
    conflict = service.submit_attachment_version(
        submission=changed,
        idempotency_key="s06-response-loss",
        principal=SUPPLEMENT_INTEGRATOR,
        now=201,
    )

    assert replay.receipt_id == accepted.receipt_id
    assert replay.replayed is True
    assert duplicate.receipt_id == accepted.receipt_id
    assert duplicate.replayed is True
    assert conflict.disposition is AdmissionDisposition.REJECTED
    assert conflict.reason_code == "intake.idempotency_conflict"
    assert service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    ) == after_accept


def test_same_source_revision_with_new_fingerprint_is_quarantined(
    tmp_path: Path,
) -> None:
    service, application_id, _, request, source = _ready_supplement_request(tmp_path)
    submission = _attachment_submission(request, source, closed=False)
    service.submit_attachment_version(
        submission=submission,
        idempotency_key="s06-source-revision-first",
        principal=SUPPLEMENT_INTEGRATOR,
        now=200,
    )
    before = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    conflicting = copy.deepcopy(submission)
    conflicting["envelope_id"] = "s06-conflicting-source-revision"

    result = service.submit_attachment_version(
        submission=conflicting,
        idempotency_key="s06-source-revision-conflict",
        principal=SUPPLEMENT_INTEGRATOR,
        now=201,
    )

    assert result.disposition is AdmissionDisposition.QUARANTINED
    assert result.reason_code == "intake.source_revision_conflict"
    assert service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    ) == before


@pytest.mark.parametrize("first_command", ("ordinary", "supplement"))
def test_source_revision_is_unique_across_canonical_command_types(
    tmp_path: Path,
    first_command: str,
) -> None:
    service, application_id, _, request, source = _ready_supplement_request(tmp_path)
    ordinary = _generic_observation_submission(request, source)
    ordinary["stream_id"] = "s06-supplement-stream"
    supplement = _attachment_submission(request, source, closed=False)

    if first_command == "ordinary":
        first = service.submit_registered(
            submission=ordinary,
            idempotency_key="s06-cross-command-ordinary-first",
            principal=SUPPLEMENT_INTEGRATOR,
        )
    else:
        first = service.submit_attachment_version(
            submission=supplement,
            idempotency_key="s06-cross-command-supplement-first",
            principal=SUPPLEMENT_INTEGRATOR,
            now=200,
        )
    before = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )

    if first_command == "ordinary":
        conflict = service.submit_attachment_version(
            submission=supplement,
            idempotency_key="s06-cross-command-supplement-second",
            principal=SUPPLEMENT_INTEGRATOR,
            now=200,
        )
    else:
        conflict = service.submit_registered(
            submission=ordinary,
            idempotency_key="s06-cross-command-ordinary-second",
            principal=SUPPLEMENT_INTEGRATOR,
        )

    assert first.disposition is AdmissionDisposition.ACCEPTED
    assert conflict.disposition is AdmissionDisposition.QUARANTINED
    assert conflict.reason_code == "intake.source_revision_conflict"
    assert service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    ) == before


@pytest.mark.parametrize(
    "fault_point",
    (
        "supplement_request.lifecycle",
        "supplement_request.request",
        "supplement_request.review_work",
        "supplement_request.work_item",
        "supplement_request.audit",
        "supplement_request.idempotency",
        "supplement_request.publish",
    ),
)
def test_each_supplement_request_write_fault_is_atomic(
    tmp_path: Path,
    fault_point: str,
) -> None:
    active_faults = {fault_point}

    def fail(write_point: str) -> None:
        if write_point in active_faults:
            raise OSError("injected supplement request write failure")

    service, application_id, work_item_id, review, claim, finding, _ = (
        _claimed_manual_review(tmp_path, fault_injector=fail)
    )
    history_before = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    work_before = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=101,
    )

    failed = _request_supplement(
        service,
        work_item_id,
        review,
        claim,
        finding,
        idempotency_key=f"s06-request-fault-{fault_point}",
    )

    assert failed["status"] == "unavailable"
    assert failed["reason_code"] == (
        "AUDIT_UNAVAILABLE"
        if fault_point == "supplement_request.audit"
        else "STORAGE_UNAVAILABLE"
    )
    assert service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    ) == history_before
    assert service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=101,
    ) == work_before

    active_faults.clear()
    retried = _request_supplement(
        service,
        work_item_id,
        review,
        claim,
        finding,
        idempotency_key=f"s06-request-fault-{fault_point}",
    )
    assert retried["status"] == "accepted"


@pytest.mark.parametrize(
    "fault_point",
    (
        "supplement_progress.evidence",
        "supplement_progress.lifecycle",
        "supplement_progress.request",
        "supplement_progress.receipt",
        "supplement_progress.audit",
        "supplement_progress.idempotency",
        "supplement_progress.publish",
    ),
)
def test_each_supplement_progress_write_fault_is_atomic(
    tmp_path: Path,
    fault_point: str,
) -> None:
    active_faults: set[str] = set()

    def fail(write_point: str) -> None:
        if write_point in active_faults:
            raise OSError("injected supplement progress write failure")

    service, application_id, _, request, source = _ready_supplement_request(
        tmp_path,
        fault_injector=fail,
    )
    history_before = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    request_before = service.supplement_request_view(
        principal=REVIEWER,
        request_id=request["request_id"],  # type: ignore[arg-type]
        now=200,
    )
    submission = _attachment_submission(request, source, closed=False)
    active_faults.add(fault_point)

    failed = service.submit_attachment_version(
        submission=submission,
        idempotency_key=f"s06-progress-fault-{fault_point}",
        principal=SUPPLEMENT_INTEGRATOR,
        now=200,
    )

    assert failed.disposition is AdmissionDisposition.REJECTED
    assert failed.reason_code == (
        "intake.audit_unavailable"
        if fault_point == "supplement_progress.audit"
        else "intake.storage_unavailable"
    )
    assert service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    ) == history_before
    assert service.supplement_request_view(
        principal=REVIEWER,
        request_id=request["request_id"],  # type: ignore[arg-type]
        now=200,
    ) == request_before
    assert service.process_next_job().status == "idle"

    active_faults.clear()
    retried = service.submit_attachment_version(
        submission=submission,
        idempotency_key=f"s06-progress-fault-{fault_point}",
        principal=SUPPLEMENT_INTEGRATOR,
        now=200,
    )
    assert retried.disposition is AdmissionDisposition.ACCEPTED
    assert retried.reason_code == "request_progress_accepted"


@pytest.mark.parametrize(
    "fault_point",
    (
        "supplement_fulfillment.lifecycle",
        "supplement_fulfillment.request",
        "supplement_fulfillment.work_item",
        "supplement_fulfillment.job",
        "supplement_fulfillment.outbox",
        "supplement_fulfillment.receipt",
        "supplement_fulfillment.audit",
        "supplement_fulfillment.idempotency",
        "supplement_fulfillment.publish",
    ),
)
def test_each_supplement_fulfillment_write_fault_is_atomic(
    tmp_path: Path,
    fault_point: str,
) -> None:
    active_faults: set[str] = set()

    def fail(write_point: str) -> None:
        if write_point in active_faults:
            raise OSError("injected supplement fulfillment write failure")

    service, application_id, _, request, source = _ready_supplement_request(
        tmp_path,
        fault_injector=fail,
    )
    service.submit_attachment_version(
        submission=_attachment_submission(request, source, closed=False),
        idempotency_key="s06-fulfillment-fault-progress",
        principal=SUPPLEMENT_INTEGRATOR,
        now=200,
    )
    history_before = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    request_before = service.supplement_request_view(
        principal=REVIEWER,
        request_id=request["request_id"],  # type: ignore[arg-type]
        now=201,
    )
    submission = _attachment_submission(request, source, closed=True)
    active_faults.add(fault_point)

    failed = service.submit_attachment_version(
        submission=submission,
        idempotency_key=f"s06-fulfillment-fault-{fault_point}",
        principal=SUPPLEMENT_INTEGRATOR,
        now=201,
    )

    assert failed.disposition is AdmissionDisposition.REJECTED
    assert failed.reason_code == (
        "intake.audit_unavailable"
        if fault_point == "supplement_fulfillment.audit"
        else "intake.storage_unavailable"
    )
    assert service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    ) == history_before
    assert service.supplement_request_view(
        principal=REVIEWER,
        request_id=request["request_id"],  # type: ignore[arg-type]
        now=201,
    ) == request_before
    assert service.process_next_job().status == "idle"

    active_faults.clear()
    retried = service.submit_attachment_version(
        submission=submission,
        idempotency_key=f"s06-fulfillment-fault-{fault_point}",
        principal=SUPPLEMENT_INTEGRATOR,
        now=201,
    )
    assert retried.disposition is AdmissionDisposition.ACCEPTED
    assert retried.reason_code == "request_fulfilled"


@pytest.mark.parametrize(
    "fault_point",
    (
        "supplement_expiry.lifecycle",
        "supplement_expiry.request",
        "supplement_expiry.work_item",
        "supplement_expiry.receipt",
        "supplement_expiry.audit",
        "supplement_expiry.idempotency",
        "supplement_expiry.publish",
    ),
)
def test_each_supplement_expiry_write_fault_is_atomic(
    tmp_path: Path,
    fault_point: str,
) -> None:
    active_faults: set[str] = set()

    def fail(write_point: str) -> None:
        if write_point in active_faults:
            raise OSError("injected supplement expiry write failure")

    service, application_id, _, request, source = _ready_supplement_request(
        tmp_path,
        fault_injector=fail,
    )
    service.submit_attachment_version(
        submission=_attachment_submission(request, source, closed=False),
        idempotency_key="s06-expiry-fault-progress",
        principal=SUPPLEMENT_INTEGRATOR,
        now=200,
    )
    history_before = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    request_before = service.supplement_request_view(
        principal=REVIEWER,
        request_id=request["request_id"],  # type: ignore[arg-type]
        now=request["due_at"],  # type: ignore[arg-type]
    )
    submission = _attachment_submission(request, source, closed=True)
    active_faults.add(fault_point)

    failed = service.submit_attachment_version(
        submission=submission,
        idempotency_key=f"s06-expiry-fault-{fault_point}",
        principal=SUPPLEMENT_INTEGRATOR,
        now=request["due_at"],  # type: ignore[arg-type]
    )

    assert failed.disposition is AdmissionDisposition.REJECTED
    assert failed.reason_code == (
        "intake.audit_unavailable"
        if fault_point == "supplement_expiry.audit"
        else "intake.storage_unavailable"
    )
    assert service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    ) == history_before
    assert service.supplement_request_view(
        principal=REVIEWER,
        request_id=request["request_id"],  # type: ignore[arg-type]
        now=request["due_at"],  # type: ignore[arg-type]
    ) == request_before

    active_faults.clear()
    retried = service.submit_attachment_version(
        submission=submission,
        idempotency_key=f"s06-expiry-fault-{fault_point}",
        principal=SUPPLEMENT_INTEGRATOR,
        now=request["due_at"],  # type: ignore[arg-type]
    )
    assert retried.reason_code == "supplement.deadline_reached"
    assert retried.request_status == "expired"


@pytest.mark.parametrize(
    "fault_point",
    (
        "supplement_invalidation.lifecycle",
        "supplement_invalidation.request",
        "supplement_invalidation.work_item",
        "supplement_invalidation.receipt",
        "supplement_invalidation.audit",
        "supplement_invalidation.idempotency",
        "supplement_invalidation.publish",
    ),
)
def test_each_supplement_invalidation_write_fault_is_atomic(
    tmp_path: Path,
    fault_point: str,
) -> None:
    active_faults: set[str] = set()

    def fail(write_point: str) -> None:
        if write_point in active_faults:
            raise OSError("injected supplement invalidation write failure")

    service, application_id, _, request, source = _ready_supplement_request(
        tmp_path,
        fault_injector=fail,
    )
    service.submit_attachment_version(
        submission=_attachment_submission(request, source, closed=False),
        idempotency_key="s06-invalidation-fault-progress",
        principal=SUPPLEMENT_INTEGRATOR,
        now=200,
    )
    history_before = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    request_before = service.supplement_request_view(
        principal=REVIEWER,
        request_id=request["request_id"],  # type: ignore[arg-type]
        now=201,
    )
    submission = _attachment_submission(request, source, closed=True)
    active_faults.update({"review.source_read", fault_point})

    failed = service.submit_attachment_version(
        submission=submission,
        idempotency_key=f"s06-invalidation-fault-{fault_point}",
        principal=SUPPLEMENT_INTEGRATOR,
        now=201,
    )

    assert failed.disposition is AdmissionDisposition.REJECTED
    assert failed.reason_code == (
        "intake.audit_unavailable"
        if fault_point == "supplement_invalidation.audit"
        else "intake.storage_unavailable"
    )
    assert service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    ) == history_before
    assert service.supplement_request_view(
        principal=REVIEWER,
        request_id=request["request_id"],  # type: ignore[arg-type]
        now=201,
    ) == request_before

    active_faults.remove(fault_point)
    retried = service.submit_attachment_version(
        submission=submission,
        idempotency_key=f"s06-invalidation-fault-{fault_point}",
        principal=SUPPLEMENT_INTEGRATOR,
        now=201,
    )
    assert retried.reason_code == "supplement.source_evidence_unavailable"
    assert retried.request_status == "invalidated"


def test_unrelated_generic_canonical_submission_has_no_request_effect(
    tmp_path: Path,
) -> None:
    service, application_id, _, request, source = _ready_supplement_request(tmp_path)
    request_before = service.supplement_request_view(
        principal=REVIEWER,
        request_id=request["request_id"],  # type: ignore[arg-type]
        now=200,
    )
    history_before = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )

    generic = service.submit_registered(
        submission=_generic_observation_submission(request, source),
        idempotency_key="s06-unrelated-generic-submission",
        principal=SUPPLEMENT_INTEGRATOR,
    )

    assert generic.disposition is AdmissionDisposition.ACCEPTED
    assert generic.application_id != application_id
    assert generic.request_id is None
    assert generic.request_status is None
    assert generic.fulfilled is None
    assert service.supplement_request_view(
        principal=REVIEWER,
        request_id=request["request_id"],  # type: ignore[arg-type]
        now=200,
    ) == request_before
    assert service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    ) == history_before


@pytest.mark.parametrize("winner", ("request", "correction"))
def test_request_and_correction_store_cas_has_one_winner(
    tmp_path: Path,
    winner: str,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    losing_point = (
        "correction.publish"
        if winner == "request"
        else "supplement_request.publish"
    )

    def block_loser(write_point: str) -> None:
        if write_point != losing_point:
            return
        entered.set()
        if not release.wait(timeout=5):
            raise TimeoutError("S06 request/correction loser was not released")

    service, application_id, work_item_id, review, claim, finding, source = (
        _claimed_manual_review(
            tmp_path,
            fault_injector=block_loser if winner == "correction" else None,
        )
    )
    correction = _source_backed_correction(
        service,
        application_id,
        finding["finding_id"],  # type: ignore[arg-type]
    )
    correction_service = _supplement_service(
        tmp_path,
        source,
        fault_injector=block_loser if winner == "request" else None,
    )

    def request() -> dict[str, object]:
        return _request_supplement(
            service,
            work_item_id,
            review,
            claim,
            finding,
            idempotency_key=f"s06-race-request-{winner}",
        )

    def correct() -> dict[str, object]:
        return correction_service.correct_field_observation(
            principal=REVIEWER,
            application_id=application_id,
            work_item_id=work_item_id,
            expected_fence=claim["claim_fence"],  # type: ignore[arg-type]
            expected_context=review["command_context"],  # type: ignore[arg-type]
            idempotency_key=f"s06-race-correction-{winner}",
            correction=correction,
            now=101,
        )

    loser = correct if winner == "request" else request
    winning_command = request if winner == "request" else correct
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(loser)
        assert entered.wait(timeout=5)
        try:
            winning_result = winning_command()
        finally:
            release.set()
        losing_result = future.result(timeout=5)

    assert winning_result["status"] == "accepted"
    assert losing_result["status"] == "stale"
    history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    route = service.current_route_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    if winner == "request":
        assert history["corrections"] == []
        assert route["phase"] == "Supplement"
        assert route["route"] == "supplement_pending"
        assert service.supplement_request_view(
            principal=REVIEWER,
            request_id=winning_result["request_id"],  # type: ignore[arg-type]
            now=102,
        )["status"] == "open"
    else:
        assert len(history["corrections"]) == 1
        assert route["phase"] == "Assembly"
        assert route["route"] == "pending_check"


def test_fulfillment_fences_a_correction_already_in_flight_before_request(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def block_correction(write_point: str) -> None:
        if write_point != "correction.publish":
            return
        entered.set()
        if not release.wait(timeout=5):
            raise TimeoutError("S06 in-flight correction was not released")

    service, application_id, work_item_id, review, claim, finding, source = (
        _claimed_manual_review(tmp_path)
    )
    correction_service = _supplement_service(
        tmp_path,
        source,
        fault_injector=block_correction,
    )
    correction = _source_backed_correction(
        service,
        application_id,
        finding["finding_id"],  # type: ignore[arg-type]
    )

    def correct() -> dict[str, object]:
        return correction_service.correct_field_observation(
            principal=REVIEWER,
            application_id=application_id,
            work_item_id=work_item_id,
            expected_fence=claim["claim_fence"],  # type: ignore[arg-type]
            expected_context=review["command_context"],  # type: ignore[arg-type]
            idempotency_key="s06-in-flight-correction",
            correction=correction,
            now=101,
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(correct)
        assert entered.wait(timeout=5)
        try:
            created = _request_supplement(
                service,
                work_item_id,
                review,
                claim,
                finding,
                idempotency_key="s06-in-flight-request",
            )
            request = service.supplement_request_view(
                principal=REVIEWER,
                request_id=created["request_id"],  # type: ignore[arg-type]
                now=102,
            )
            service.submit_attachment_version(
                submission=_attachment_submission(request, source, closed=False),
                idempotency_key="s06-in-flight-progress",
                principal=SUPPLEMENT_INTEGRATOR,
                now=200,
            )
            fulfilled = service.submit_attachment_version(
                submission=_attachment_submission(request, source, closed=True),
                idempotency_key="s06-in-flight-fulfillment",
                principal=SUPPLEMENT_INTEGRATOR,
                now=201,
            )
        finally:
            release.set()
        stale_correction = future.result(timeout=5)

    assert fulfilled.reason_code == "request_fulfilled"
    assert stale_correction["status"] == "stale"
    history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    assert history["corrections"] == []
    assert [item["version"] for item in history["attachment_versions"]] == [1, 2]


@pytest.mark.parametrize("winner", ("fulfillment", "expiry"))
def test_fulfillment_and_deadline_store_cas_has_one_terminal_winner(
    tmp_path: Path,
    winner: str,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    losing_point = (
        "supplement_expiry.publish"
        if winner == "fulfillment"
        else "supplement_fulfillment.publish"
    )

    def block_loser(write_point: str) -> None:
        if write_point != losing_point:
            return
        entered.set()
        if not release.wait(timeout=5):
            raise TimeoutError("S06 fulfillment/deadline loser was not released")

    service, application_id, _, request, source = _ready_supplement_request(
        tmp_path,
        fault_injector=block_loser if winner == "expiry" else None,
    )
    service.submit_attachment_version(
        submission=_attachment_submission(request, source, closed=False),
        idempotency_key="s06-deadline-race-progress",
        principal=SUPPLEMENT_INTEGRATOR,
        now=200,
    )
    expiry_service = _supplement_service(
        tmp_path,
        source,
        fault_injector=block_loser if winner == "fulfillment" else None,
    )
    closure = _attachment_submission(request, source, closed=True)

    def fulfill() -> object:
        return service.submit_attachment_version(
            submission=closure,
            idempotency_key=f"s06-deadline-race-fulfill-{winner}",
            principal=SUPPLEMENT_INTEGRATOR,
            now=request["due_at"] - 1,  # type: ignore[operator]
        )

    def expire() -> object:
        return expiry_service.submit_attachment_version(
            submission=closure,
            idempotency_key=f"s06-deadline-race-expire-{winner}",
            principal=SUPPLEMENT_INTEGRATOR,
            now=request["due_at"],  # type: ignore[arg-type]
        )

    loser = expire if winner == "fulfillment" else fulfill
    winning_command = fulfill if winner == "fulfillment" else expire
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(loser)
        assert entered.wait(timeout=5)
        try:
            winning_result = winning_command()
        finally:
            release.set()
        losing_result = future.result(timeout=5)

    expected_reason = (
        "request_fulfilled"
        if winner == "fulfillment"
        else "supplement.deadline_reached"
    )
    expected_status = "fulfilled" if winner == "fulfillment" else "expired"
    assert winning_result.reason_code == expected_reason
    assert winning_result.replayed is False
    if winner == "fulfillment":
        assert losing_result.reason_code == expected_reason
        assert winning_result.receipt_id == losing_result.receipt_id
        assert losing_result.replayed is True
    else:
        assert losing_result.reason_code == "evidence.late_input_requires_reopen"
        assert losing_result.disposition is AdmissionDisposition.REJECTED
        assert losing_result.replayed is False
    terminal = service.supplement_request_view(
        principal=REVIEWER,
        request_id=request["request_id"],  # type: ignore[arg-type]
        now=request["due_at"],  # type: ignore[arg-type]
    )
    assert terminal["status"] == expected_status
    history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    assert len(history["attachment_versions"]) == 2


def test_two_integrators_racing_final_closure_publish_one_fulfillment(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def block_first(write_point: str) -> None:
        if write_point != "supplement_fulfillment.publish":
            return
        entered.set()
        if not release.wait(timeout=5):
            raise TimeoutError("S06 first Integrator was not released")

    service, application_id, _, request, source = _ready_supplement_request(
        tmp_path,
        fault_injector=block_first,
    )
    service.submit_attachment_version(
        submission=_attachment_submission(request, source, closed=False),
        idempotency_key="s06-integrator-race-progress",
        principal=SUPPLEMENT_INTEGRATOR,
        now=200,
    )
    second = _supplement_service(tmp_path, source)
    other_integrator = S01CommandPrincipal(
        subject="s06-integrator-two",
        role="integrator",
        scope=SUPPLEMENT_INTEGRATOR.scope,
        source_id=SUPPLEMENT_INTEGRATOR.source_id,
    )
    closure = _attachment_submission(request, source, closed=True)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            service.submit_attachment_version,
            submission=closure,
            idempotency_key="s06-integrator-race-one",
            principal=SUPPLEMENT_INTEGRATOR,
            now=201,
        )
        assert entered.wait(timeout=5)
        try:
            winner = second.submit_attachment_version(
                submission=closure,
                idempotency_key="s06-integrator-race-two",
                principal=other_integrator,
                now=201,
            )
        finally:
            release.set()
        replay = future.result(timeout=5)

    assert winner.reason_code == "request_fulfilled"
    assert replay.reason_code == "request_fulfilled"
    assert winner.receipt_id == replay.receipt_id
    assert (winner.replayed, replay.replayed) == (False, True)
    assert service.process_next_job().status == "complete"
    assert service.process_next_job().status == "idle"
    history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    assert len(history["runs"]) == 2
    assert sum(run["current"] for run in history["runs"]) == 1


@pytest.mark.parametrize("terminal_status", ("fulfilled", "expired", "invalidated"))
def test_late_input_after_terminal_request_requires_reopen_without_business_effect(
    tmp_path: Path,
    terminal_status: str,
) -> None:
    active_faults: set[str] = set()

    def fail(write_point: str) -> None:
        if write_point in active_faults:
            raise OSError("injected terminal source failure")

    service, application_id, _, request, source = _ready_supplement_request(
        tmp_path,
        fault_injector=fail,
    )
    service.submit_attachment_version(
        submission=_attachment_submission(request, source, closed=False),
        idempotency_key=f"s06-terminal-progress-{terminal_status}",
        principal=SUPPLEMENT_INTEGRATOR,
        now=200,
    )
    closure = _attachment_submission(request, source, closed=True)
    if terminal_status == "fulfilled":
        terminal = service.submit_attachment_version(
            submission=closure,
            idempotency_key="s06-terminal-fulfilled",
            principal=SUPPLEMENT_INTEGRATOR,
            now=201,
        )
    elif terminal_status == "expired":
        terminal = service.submit_attachment_version(
            submission=closure,
            idempotency_key="s06-terminal-expired",
            principal=SUPPLEMENT_INTEGRATOR,
            now=request["due_at"],  # type: ignore[arg-type]
        )
    else:
        active_faults.add("review.source_read")
        terminal = service.submit_attachment_version(
            submission=closure,
            idempotency_key="s06-terminal-invalidated",
            principal=SUPPLEMENT_INTEGRATOR,
            now=201,
        )
    assert terminal.request_status == terminal_status
    request_before = service.supplement_request_view(
        principal=REVIEWER,
        request_id=request["request_id"],  # type: ignore[arg-type]
        now=202,
    )
    history_before = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    late_submission = copy.deepcopy(closure)
    late_submission["envelope_id"] = f"s06-late-after-{terminal_status}"

    late = service.submit_attachment_version(
        submission=late_submission,
        idempotency_key=f"s06-late-after-{terminal_status}",
        principal=SUPPLEMENT_INTEGRATOR,
        now=request["due_at"] + 1,  # type: ignore[operator]
    )

    assert late.disposition is AdmissionDisposition.REJECTED
    assert late.reason_code == "evidence.late_input_requires_reopen"
    assert service.supplement_request_view(
        principal=REVIEWER,
        request_id=request["request_id"],  # type: ignore[arg-type]
        now=202,
    ) == request_before
    assert service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    ) == history_before


def test_supplement_successor_fences_an_old_in_flight_run_result(
    tmp_path: Path,
) -> None:
    checker_entered = threading.Event()
    release_checker = threading.Event()
    delegate = RuleEngine(load_rules(ROOT / "configs" / "rules_auto_lease.yaml"))

    def blocking_checker(application: object) -> object:
        checker_entered.set()
        if not release_checker.wait(timeout=5):
            raise TimeoutError("S06 old worker was not released")
        return delegate.run(application)  # type: ignore[arg-type]

    source = _supplement_source()
    old_worker = _supplement_service(
        tmp_path,
        source,
        checker_runner=blocking_checker,
        worker_identity="s06-old-worker",
        clock=lambda: 0,
    )
    admitted = old_worker.submit_demo(
        scenario_id="app_missing_vin_docs.json",
        idempotency_key="s06-old-result-intake",
        principal=INTAKE,
    )
    assert admitted.application_id is not None
    application_id = admitted.application_id
    late_results: list[object] = []
    errors: list[BaseException] = []

    def finish_old_attempt() -> None:
        try:
            late_results.append(old_worker.process_next_job())
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    worker = threading.Thread(target=finish_old_attempt)
    worker.start()
    assert checker_entered.wait(timeout=5)
    takeover = _supplement_service(
        tmp_path,
        source,
        worker_identity="s06-takeover-worker",
        clock=lambda: 31,
    )
    initial = takeover.process_next_job()
    assert initial.status == "complete"
    takeover.refresh_projection()
    queue = takeover.queue_view(
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=31,
    )
    work_item_id = queue["items"][0]["work_item_id"]
    review = takeover.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=31,
    )
    finding = next(
        item
        for item in review["automatic_findings"]
        if item["rule_id"] == "R_VIN_CROSS"
    )
    claim = takeover.claim_review_work_item(
        principal=REVIEWER,
        work_item_id=work_item_id,
        expected_context=review["command_context"],
        now=31,
    )
    created = _request_supplement(
        takeover,
        work_item_id,
        review,
        claim,
        finding,
        idempotency_key="s06-old-result-request",
        now=32,
    )
    request = takeover.supplement_request_view(
        principal=REVIEWER,
        request_id=created["request_id"],  # type: ignore[arg-type]
        now=32,
    )
    takeover.submit_attachment_version(
        submission=_attachment_submission(request, source, closed=False),
        idempotency_key="s06-old-result-progress",
        principal=SUPPLEMENT_INTEGRATOR,
        now=200,
    )
    takeover.submit_attachment_version(
        submission=_attachment_submission(request, source, closed=True),
        idempotency_key="s06-old-result-fulfillment",
        principal=SUPPLEMENT_INTEGRATOR,
        now=201,
    )
    try:
        release_checker.set()
        worker.join(timeout=5)
    finally:
        release_checker.set()
    assert not worker.is_alive()
    assert errors == []
    assert late_results[0].status == "stale"  # type: ignore[union-attr]
    assert late_results[0].reason_code == "STALE_COMPARE_AND_SET"  # type: ignore[union-attr]
    finisher = _supplement_service(
        tmp_path,
        source,
        worker_identity="s06-successor-worker",
        clock=lambda: 33,
    )
    successor = finisher.process_next_job()
    history = finisher.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    assert len(history["runs"]) == 3
    assert sum(run["current"] for run in history["runs"]) == 1
    current = next(run for run in history["runs"] if run["current"])
    assert successor.status == "complete"
    assert current["run_id"] == successor.run_id
    assert current["run_id"] != initial.run_id


def test_restart_rebuilds_missing_supplement_job_from_durable_outbox(
    tmp_path: Path,
) -> None:
    service, application_id, _, request, source = _ready_supplement_request(tmp_path)
    service.submit_attachment_version(
        submission=_attachment_submission(request, source, closed=False),
        idempotency_key="s06-outbox-progress",
        principal=SUPPLEMENT_INTEGRATOR,
        now=200,
    )
    fulfilled = service.submit_attachment_version(
        submission=_attachment_submission(request, source, closed=True),
        idempotency_key="s06-outbox-fulfillment",
        principal=SUPPLEMENT_INTEGRATOR,
        now=201,
    )
    with sqlite3.connect(tmp_path / "target.sqlite3") as connection:
        connection.execute("DELETE FROM jobs WHERE item_id = ?", (fulfilled.job_id,))
        connection.commit()

    restarted = _supplement_service(tmp_path, source)
    completed = restarted.process_next_job()

    assert completed.status == "complete"
    assert completed.job_id == fulfilled.job_id
    history = restarted.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    assert [item["version"] for item in history["attachment_versions"]] == [1, 2]
    assert sum(run["current"] for run in history["runs"]) == 1


def test_stop_request_bound_intake_fences_a_submission_blocked_before_publish(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def block_submission(write_point: str) -> None:
        if write_point != "supplement_progress.publish":
            return
        entered.set()
        if not release.wait(timeout=5):
            raise TimeoutError("S06 stopped submission was not released")

    service, application_id, _, request, source = _ready_supplement_request(
        tmp_path,
        fault_injector=block_submission,
    )
    operator = S01CommandPrincipal(
        subject="s06-stop-race-operator",
        role="operator",
        scope="C-DEMO",
        source_id="s06-operations-console",
    )
    control = _supplement_service(tmp_path, source)
    history_before = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            service.submit_attachment_version,
            submission=_attachment_submission(request, source, closed=False),
            idempotency_key="s06-stop-race-progress",
            principal=SUPPLEMENT_INTEGRATOR,
            now=200,
        )
        assert entered.wait(timeout=5)
        try:
            stopped = control.stop_supplement_intake(
                principal=operator,
                idempotency_key="s06-stop-race-control",
                now=200,
            )
        finally:
            release.set()
        rejected = future.result(timeout=5)

    assert stopped["intake"] == "closed"
    assert rejected.disposition is AdmissionDisposition.REJECTED
    assert rejected.reason_code == "supplement.intake_stopped"
    assert service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    ) == history_before
    assert service.supplement_request_view(
        principal=REVIEWER,
        request_id=request["request_id"],  # type: ignore[arg-type]
        now=201,
    )["status"] == "open"


def test_worker_fence_invalidates_an_in_flight_supplement_result(
    tmp_path: Path,
) -> None:
    service, application_id, _, request, source = _ready_supplement_request(tmp_path)
    service.submit_attachment_version(
        submission=_attachment_submission(request, source, closed=False),
        idempotency_key="s06-worker-fence-progress",
        principal=SUPPLEMENT_INTEGRATOR,
        now=200,
    )
    service.submit_attachment_version(
        submission=_attachment_submission(request, source, closed=True),
        idempotency_key="s06-worker-fence-fulfillment",
        principal=SUPPLEMENT_INTEGRATOR,
        now=201,
    )
    checker_entered = threading.Event()
    release_checker = threading.Event()
    delegate = RuleEngine(load_rules(ROOT / "configs" / "rules_auto_lease.yaml"))

    def blocking_checker(application: object) -> object:
        checker_entered.set()
        if not release_checker.wait(timeout=5):
            raise TimeoutError("S06 fenced worker was not released")
        return delegate.run(application)  # type: ignore[arg-type]

    worker_service = _supplement_service(
        tmp_path,
        source,
        checker_runner=blocking_checker,
        worker_identity="s06-fenced-worker",
        clock=lambda: 0,
    )
    late_results: list[object] = []
    worker = threading.Thread(
        target=lambda: late_results.append(worker_service.process_next_job())
    )
    worker.start()
    assert checker_entered.wait(timeout=5)
    operator = S01CommandPrincipal(
        subject="s06-worker-fence-operator",
        role="operator",
        scope="C-DEMO",
        source_id="s06-operations-console",
    )
    active_faults = {"supplement_operations.worker_fence"}

    def fail(write_point: str) -> None:
        if write_point in active_faults:
            raise OSError("injected supplement worker-fence write failure")

    control = _supplement_service(tmp_path, source, fault_injector=fail)
    try:
        failed = control.fence_supplement_workers(
            principal=operator,
            idempotency_key="s06-worker-fence-fault",
            now=1,
        )
        assert failed["status"] == "unavailable"
        assert failed["reason_code"] == "STORAGE_UNAVAILABLE"
        assert control.supplement_operations_status(
            principal=operator,
            now=1,
        )["workers"] == "open"
        active_faults.clear()
        fenced = control.fence_supplement_workers(
            principal=operator,
            idempotency_key="s06-worker-fence-control",
            now=1,
        )
    finally:
        release_checker.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert fenced["workers"] == "fenced"
    assert late_results[0].status == "stale"  # type: ignore[union-attr]
    assert late_results[0].reason_code == "STALE_COMPARE_AND_SET"  # type: ignore[union-attr]
    control.resume_supplement_operations(
        principal=operator,
        idempotency_key="s06-worker-fence-resume",
        now=31,
    )
    takeover = _supplement_service(
        tmp_path,
        source,
        worker_identity="s06-fence-takeover",
        clock=lambda: 31,
    )
    completed = takeover.process_next_job()
    history = takeover.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    assert completed.status == "complete"
    assert sum(run["current"] for run in history["runs"]) == 1


INTEGRATOR_PROJECTION_KEYS = frozenset(
    {
        "schema_version",
        "request_id",
        "status",
        "current",
        "requested_at",
        "due_at",
        "context_digest",
        "upstream_application_ref",
        "material_requirement",
        "expected_predecessor_attachment_id",
        "expected_predecessor_attachment_version",
        "next_attachment_version",
        "next_request_progress_revision",
        "next_source_revision",
        "expected_predecessor_revision",
        "next_batch_item_sequence",
        "batch",
    }
)
INTEGRATOR_MATERIAL_KEYS = frozenset(
    {
        "material_requirement_id",
        "document_role",
        "material_kind",
        "operation",
        "required_fact_kinds",
        "responsible_party",
        "allowed_tenant_id",
        "allowed_source_system_ids",
        "allowed_workload_identity_ids",
        "batch_item_count",
        "batch_closure_required",
        "integrity_required",
        "provenance_required",
        "evidence_eligibility_required",
    }
)
INTEGRATOR_BATCH_KEYS = frozenset({"batch_id", "manifest_digest", "stream_id"})
INTEGRATOR_INTERNAL_KEYS = frozenset(
    {
        "application_id",
        "work_item_id",
        "source_work_item_id",
        "cycle",
        "run_id",
        "finding_id",
        "rule_id",
        "finding_reason_code",
        "finding_verdict",
        "requester_claim_fence",
        "fixed_context",
        "phase",
        "route",
        "lifecycle_revision",
        "evidence_revision",
        "projection_watermark",
        "requester_subject",
        "requester_role",
        "requester_source_id",
        "evidence_snapshot_id",
        "evidence_snapshot_digest",
        "satisfaction_policy_id",
        "satisfaction_policy_digest",
    }
)


def _attachment_submission_from_projection(
    projection: dict[str, object],
    source: dict[str, object],
    *,
    closed: bool,
    batch_id: str,
    stream_id: str,
) -> dict[str, object]:
    material = projection["material_requirement"]
    item_sequence = projection["next_batch_item_sequence"]
    manifest = {
        "batch_id": batch_id,
        "final_sequence": material["batch_item_count"],
        "item_count": material["batch_item_count"],
        "scope_mode": "full",
        "stream_id": stream_id,
        "supplement_request_id": projection["request_id"],
    }
    return {
        "envelope_id": f"t04-envelope-{item_sequence}",
        "schema_version": "1.0.0",
        "semantic_version": "1.0.0",
        "command_type": "submit_attachment_version",
        "upstream_application_ref": projection["upstream_application_ref"],
        "stream_id": stream_id,
        "source_revision": projection["next_source_revision"],
        "predecessor_revision": projection["expected_predecessor_revision"],
        "must_understand": [],
        "workload_identity_id": material["allowed_workload_identity_ids"][0],
        "request_binding": {
            "supplement_request_id": projection["request_id"],
            "request_context_digest": projection["context_digest"],
            "material_requirement_id": material["material_requirement_id"],
            "request_progress_revision": projection[
                "next_request_progress_revision"
            ],
        },
        "document_binding": {
            "source_document_ref": "s06-lease-replacement",
            "document_type": material["material_kind"],
            "document_role": material["document_role"],
        },
        "attachment_lineage": {
            "operation": material["operation"],
            "predecessor_attachment_id": projection[
                "expected_predecessor_attachment_id"
            ],
            "predecessor_attachment_version": projection[
                "expected_predecessor_attachment_version"
            ],
            "attachment_version": projection["next_attachment_version"],
        },
        "batch": {
            "batch_id": batch_id,
            "item_sequence": item_sequence,
            "item_count": material["batch_item_count"],
            "final_sequence": material["batch_item_count"],
            "scope_mode": "full",
            "closed": closed,
            "manifest_digest": hashlib.sha256(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            ).hexdigest(),
        },
        "result_object": _descriptor(
            "s06-result-object", "application/json", source["result"]
        ),
        "attachments": [
            {
                "source_attachment_ref": "s06-source-attachment-2",
                "page_ref": "s06-source-page-2",
                "page_ordinal": 1,
                "source_name_sha256": hashlib.sha256(
                    b"lease-page.png"
                ).hexdigest(),
                "object": _descriptor("s06-page-object", "image/png", source["page"]),
            }
        ],
        "producer": {
            "producer_id": "s06-producer",
            "producer_family": "s06-ocr",
            "task_id": "s06-lease-field-extraction",
            "task_version": "1",
            "run_id": "s06-producer-run-1",
            "model_id": "s06-model",
            "model_version": "1",
            "coordinate_system": {
                "name": "pixel",
                "unit": "pixel",
                "origin": "top_left",
            },
            "confidence_semantics": {
                "minimum": 0.0,
                "maximum": 1.0,
                "higher_is": "stronger_detection",
                "meaning": "producer_detection_score",
                "granularity": "observation",
                "calibration": "unknown",
            },
        },
    }


def test_integrator_projection_binds_each_next_command_and_hides_scope(
    tmp_path: Path,
) -> None:
    service, application_id, _, request, source = _ready_supplement_request(tmp_path)
    with pytest.raises(QueryNotFound):
        service.integrator_supplement_request_view(
            principal=REVIEWER, request_id=request["request_id"], now=102
        )
    with pytest.raises(QueryNotFound):
        service.integrator_supplement_request_view(
            principal=S01CommandPrincipal(
                subject="s06-other-source",
                role="integrator",
                scope="R-OBSERVED/c-demo",
                source_id="s06-other-source",
            ),
            request_id=request["request_id"],
            now=102,
        )
    with pytest.raises(QueryNotFound):
        service.integrator_supplement_request_view(
            principal=S01CommandPrincipal(
                subject="s06-other-tenant",
                role="integrator",
                scope="R-OBSERVED/other-tenant",
                source_id="s06-material-source",
            ),
            request_id=request["request_id"],
            now=102,
        )
    with pytest.raises(QueryNotFound):
        service.integrator_supplement_request_view(
            principal=S01CommandPrincipal(
                subject="s06-expired",
                role="integrator",
                scope="R-OBSERVED/c-demo",
                source_id="s06-material-source",
                expires_at=1,
            ),
            request_id=request["request_id"],
            now=102,
        )
    with pytest.raises(QueryNotFound):
        service.integrator_supplement_request_view(
            principal=SUPPLEMENT_INTEGRATOR,
            request_id="supplement_request_missing00000000000000000000000",
            now=102,
        )

    projection = service.integrator_supplement_request_view(
        principal=SUPPLEMENT_INTEGRATOR,
        request_id=request["request_id"],
        now=102,
    )
    assert set(projection) == INTEGRATOR_PROJECTION_KEYS
    assert set(projection["material_requirement"]) == INTEGRATOR_MATERIAL_KEYS
    assert set(projection["batch"]) == INTEGRATOR_BATCH_KEYS
    assert INTEGRATOR_INTERNAL_KEYS.isdisjoint(projection)
    assert projection["schema_version"] == "supplement-request-integrator/1"
    assert projection["request_id"] == request["request_id"]
    assert projection["status"] == "open"
    assert projection["current"] is True
    assert projection["requested_at"] == request["requested_at"]
    assert projection["due_at"] == request["due_at"]
    assert projection["context_digest"] == request["context_digest"]
    assert projection["upstream_application_ref"] == "APP-MISS-VINDOC"
    assert projection["expected_predecessor_attachment_id"] == request[
        "expected_predecessor_attachment_id"
    ]
    assert projection["expected_predecessor_attachment_version"] == request[
        "expected_predecessor_attachment_version"
    ]
    assert projection["next_attachment_version"] == 2
    assert projection["next_request_progress_revision"] == 1
    assert projection["next_source_revision"] == 1
    assert projection["expected_predecessor_revision"] is None
    assert projection["next_batch_item_sequence"] == 1
    assert projection["batch"] == {"batch_id": None, "manifest_digest": None, "stream_id": None}
    material = projection["material_requirement"]
    assert material["material_requirement_id"] == "c-demo-financing-lease-vin/1"
    assert material["document_role"] == "financing_lease_contract"
    assert material["material_kind"] == "financing_lease_contract"
    assert material["operation"] == "replacement"
    assert material["responsible_party"] == "application_material_provider"
    assert material["allowed_tenant_id"] == "c-demo"
    assert material["allowed_source_system_ids"] == ["s06-material-source"]
    assert material["allowed_workload_identity_ids"] == ["s06-material-workload"]
    assert material["batch_item_count"] == 2
    assert material["batch_closure_required"] is True
    assert material["integrity_required"] is True
    assert material["provenance_required"] is True
    assert material["evidence_eligibility_required"] is True

    progress = service.submit_attachment_version(
        submission=_attachment_submission_from_projection(
            projection,
            source,
            closed=False,
            batch_id="s06-batch-1",
            stream_id="s06-supplement-stream",
        ),
        idempotency_key="s06-t04-projection-progress",
        principal=SUPPLEMENT_INTEGRATOR,
        now=200,
    )
    assert progress.disposition is AdmissionDisposition.ACCEPTED
    assert progress.request_status == "open"

    after_progress = service.integrator_supplement_request_view(
        principal=SUPPLEMENT_INTEGRATOR,
        request_id=request["request_id"],
        now=201,
    )
    assert after_progress["status"] == "open"
    assert after_progress["current"] is True
    assert after_progress["next_request_progress_revision"] == 2
    assert after_progress["next_source_revision"] == 2
    assert after_progress["expected_predecessor_revision"] == 1
    assert after_progress["next_batch_item_sequence"] == 2
    assert after_progress["next_attachment_version"] == 2
    assert after_progress["batch"]["batch_id"] == "s06-batch-1"
    assert after_progress["batch"]["stream_id"] == "s06-supplement-stream"
    expected_manifest_digest = hashlib.sha256(
        json.dumps(
            {
                "batch_id": "s06-batch-1",
                "final_sequence": 2,
                "item_count": 2,
                "scope_mode": "full",
                "stream_id": "s06-supplement-stream",
                "supplement_request_id": request["request_id"],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert after_progress["batch"]["manifest_digest"] == expected_manifest_digest

    closed_submission = _attachment_submission_from_projection(
        after_progress,
        source,
        closed=True,
        batch_id="s06-batch-1",
        stream_id="s06-supplement-stream",
    )
    assert (
        closed_submission["batch"]["manifest_digest"]
        == after_progress["batch"]["manifest_digest"]
    )
    fulfilled = service.submit_attachment_version(
        submission=closed_submission,
        idempotency_key="s06-t04-projection-closure",
        principal=SUPPLEMENT_INTEGRATOR,
        now=202,
    )
    assert fulfilled.disposition is AdmissionDisposition.ACCEPTED
    assert fulfilled.request_status == "fulfilled"

    terminal = service.integrator_supplement_request_view(
        principal=SUPPLEMENT_INTEGRATOR,
        request_id=request["request_id"],
        now=203,
    )
    assert terminal["status"] == "fulfilled"
    assert terminal["current"] is False
    assert service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )["attachment_versions"][-1]["current"] is True
    assert service.process_next_job().status == "complete"
    service.refresh_projection()
    history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    assert [run["current"] for run in history["runs"]] == [False, True]
    assert [item["version"] for item in history["attachment_versions"]] == [1, 2]
    assert [item["current"] for item in history["attachment_versions"]] == [
        False,
        True,
    ]


@pytest.mark.parametrize(
    "expires_at",
    [
        "not-a-number",
        float("nan"),
        float("inf"),
        True,
        1.0,
    ],
)
def test_integrator_projection_fails_closed_on_malformed_or_expired_expiry(
    tmp_path: Path,
    expires_at: object,
) -> None:
    service, application_id, _, request, source = _ready_supplement_request(tmp_path)
    principal = S01CommandPrincipal(
        subject="s06-integrator",
        role="integrator",
        scope="R-OBSERVED/c-demo",
        source_id="s06-material-source",
        expires_at=expires_at,  # type: ignore[arg-type]
    )
    with pytest.raises(QueryNotFound):
        service.integrator_supplement_request_view(
            principal=principal,
            request_id=request["request_id"],
            now=102,
        )
    with pytest.raises(QueryNotFound):
        service.integrator_supplement_request_view(
            principal=principal,
            request_id="supplement_request_missing00000000000000000000000",
            now=102,
        )
    live = S01CommandPrincipal(
        subject="s06-integrator",
        role="integrator",
        scope="R-OBSERVED/c-demo",
        source_id="s06-material-source",
        expires_at=10_000.0,
    )
    projection = service.integrator_supplement_request_view(
        principal=live,
        request_id=request["request_id"],
        now=102,
    )
    assert projection["status"] == "open"


def test_reviewer_and_integrator_derivations_follow_the_same_transition(
    tmp_path: Path,
) -> None:
    service, application_id, _, request, source = _ready_supplement_request(tmp_path)
    request_id = request["request_id"]

    def reviewer_view(now: int) -> dict[str, object]:
        return service.supplement_request_view(
            principal=REVIEWER,
            request_id=request_id,
            now=now,
        )

    def integrator_view(now: int) -> dict[str, object]:
        return service.integrator_supplement_request_view(
            principal=SUPPLEMENT_INTEGRATOR,
            request_id=request_id,
            now=now,
        )

    open_reviewer = reviewer_view(102)
    open_integrator = integrator_view(102)
    assert open_reviewer["status"] == open_integrator["status"] == "open"
    assert open_reviewer["current"] is True
    assert open_integrator["current"] is True

    service.submit_attachment_version(
        submission=_attachment_submission(request, source, closed=False),
        idempotency_key="s06-t04-parity-progress",
        principal=SUPPLEMENT_INTEGRATOR,
        now=200,
    )
    progress_reviewer = reviewer_view(201)
    progress_integrator = integrator_view(201)
    assert progress_reviewer["status"] == progress_integrator["status"] == "open"
    assert progress_reviewer["current"] is True
    assert progress_integrator["current"] is True

    service.submit_attachment_version(
        submission=_attachment_submission(request, source, closed=True),
        idempotency_key="s06-t04-parity-closure",
        principal=SUPPLEMENT_INTEGRATOR,
        now=202,
    )
    terminal_reviewer = reviewer_view(203)
    terminal_integrator = integrator_view(203)
    assert terminal_reviewer["status"] == terminal_integrator["status"] == "fulfilled"
    assert terminal_reviewer["current"] is False
    assert terminal_integrator["current"] is False

    # The Integrator projection never exposes Reviewer/application/run data
    # at any transition point.
    for projection in (open_integrator, progress_integrator, terminal_integrator):
        assert INTEGRATOR_INTERNAL_KEYS.isdisjoint(projection)
