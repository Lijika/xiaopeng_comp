"""Production FastAPI fixture for the Ticket #49 T15 browser tracer.

The factory drives the released manual, correction, supplement, exception,
and recovery service seams into Verification Completed in one SQLite
authority.  The browser then reads those immutable facts through the real
S13 HTTP adapter and the shared production React build.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from task4_consistency.controlled.s01 import (
    ControlledScenarioService,
    ControlledScenarioTestDriver,
    S01CommandPrincipal,
)

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "configs" / "rules_auto_lease.yaml"
FIXTURES = ROOT / "fixtures" / "applications"

S13_CREDENTIAL = "t15-s13-operator-credential"
S13_SUBJECT = "t15-s13-operator"


def _service(
    work_root: Path,
    scenario_id: str,
    **kwargs: Any,
) -> ControlledScenarioService:
    return ControlledScenarioService(
        fixture_root=FIXTURES,
        rules_path=RULES,
        state_path=work_root / "target.sqlite3",
        scenario_id=scenario_id,
        **kwargs,
    )


def _complete_review(
    service: ControlledScenarioService,
    *,
    application_id: str,
    reviewer: S01CommandPrincipal,
    idempotency_key: str,
    now: int,
) -> None:
    service.refresh_projection()
    work_item = next(
        item
        for item in service.queue_view(
            role="reviewer",
            scope=reviewer.scope,
            subject=reviewer.subject,
            now=now,
        )["items"]
        if item["application_id"] == application_id
    )
    view = service.review_work_item_view(
        principal=reviewer,
        work_item_id=work_item["work_item_id"],
        now=now,
    )
    claim = service.claim_review_work_item(
        principal=reviewer,
        work_item_id=work_item["work_item_id"],
        expected_context=view["command_context"],
        now=now,
    )
    result = service.submit_review_work_item(
        principal=reviewer,
        work_item_id=work_item["work_item_id"],
        expected_fence=claim["claim_fence"],
        expected_context=view["command_context"],
        idempotency_key=idempotency_key,
        verification={
            "schema_version": "human-decision/1",
            "outcome": "confirmed",
            "reason_code": "HUMAN_REVIEW_COMPLETED",
            "finding_decisions": [
                {
                    "finding_id": finding["finding_id"],
                    "outcome": (
                        "confirmed"
                        if finding["verdict"] == "uncertain"
                        else "inconclusive"
                    ),
                }
                for finding in view["automatic_findings"]
            ],
        },
        now=now + 1,
    )
    assert result["status"] == "accepted"


def _recovery_verifier(work: dict[str, Any]) -> dict[str, Any]:
    return {
        "verification_id": "t15-recovery-fact",
        "observed_at": int(work["opened_at"]) + 1,
        "evidence_kind": work["criterion"]["evidence_kind"],
        "scope": work["visibility_scope"],
        "recovery_work_id": work["recovery_work_id"],
        "criterion_digest": work["criterion"]["digest"],
        "conditions": [
            {
                "condition_id": condition["condition_id"],
                "verified": True,
                "evidence_digest": "a" * 64,
            }
            for condition in work["conditions"]
        ],
    }


def _build_workflows(work_root: Path) -> ControlledScenarioService:
    from tests.test_s04_controlled import (
        REVIEWER as CORRECTION_REVIEWER,
        _ready_engine_correction,
    )
    from tests.test_s05_controlled import (
        ROUTER as EXCEPTION_ROUTER,
        _approve_brand_exception,
        _ready_brand_exception,
        _request_brand_exception,
    )
    from tests.test_s06_controlled import (
        REVIEWER as SUPPLEMENT_REVIEWER,
        SUPPLEMENT_INTEGRATOR,
        _attachment_submission,
        _ready_supplement_request,
    )
    from tests.test_s07_controlled import (
        INTAKE as RECOVERY_INTAKE,
        OPERATOR as RECOVERY_OPERATOR,
        REVIEWER as RECOVERY_REVIEWER,
    )
    from tests.test_s13_controlled import (
        TEST_INTEGRATOR,
        TEST_OPERATOR,
        TEST_REVIEWER,
    )
    from task4_consistency.controlled.s13 import (
        InMemoryDownstreamAdapter,
        build_c_demo_registry,
    )

    applications: dict[str, str] = {}

    correction, application_id, work_id, claim, command = (
        _ready_engine_correction(work_root)
    )
    view = correction.review_work_item_view(
        principal=CORRECTION_REVIEWER,
        work_item_id=work_id,
        now=100,
    )
    accepted = correction.correct_field_observation(
        principal=CORRECTION_REVIEWER,
        application_id=application_id,
        work_item_id=work_id,
        expected_fence=claim["claim_fence"],
        expected_context=view["command_context"],
        idempotency_key="t15-correction",
        correction=command,
        now=101,
    )
    assert accepted["status"] == "accepted"
    assert correction.process_next_job().status == "complete"
    applications["correction"] = application_id

    manual = _service(work_root, "app_uncertain_ocr_noise.json")
    admission = manual.submit_demo(
        scenario_id="app_uncertain_ocr_noise.json",
        idempotency_key="t15-manual-intake",
        principal=TEST_INTEGRATOR,
    )
    assert admission.application_id is not None
    assert manual.process_next_job().status == "complete"
    applications["manual"] = str(admission.application_id)
    _complete_review(
        manual,
        application_id=applications["manual"],
        reviewer=TEST_REVIEWER,
        idempotency_key="t15-manual-submit",
        now=110,
    )

    supplement, application_id, _, request, source = _ready_supplement_request(
        work_root
    )
    progress = supplement.submit_attachment_version(
        submission=_attachment_submission(request, source, closed=False),
        idempotency_key="t15-supplement-progress",
        principal=SUPPLEMENT_INTEGRATOR,
        now=200,
    )
    fulfilled = supplement.submit_attachment_version(
        submission=_attachment_submission(request, source, closed=True),
        idempotency_key="t15-supplement-closure",
        principal=SUPPLEMENT_INTEGRATOR,
        now=201,
    )
    assert progress.reason_code == "request_progress_accepted"
    assert fulfilled.reason_code == "request_fulfilled"
    assert supplement.process_next_job().status == "complete"
    applications["supplement"] = application_id
    _complete_review(
        supplement,
        application_id=application_id,
        reviewer=SUPPLEMENT_REVIEWER,
        idempotency_key="t15-supplement-complete",
        now=210,
    )

    exception, application_id, work_id, claim, finding = _ready_brand_exception(
        work_root
    )
    request = _request_brand_exception(
        exception,
        work_id,
        claim,
        finding,
        key="t15-exception-request",
    )
    decision = _approve_brand_exception(exception, request, now=103)
    routed = exception.determine_business_exception_route(
        principal=EXCEPTION_ROUTER,
        request_id=str(request["request_id"]),
        expected_context=decision["routing_context"],
        idempotency_key="t15-exception-route",
        now=104,
    )
    assert routed["phase"] == "Verification Completed"
    applications["exception"] = application_id

    recovery_transport = InMemoryDownstreamAdapter(
        adapter_id="c-demo-inmemory-transport",
        compensation_behavior="fail",
    )
    recovery = _service(
        work_root,
        "app_inconsistent_vin.json",
        recovery_verifier=_recovery_verifier,
        downstream_registry=build_c_demo_registry(
            extra_adapters={"c-demo-inmemory-transport": recovery_transport}
        ),
    )
    admission = recovery.submit_demo(
        scenario_id="app_inconsistent_vin.json",
        idempotency_key="t15-recovery-intake",
        principal=RECOVERY_INTAKE,
    )
    assert admission.application_id is not None
    applications["recovery"] = str(admission.application_id)
    blocked = ControlledScenarioTestDriver(recovery).process_next_job(
        worker_id="t15-recovery-worker",
        now=10,
        operation_fault="checker_incompatible",
    )
    assert blocked.recovery_work_id is not None
    recovery_view = recovery.recovery_work_view(
        principal=RECOVERY_OPERATOR,
        recovery_work_id=blocked.recovery_work_id,
    )
    verified = recovery.verify_recovery(
        principal=RECOVERY_OPERATOR,
        recovery_work_id=blocked.recovery_work_id,
        expected_lifecycle_revision=recovery_view["lifecycle_revision"],
        expected_criterion_digest=recovery_view["criterion"]["digest"],
        idempotency_key="t15-recovery-verify",
    )
    assert verified["phase"] == "Evidence Ready"
    assert (
        ControlledScenarioTestDriver(recovery)
        .process_next_job(worker_id="t15-recovery-rerun", now=12)
        .status
        == "complete"
    )
    _complete_review(
        recovery,
        application_id=applications["recovery"],
        reviewer=RECOVERY_REVIEWER,
        idempotency_key="t15-recovery-complete",
        now=20,
    )

    sent = recovery.process_next_delivery(principal=TEST_OPERATOR)
    assert sent["status"] == "received"
    failed = recovery.compensate_delivery(
        principal=TEST_OPERATOR,
        obligation_id=str(sent["obligation_id"]),
    )
    assert failed["status"] == "failed"
    received = recovery.process_next_delivery(principal=TEST_OPERATOR)
    assert received["status"] == "received"

    fixture = []
    for workflow in ("manual", "correction", "supplement", "exception", "recovery"):
        delivery = recovery.delivery_view(
            principal=TEST_OPERATOR,
            application_id=applications[workflow],
        )
        assert delivery["phase"] == "Verification Completed"
        assert delivery["verification_completed"] is True
        assert delivery["obligation"] is not None
        assert len(delivery["routing_history"]) == 1
        fixture.append(
            {
                "workflow": workflow,
                "application_id": applications[workflow],
                "route": delivery["route"],
                "attribution_kind": delivery["obligation"]["attribution_kind"],
                "delivery_status": delivery["delivery_status"],
                "obligation_id": delivery["obligation"]["obligation_id"],
                "operation_id": delivery["obligation"]["operation_id"],
            }
        )
    assert {entry["delivery_status"] for entry in fixture} == {
        "pending",
        "received",
        "compensation_failed",
    }
    (work_root / "fixture.json").write_text(
        json.dumps(
            {"schema_version": "t15-browser-fixture/1", "applications": fixture},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return recovery


def create_t15_react_test_app():
    """Return the real FastAPI app bound to one persisted T15 authority."""
    import task4_consistency.web.app as web

    work_root = Path(os.environ["TASK4_T15_FIXTURE_ROOT"])
    work_root.mkdir(parents=True, exist_ok=True)
    if (work_root / "fixture.json").is_file() and (
        work_root / "target.sqlite3"
    ).is_file():
        service = _service(work_root, "app_inconsistent_vin.json")
    else:
        service = _build_workflows(work_root)

    web.S01_BACKGROUND_ENABLED = False
    web.S01_REQUIRE_CONFIGURED_STARTUP = False
    web.S01_SERVICE = service
    web.S13_OPERATOR_CREDENTIAL = S13_CREDENTIAL
    web.S13_OPERATOR_SUBJECT = S13_SUBJECT
    web.S13_OPERATOR_SCOPE = "C-DEMO"
    react_dir = os.environ.get("TASK4_T15_REACT_DIR", "").strip()
    web.S01_REACT_INDEX = (
        Path(react_dir).resolve() / "index.html"
        if react_dir
        else web.S01_REACT_STATIC / "index.html"
    )
    return web.app
