from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import threading

import pytest

from task4_consistency.controlled.s01 import (
    AdmissionDisposition,
    ControlledScenarioService,
    QueryNotFound,
    S01CommandPrincipal,
)
from task4_consistency.controlled.s01_checker import TargetRelease
from task4_consistency.kb.store import get_kb
from task4_consistency.models import Application, Verdict
from task4_consistency.rules.engine import RuleEngine
from task4_consistency.rules.loader import load_rules


ROOT = Path(__file__).resolve().parents[1]
REVIEWER = S01CommandPrincipal(
    subject="s05-reviewer",
    role="reviewer",
    scope="C-DEMO",
    source_id="s05-review-console",
)
INTEGRATOR = S01CommandPrincipal(
    subject=REVIEWER.subject,
    role="integrator",
    scope=REVIEWER.scope,
    source_id="s05-intake",
)
APPROVER = S01CommandPrincipal(
    subject="s05-exception-approver",
    role="exception_approver",
    scope="C-DEMO",
    source_id="s05-approver-console",
)
ROUTER = S01CommandPrincipal(
    subject="s05-router",
    role="operator",
    scope="C-DEMO",
    source_id="s05-lifecycle-router",
)


def _authority_snapshot(service: ControlledScenarioService) -> dict[str, object]:
    service._reload_store()
    return {
        name: copy.deepcopy(getattr(service._store, name))
        for name in (
            "applications",
            "receipts",
            "lifecycle_events",
            "evidence_events",
            "audit_events",
            "jobs",
            "idempotency",
            "attempts",
            "runs",
            "findings",
            "work_items",
            "review_records",
            "outbox",
            "projections",
        )
    }


def _faulty_service(
    service: ControlledScenarioService,
    state_path: Path,
    fault_point: str,
) -> ControlledScenarioService:
    def fail(write_point: str) -> None:
        if write_point == fault_point:
            raise OSError(f"injected {fault_point}")

    return ControlledScenarioService(
        fixture_root=service.fixture_root,
        rules_path=service.rules_path,
        state_path=state_path,
        scenario_id="app_bad_brand.json",
        exception_approver_subject=APPROVER.subject,
        fault_injector=fail,
    )


def _ready_brand_exception(
    tmp_path: Path,
) -> tuple[ControlledScenarioService, str, str, dict[str, object], dict[str, object]]:
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "target.sqlite3",
        scenario_id="app_bad_brand.json",
        exception_approver_subject=APPROVER.subject,
    )
    admitted = service.submit_demo(
        scenario_id="app_bad_brand.json",
        idempotency_key="s05-brand-intake",
        principal=INTEGRATOR,
    )
    completed = service.process_next_job()
    service.refresh_projection()

    assert admitted.disposition is AdmissionDisposition.ACCEPTED
    assert admitted.application_id is not None
    assert completed.status == "complete"
    queue = service.queue_view(
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=100,
    )
    assert len(queue["items"]) == 1
    work_item_id = queue["items"][0]["work_item_id"]
    work_item = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=100,
    )
    finding = next(
        item
        for item in work_item["automatic_findings"]
        if item["rule_id"] == "R_BRAND_CROSS"
    )
    claimed = service.claim_review_work_item(
        principal=REVIEWER,
        work_item_id=work_item_id,
        expected_context=work_item["command_context"],
        now=100,
    )
    return service, admitted.application_id, work_item_id, claimed, finding


def _ready_scenario_finding(
    tmp_path: Path,
    *,
    scenario_id: str,
    rule_id: str,
    fixture_root: Path | None = None,
) -> tuple[ControlledScenarioService, str, str, dict[str, object], dict[str, object]]:
    service = ControlledScenarioService(
        fixture_root=fixture_root or ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / f"{scenario_id}.sqlite3",
        scenario_id=scenario_id,
        exception_approver_subject=APPROVER.subject,
    )
    admitted = service.submit_demo(
        scenario_id=scenario_id,
        idempotency_key=f"s05-{scenario_id}-intake",
        principal=INTEGRATOR,
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
    view = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=100,
    )
    finding = next(
        item for item in view["automatic_findings"] if item["rule_id"] == rule_id
    )
    claimed = service.claim_review_work_item(
        principal=REVIEWER,
        work_item_id=work_item_id,
        expected_context=view["command_context"],
        now=100,
    )
    return service, admitted.application_id, work_item_id, claimed, finding


def _request_brand_exception(
    service: ControlledScenarioService,
    work_item_id: str,
    claim: dict[str, object],
    finding: dict[str, object],
    *,
    key: str = "s05-brand-request",
    now: int = 101,
) -> dict[str, object]:
    view = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=100,
    )
    return service.request_business_exception(
        principal=REVIEWER,
        work_item_id=work_item_id,
        finding_id=str(finding["finding_id"]),
        reason_code="DOCUMENTED_BRAND_VARIANCE",
        expected_fence=int(claim["claim_fence"]),
        expected_context=view["command_context"],
        idempotency_key=key,
        now=now,
    )


def _approve_brand_exception(
    service: ControlledScenarioService,
    request: dict[str, object],
    *,
    now: int = 103,
) -> dict[str, object]:
    view = service.business_exception_view(
        principal=APPROVER,
        request_id=str(request["request_id"]),
        now=now - 1,
    )
    claim = service.claim_exception_work_item(
        principal=APPROVER,
        work_item_id=str(request["work_item_id"]),
        expected_context=view["command_context"],
        now=now - 1,
    )
    return service.decide_business_exception(
        principal=APPROVER,
        request_id=str(request["request_id"]),
        work_item_id=str(request["work_item_id"]),
        decision="approved",
        reason_code="DOCUMENTED_VARIANCE_ACCEPTED",
        expected_fence=int(claim["claim_fence"]),
        expected_context=view["command_context"],
        idempotency_key=f"approve-{request['request_id']}",
        now=now,
    )


def _ready_approved_exception_with_extra_blocker(
    tmp_path: Path,
) -> tuple[
    ControlledScenarioService,
    str,
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    fixture = json.loads(
        (ROOT / "fixtures" / "applications" / "app_bad_brand.json").read_text(
            encoding="utf-8"
        )
    )
    invoice = next(
        document for document in fixture["documents"] if document["doc_id"] == "inv"
    )
    invoice["fields"]["engine_no"]["raw"] = "ENG-DIFFERENT"
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir(parents=True)
    (fixture_root / "app_bad_brand.json").write_text(
        json.dumps(fixture, ensure_ascii=False),
        encoding="utf-8",
    )
    service = ControlledScenarioService(
        fixture_root=fixture_root,
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "partial-route.sqlite3",
        scenario_id="app_bad_brand.json",
        exception_approver_subject=APPROVER.subject,
    )
    admitted = service.submit_demo(
        scenario_id="app_bad_brand.json",
        idempotency_key="s05-partial-intake",
        principal=INTEGRATOR,
    )
    assert admitted.application_id is not None
    assert service.process_next_job().status == "complete"
    service.refresh_projection()
    queue = service.queue_view(
        role="reviewer", scope="C-DEMO", subject=REVIEWER.subject, now=100
    )
    old_work_id = queue["items"][0]["work_item_id"]
    old_view = service.review_work_item_view(
        principal=REVIEWER, work_item_id=old_work_id, now=100
    )
    brand = next(
        finding
        for finding in old_view["automatic_findings"]
        if finding["rule_id"] == "R_BRAND_CROSS"
    )
    old_claim = service.claim_review_work_item(
        principal=REVIEWER,
        work_item_id=old_work_id,
        expected_context=old_view["command_context"],
        now=100,
    )
    request = _request_brand_exception(service, old_work_id, old_claim, brand)
    decision = _approve_brand_exception(service, request)
    return service, str(admitted.application_id), request, decision, brand


def _ready_partial_routed_exception(
    tmp_path: Path,
) -> tuple[
    ControlledScenarioService,
    str,
    dict[str, object],
    dict[str, object],
    dict[str, object],
    str,
]:
    service, application_id, request, decision, brand = (
        _ready_approved_exception_with_extra_blocker(tmp_path)
    )
    routed = service.determine_business_exception_route(
        principal=ROUTER,
        request_id=str(request["request_id"]),
        expected_context=decision["routing_context"],
        idempotency_key="s05-partial-route",
        now=104,
    )
    assert routed["phase"] == "Manual Review"
    return (
        service,
        application_id,
        request,
        decision,
        brand,
        str(routed["successor_work_item_id"]),
    )


def test_release_freezes_exact_default_deny_waiver_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    rules_path = ROOT / "configs" / "rules_auto_lease.yaml"
    config = load_rules(rules_path)
    release = TargetRelease.compile(
        config,
        "f" * 64,
        knowledge=get_kb().to_dict(),
    )
    by_id = {rule.rule_id: rule for rule in release.rules}

    assert {
        rule_id for rule_id, rule in by_id.items() if rule.waivable
    } == {"R_BRAND_CROSS"}
    assert by_id["R_BRAND_CROSS"].waiver_policy_id == "c-demo-brand-exception/1"
    assert by_id["R_BRAND_CROSS"].waiver_reasons == (
        "DOCUMENTED_BRAND_VARIANCE",
    )
    assert by_id["R_BRAND_CROSS"].waiver_scope == (
        "one_application_cycle_run_finding"
    )
    assert by_id["R_BRAND_CROSS"].waiver_ttl_seconds == 900
    assert all(
        by_id[rule_id].waivable is False
        for rule_id in ("R_VIN_CROSS", "R_ENGINE_CROSS", "R_ID_EXACT")
    )
    manifest = release.public_manifest()
    assert manifest["waiver_policy_digest"] == release.waiver_policy_digest

    from task4_consistency.controlled import s01_checker

    monkeypatch.setattr(
        s01_checker,
        "_PROTECTED_WAIVER_CHECKS",
        frozenset({"R_BRAND_CROSS"}),
    )
    with pytest.raises(ValueError, match="protected-baseline"):
        TargetRelease.compile(config, "f" * 64, knowledge=get_kb().to_dict())


@pytest.mark.parametrize(
    ("scenario_id", "rule_id", "reason_code"),
    (
        (
            "app_inconsistent_vin.json",
            "R_VIN_CROSS",
            "FINDING_NOT_EXCEPTION_ELIGIBLE",
        ),
        (
            "app_s04_bad_vin.json",
            "R_VIN_CROSS",
            "PROTECTED_CHECK_NOT_WAIVABLE",
        ),
        (
            "app_bad_model.json",
            "R_MODEL_CROSS",
            "CHECK_NOT_WAIVABLE_BY_PINNED_RELEASE",
        ),
        (
            "app_uncertain_ocr_noise.json",
            "R_VIN_CROSS",
            "FINDING_NOT_EXCEPTION_ELIGIBLE",
        ),
    ),
)
def test_exception_entry_is_exact_and_default_deny_without_business_effect(
    tmp_path: Path,
    scenario_id: str,
    rule_id: str,
    reason_code: str,
) -> None:
    service, _, work_item_id, claim, finding = _ready_scenario_finding(
        tmp_path,
        scenario_id=scenario_id,
        rule_id=rule_id,
    )
    before = copy.deepcopy(service._store)
    view = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=100,
    )

    denied = service.request_business_exception(
        principal=REVIEWER,
        work_item_id=work_item_id,
        finding_id=str(finding["finding_id"]),
        reason_code="DOCUMENTED_BRAND_VARIANCE",
        expected_fence=int(claim["claim_fence"]),
        expected_context=view["command_context"],
        idempotency_key=f"deny-{scenario_id}",
        now=101,
    )
    service._reload_store()

    assert denied["status"] == "rejected"
    assert denied["reason_code"] == reason_code
    assert service._store.applications == before.applications
    assert service._store.lifecycle_events == before.lifecycle_events
    assert service._store.work_items == before.work_items
    assert service._store.review_records == before.review_records
    assert service._store.audit_events == before.audit_events
    assert service._store.idempotency == before.idempotency


def test_missing_skipped_brand_finding_is_not_exception_eligible(
    tmp_path: Path,
) -> None:
    fixture = json.loads(
        (ROOT / "fixtures" / "applications" / "app_bad_model.json").read_text(
            encoding="utf-8"
        )
    )
    for document in fixture["documents"]:
        document["fields"].pop("brand", None)
    fixture_root = tmp_path / "missing-brand"
    fixture_root.mkdir()
    (fixture_root / "app_bad_model.json").write_text(
        json.dumps(fixture, ensure_ascii=False), encoding="utf-8"
    )
    service, _, work_item_id, claim, finding = _ready_scenario_finding(
        tmp_path,
        scenario_id="app_bad_model.json",
        rule_id="R_BRAND_CROSS",
        fixture_root=fixture_root,
    )
    work = service.review_work_item_view(
        principal=REVIEWER, work_item_id=work_item_id, now=100
    )
    before = _authority_snapshot(service)

    denied = service.request_business_exception(
        principal=REVIEWER,
        work_item_id=work_item_id,
        finding_id=str(finding["finding_id"]),
        reason_code="DOCUMENTED_BRAND_VARIANCE",
        expected_fence=int(claim["claim_fence"]),
        expected_context=work["command_context"],
        idempotency_key="s05-missing-brand-request",
        now=101,
    )

    assert finding["verdict"] == "skipped"
    assert finding["reason_code"] in {"MISSING_FIELD", "SKIPPED"}
    assert denied["status"] == "rejected"
    assert denied["reason_code"] == "FINDING_NOT_EXCEPTION_ELIGIBLE"
    assert _authority_snapshot(service) == before


def test_legacy_intent_is_only_a_differential_oracle(
    tmp_path: Path,
) -> None:
    scenario_id = "app_bad_brand.json"
    rule_id = "R_BRAND_CROSS"
    fixture_path = ROOT / "fixtures" / "applications" / scenario_id
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    legacy = RuleEngine(load_rules(ROOT / "configs" / "rules_auto_lease.yaml"))
    legacy_result = next(
        check
        for check in legacy.run(Application.from_dict(payload)).checks
        if check.rule_id == rule_id
    )
    _, _, _, _, target_finding = _ready_scenario_finding(
        tmp_path,
        scenario_id=scenario_id,
        rule_id=rule_id,
    )

    assert legacy_result.verdict is Verdict.INCONSISTENT
    assert target_finding["verdict"] == legacy_result.verdict.value


def test_request_is_bound_to_live_claim_unique_and_semantically_idempotent(
    tmp_path: Path,
) -> None:
    service, _, work_item_id, claim, finding = _ready_brand_exception(tmp_path)
    initial = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=100,
    )
    before = service.fact_counts()
    stale = service.request_business_exception(
        principal=REVIEWER,
        work_item_id=work_item_id,
        finding_id=str(finding["finding_id"]),
        reason_code="DOCUMENTED_BRAND_VARIANCE",
        expected_fence=int(claim["claim_fence"]) + 1,
        expected_context=initial["command_context"],
        idempotency_key="s05-stale-fence",
        now=101,
    )
    accepted = _request_brand_exception(
        service,
        work_item_id,
        claim,
        finding,
        key="s05-unique-request",
    )
    replay = service.request_business_exception(
        principal=REVIEWER,
        work_item_id=work_item_id,
        finding_id=str(finding["finding_id"]),
        reason_code="DOCUMENTED_BRAND_VARIANCE",
        expected_fence=int(claim["claim_fence"]),
        expected_context=initial["command_context"],
        idempotency_key="s05-unique-request",
        now=102,
    )
    duplicate = service.request_business_exception(
        principal=REVIEWER,
        work_item_id=work_item_id,
        finding_id=str(finding["finding_id"]),
        reason_code="DOCUMENTED_BRAND_VARIANCE",
        expected_fence=int(claim["claim_fence"]),
        expected_context=initial["command_context"],
        idempotency_key="s05-competing-request",
        now=102,
    )

    assert stale["status"] == "stale"
    assert service.fact_counts()["findings"] == before["findings"]
    assert replay == {**accepted, "replayed": True}
    assert duplicate["status"] in {"conflict", "stale"}
    service._reload_store()
    requests = [
        record
        for record in service._store.review_records
        if record.get("record_type") == "business_exception_request"
    ]
    approval_work = [
        item
        for item in service._store.work_items
        if item.get("kind") == "exception_approval"
    ]
    request_record = requests[0]
    digest_payload = copy.deepcopy(request_record)
    context_digest = digest_payload.pop("context_digest")
    assert len(requests) == len(approval_work) == 1
    assert request_record["visibility_scope"] == "C-DEMO"
    assert request_record["cycle"] == 1
    assert request_record["verdict"] == "inconsistent"
    assert request_record["requester_subject"] == REVIEWER.subject
    assert request_record["requester_claim_fence"] == claim["claim_fence"]
    assert request_record["predecessor_request_id"] is None
    assert request_record["reason_code"] == "DOCUMENTED_BRAND_VARIANCE"
    assert request_record["scope"] == "one_application_cycle_run_finding"
    assert request_record["expires_at"] - request_record["requested_at"] == 900
    assert request_record["fixed_context"] == initial["command_context"]
    assert request_record["post_request_lifecycle_revision"] == (
        request_record["pre_request_lifecycle_revision"] + 1
    )
    assert context_digest == hashlib.sha256(
        json.dumps(
            digest_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


@pytest.mark.parametrize("terminal_status", ("rejected", "expired", "invalidated"))
def test_inactive_exception_rerequest_requires_latest_predecessor_and_new_context(
    tmp_path: Path,
    terminal_status: str,
) -> None:
    service, application_id, work_item_id, claim, finding = _ready_brand_exception(
        tmp_path
    )
    first = _request_brand_exception(service, work_item_id, claim, finding)
    exception_view = service.business_exception_view(
        principal=APPROVER,
        request_id=str(first["request_id"]),
        now=102,
    )
    if terminal_status == "rejected":
        approver_claim = service.claim_exception_work_item(
            principal=APPROVER,
            work_item_id=str(first["work_item_id"]),
            expected_context=exception_view["command_context"],
            now=102,
        )
        terminal = service.decide_business_exception(
            principal=APPROVER,
            request_id=str(first["request_id"]),
            work_item_id=str(first["work_item_id"]),
            decision="rejected",
            reason_code="DOCUMENTED_VARIANCE_REJECTED",
            expected_fence=int(approver_claim["claim_fence"]),
            expected_context=exception_view["command_context"],
            idempotency_key="s05-rerequest-reject",
            now=103,
        )
        review_time = 104
    elif terminal_status == "expired":
        terminal = service.expire_business_exception(
            principal=ROUTER,
            request_id=str(first["request_id"]),
            expected_context=exception_view["command_context"],
            idempotency_key="s05-rerequest-expire",
            now=int(first["expires_at"]),
        )
        review_time = int(first["expires_at"]) + 1
    else:
        terminal = service.invalidate_business_exception(
            principal=ROUTER,
            request_id=str(first["request_id"]),
            reason_code="POLICY_REVOKED",
            expected_context=exception_view["command_context"],
            idempotency_key="s05-rerequest-invalidate",
            now=103,
        )
        review_time = 104

    assert terminal["status"] == "accepted"
    service.refresh_projection()
    successor_work_item_id = str(terminal["successor_work_item_id"])
    successor = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=successor_work_item_id,
        now=review_time,
    )
    workspace = service.workspace_view(
        application_id,
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=review_time,
    )
    successor_brand = next(
        item
        for item in workspace["mandatory_blockers"]
        if item["rule_id"] == "R_BRAND_CROSS"
    )
    successor_claim = service.claim_review_work_item(
        principal=REVIEWER,
        work_item_id=successor_work_item_id,
        expected_context=successor["command_context"],
        now=review_time,
    )
    before_same_context = _authority_snapshot(service)
    same_context = service.request_business_exception(
        principal=REVIEWER,
        work_item_id=successor_work_item_id,
        finding_id=str(successor_brand["finding_id"]),
        reason_code="DOCUMENTED_BRAND_VARIANCE",
        predecessor_request_id=str(first["request_id"]),
        expected_fence=int(successor_claim["claim_fence"]),
        expected_context=successor["command_context"],
        idempotency_key=f"s05-rerequest-same-context-{terminal_status}",
        now=review_time + 1,
    )

    assert same_context["status"] == "conflict"
    assert same_context["reason_code"] == "EXCEPTION_REREQUEST_NOT_MATERIAL"
    assert _authority_snapshot(service) == before_same_context

    source = next(
        link
        for link in successor_brand["evidence_links"]
        if link["document_id"] == "pol"
    )
    corrected = service.correct_field_observation(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=successor_work_item_id,
        expected_fence=int(successor_claim["claim_fence"]),
        expected_context=successor["command_context"],
        idempotency_key=f"s05-rerequest-new-run-{terminal_status}",
        correction={
            "schema_version": "field-observation-correction/1",
            "finding_id": successor_brand["finding_id"],
            "observation_id": source["observation_id"],
            "document_id": source["document_id"],
            "document_role": source["document_role"],
            "field": source["field"],
            "raw": "HONDA",
            "source_location": {
                key: source[key]
                for key in ("source_sha256", "source_page", "source_region")
            },
            "reason_code": "SOURCE_VALUE_MISREAD",
        },
        now=review_time + 2,
    )
    assert corrected["status"] == "accepted"
    assert service.process_next_job().status == "complete"
    service.refresh_projection()
    queue = service.queue_view(
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=review_time + 3,
    )
    assert len(queue["items"]) == 1
    new_work_item_id = str(queue["items"][0]["work_item_id"])
    new_work = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=new_work_item_id,
        now=review_time + 3,
    )
    new_brand = next(
        item
        for item in new_work["automatic_findings"]
        if item["rule_id"] == "R_BRAND_CROSS"
    )
    new_claim = service.claim_review_work_item(
        principal=REVIEWER,
        work_item_id=new_work_item_id,
        expected_context=new_work["command_context"],
        now=review_time + 3,
    )
    assert new_work["run_authority"]["run_id"] != successor["run_authority"]["run_id"]
    assert new_brand["verdict"] == "inconsistent"

    before_predecessor_denials = _authority_snapshot(service)
    missing = service.request_business_exception(
        principal=REVIEWER,
        work_item_id=new_work_item_id,
        finding_id=str(new_brand["finding_id"]),
        reason_code="DOCUMENTED_BRAND_VARIANCE",
        expected_fence=int(new_claim["claim_fence"]),
        expected_context=new_work["command_context"],
        idempotency_key=f"s05-rerequest-missing-{terminal_status}",
        now=review_time + 4,
    )
    assert missing["status"] == "conflict"
    assert missing["reason_code"] == "EXCEPTION_PREDECESSOR_REQUIRED"
    assert _authority_snapshot(service) == before_predecessor_denials

    wrong = service.request_business_exception(
        principal=REVIEWER,
        work_item_id=new_work_item_id,
        finding_id=str(new_brand["finding_id"]),
        reason_code="DOCUMENTED_BRAND_VARIANCE",
        predecessor_request_id=f"{first['request_id']}-wrong",
        expected_fence=int(new_claim["claim_fence"]),
        expected_context=new_work["command_context"],
        idempotency_key=f"s05-rerequest-wrong-{terminal_status}",
        now=review_time + 4,
    )
    assert wrong["status"] == "conflict"
    assert wrong["reason_code"] == "EXCEPTION_PREDECESSOR_MISMATCH"
    assert _authority_snapshot(service) == before_predecessor_denials

    accepted = service.request_business_exception(
        principal=REVIEWER,
        work_item_id=new_work_item_id,
        finding_id=str(new_brand["finding_id"]),
        reason_code="DOCUMENTED_BRAND_VARIANCE",
        predecessor_request_id=str(first["request_id"]),
        expected_fence=int(new_claim["claim_fence"]),
        expected_context=new_work["command_context"],
        idempotency_key=f"s05-rerequest-accepted-{terminal_status}",
        now=review_time + 4,
    )

    assert accepted["status"] == "accepted"
    service._reload_store()
    requests = [
        record
        for record in service._store.review_records
        if record.get("record_type") == "business_exception_request"
    ]
    assert len(requests) == 2
    assert requests[-1]["predecessor_request_id"] == first["request_id"]


def test_subject_level_sod_and_first_decision_winner_have_zero_second_effect(
    tmp_path: Path,
) -> None:
    service, _, work_item_id, claim, finding = _ready_brand_exception(
        tmp_path / "sod"
    )
    service._exception_approver_subject = REVIEWER.subject
    request = _request_brand_exception(service, work_item_id, claim, finding)
    self_approver = S01CommandPrincipal(
        subject=REVIEWER.subject,
        role="exception_approver",
        scope=APPROVER.scope,
        source_id="different-session-and-source",
    )
    view = service.business_exception_view(
        principal=self_approver,
        request_id=str(request["request_id"]),
        now=102,
    )
    approver_claim = service.claim_exception_work_item(
        principal=self_approver,
        work_item_id=str(request["work_item_id"]),
        expected_context=view["command_context"],
        now=102,
    )
    before = copy.deepcopy(service._store)
    denied = service.decide_business_exception(
        principal=self_approver,
        request_id=str(request["request_id"]),
        work_item_id=str(request["work_item_id"]),
        decision="approved",
        reason_code="DOCUMENTED_VARIANCE_ACCEPTED",
        expected_fence=int(approver_claim["claim_fence"]),
        expected_context=view["command_context"],
        idempotency_key="s05-self-approval",
        now=103,
    )
    assert denied["status"] == "rejected"
    assert denied["reason_code"] == "SEPARATION_OF_DUTIES_REQUIRED"
    assert service._store.lifecycle_events == before.lifecycle_events
    assert not any(
        record.get("record_type") == "business_exception_decision"
        for record in service._store.review_records
    )

    winner_service, _, winner_work_id, winner_claim, winner_finding = (
        _ready_brand_exception(tmp_path / "winner")
    )
    winner_request = _request_brand_exception(
        winner_service,
        winner_work_id,
        winner_claim,
        winner_finding,
    )
    winner_view = winner_service.business_exception_view(
        principal=APPROVER,
        request_id=str(winner_request["request_id"]),
        now=102,
    )
    winner_claimed = winner_service.claim_exception_work_item(
        principal=APPROVER,
        work_item_id=str(winner_request["work_item_id"]),
        expected_context=winner_view["command_context"],
        now=102,
    )
    winner = winner_service.decide_business_exception(
        principal=APPROVER,
        request_id=str(winner_request["request_id"]),
        work_item_id=str(winner_request["work_item_id"]),
        decision="approved",
        reason_code="DOCUMENTED_VARIANCE_ACCEPTED",
        expected_fence=int(winner_claimed["claim_fence"]),
        expected_context=winner_view["command_context"],
        idempotency_key="s05-first-winner",
        now=103,
    )
    loser = winner_service.decide_business_exception(
        principal=APPROVER,
        request_id=str(winner_request["request_id"]),
        work_item_id=str(winner_request["work_item_id"]),
        decision="rejected",
        reason_code="DOCUMENTED_VARIANCE_REJECTED",
        expected_fence=int(winner_claimed["claim_fence"]),
        expected_context=winner_view["command_context"],
        idempotency_key="s05-second-loser",
        now=103,
    )
    assert winner["status"] == "accepted"
    assert loser["status"] == "already_decided"
    winner_service._reload_store()
    decisions = [
        record
        for record in winner_service._store.review_records
        if record.get("record_type") == "business_exception_decision"
    ]
    assert len(decisions) == 1
    assert decisions[0]["decision"] == "approved"


def test_exception_identity_scope_and_assignment_mismatch_hide_authority(
    tmp_path: Path,
) -> None:
    service, _, work_item_id, claim, finding = _ready_brand_exception(tmp_path)
    work = service.review_work_item_view(
        principal=REVIEWER, work_item_id=work_item_id, now=100
    )
    before = _authority_snapshot(service)
    wrong_scope_reviewer = S01CommandPrincipal(
        subject=REVIEWER.subject,
        role="reviewer",
        scope="R-OBSERVED/other-tenant",
        source_id=REVIEWER.source_id,
    )
    expired_reviewer = S01CommandPrincipal(
        subject=REVIEWER.subject,
        role="reviewer",
        scope=REVIEWER.scope,
        source_id=REVIEWER.source_id,
        expires_at=101,
    )
    for principal in (wrong_scope_reviewer, expired_reviewer):
        with pytest.raises(QueryNotFound):
            service.request_business_exception(
                principal=principal,
                work_item_id=work_item_id,
                finding_id=str(finding["finding_id"]),
                reason_code="DOCUMENTED_BRAND_VARIANCE",
                expected_fence=int(claim["claim_fence"]),
                expected_context=work["command_context"],
                idempotency_key=f"s05-hidden-request-{principal.scope}",
                now=101,
            )
    assert _authority_snapshot(service) == before

    request = _request_brand_exception(service, work_item_id, claim, finding)
    wrong_approvers = (
        S01CommandPrincipal(
            subject=APPROVER.subject,
            role="exception_approver",
            scope="R-OBSERVED/other-tenant",
            source_id=APPROVER.source_id,
        ),
        S01CommandPrincipal(
            subject=APPROVER.subject,
            role="exception_approver",
            scope=APPROVER.scope,
            source_id=APPROVER.source_id,
            expires_at=102,
        ),
        S01CommandPrincipal(
            subject="unassigned-approver",
            role="exception_approver",
            scope=APPROVER.scope,
            source_id=APPROVER.source_id,
        ),
    )
    before_hidden_views = _authority_snapshot(service)
    for principal in wrong_approvers:
        with pytest.raises(QueryNotFound):
            service.business_exception_view(
                principal=principal,
                request_id=str(request["request_id"]),
                now=102,
            )
    assert _authority_snapshot(service) == before_hidden_views


def test_approver_claim_race_takeover_and_stale_fence(
    tmp_path: Path,
) -> None:
    service, _, work_item_id, claim, finding = _ready_brand_exception(tmp_path)
    request = _request_brand_exception(service, work_item_id, claim, finding)
    view = service.business_exception_view(
        principal=APPROVER,
        request_id=str(request["request_id"]),
        now=102,
    )
    barrier = threading.Barrier(2)

    def synchronize_claim(write_point: str) -> None:
        if write_point == "exception_claim.audit":
            barrier.wait(timeout=5)

    contenders = [
        ControlledScenarioService(
            fixture_root=service.fixture_root,
            rules_path=service.rules_path,
            state_path=tmp_path / "target.sqlite3",
            scenario_id="app_bad_brand.json",
            exception_approver_subject=APPROVER.subject,
            fault_injector=synchronize_claim,
        )
        for _ in range(2)
    ]
    results: list[dict[str, object]] = []

    def claim_work(contender: ControlledScenarioService) -> None:
        results.append(
            contender.claim_exception_work_item(
                principal=APPROVER,
                work_item_id=str(request["work_item_id"]),
                expected_context=view["command_context"],
                now=102,
            )
        )

    threads = [threading.Thread(target=claim_work, args=(item,)) for item in contenders]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(result["status"] for result in results) == ["claimed", "conflict"]
    first = next(result for result in results if result["status"] == "claimed")
    takeover = service.claim_exception_work_item(
        principal=APPROVER,
        work_item_id=str(request["work_item_id"]),
        expected_context=view["command_context"],
        now=int(first["claim_expires_at"]),
    )
    stale = service.decide_business_exception(
        principal=APPROVER,
        request_id=str(request["request_id"]),
        work_item_id=str(request["work_item_id"]),
        decision="approved",
        reason_code="DOCUMENTED_VARIANCE_ACCEPTED",
        expected_fence=int(first["claim_fence"]),
        expected_context=view["command_context"],
        idempotency_key="s05-stale-claim-decision",
        now=int(first["claim_expires_at"]) + 1,
    )
    accepted = service.decide_business_exception(
        principal=APPROVER,
        request_id=str(request["request_id"]),
        work_item_id=str(request["work_item_id"]),
        decision="approved",
        reason_code="DOCUMENTED_VARIANCE_ACCEPTED",
        expected_fence=int(takeover["claim_fence"]),
        expected_context=view["command_context"],
        idempotency_key="s05-takeover-decision",
        now=int(first["claim_expires_at"]) + 1,
    )

    assert takeover["status"] == "claimed"
    assert takeover["claim_fence"] == int(first["claim_fence"]) + 1
    assert takeover["claim_expires_at"] < request["expires_at"]
    assert stale["status"] == "stale"
    assert stale["reason_code"] == "STALE_EXCEPTION_WORK_ITEM_CLAIM"
    assert accepted["status"] == "accepted"


def test_concurrent_approve_reject_has_one_immutable_winner(tmp_path: Path) -> None:
    service, _, work_item_id, claim, finding = _ready_brand_exception(tmp_path)
    request = _request_brand_exception(service, work_item_id, claim, finding)
    view = service.business_exception_view(
        principal=APPROVER,
        request_id=str(request["request_id"]),
        now=102,
    )
    approver_claim = service.claim_exception_work_item(
        principal=APPROVER,
        work_item_id=str(request["work_item_id"]),
        expected_context=view["command_context"],
        now=102,
    )
    barrier = threading.Barrier(2)

    def synchronize_decision(write_point: str) -> None:
        if write_point == "exception_decision.publish":
            barrier.wait(timeout=5)

    contenders = [
        ControlledScenarioService(
            fixture_root=service.fixture_root,
            rules_path=service.rules_path,
            state_path=tmp_path / "target.sqlite3",
            scenario_id="app_bad_brand.json",
            exception_approver_subject=APPROVER.subject,
            fault_injector=synchronize_decision,
        )
        for _ in range(2)
    ]
    outcomes = (
        ("approved", "DOCUMENTED_VARIANCE_ACCEPTED"),
        ("rejected", "DOCUMENTED_VARIANCE_REJECTED"),
    )
    results: list[dict[str, object]] = []

    def decide(
        contender: ControlledScenarioService,
        decision: str,
        reason_code: str,
    ) -> None:
        results.append(
            contender.decide_business_exception(
                principal=APPROVER,
                request_id=str(request["request_id"]),
                work_item_id=str(request["work_item_id"]),
                decision=decision,
                reason_code=reason_code,
                expected_fence=int(approver_claim["claim_fence"]),
                expected_context=view["command_context"],
                idempotency_key=f"s05-racing-{decision}",
                now=103,
            )
        )

    threads = [
        threading.Thread(target=decide, args=(contender, *outcome))
        for contender, outcome in zip(contenders, outcomes, strict=True)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(result["status"] for result in results) == [
        "accepted",
        "already_decided",
    ]
    service._reload_store()
    decisions = [
        record
        for record in service._store.review_records
        if record.get("record_type") == "business_exception_decision"
        and record.get("request_id") == request["request_id"]
    ]
    assert len(decisions) == 1


def test_expiry_after_partial_routing_restores_waived_finding_and_fences_old_work(
    tmp_path: Path,
) -> None:
    service, _, request, _, brand, partial_work_id = (
        _ready_partial_routed_exception(tmp_path)
    )
    expiry_view = service.business_exception_view(
        principal=APPROVER,
        request_id=str(request["request_id"]),
        now=1000,
    )

    expired = service.expire_business_exception(
        principal=ROUTER,
        request_id=str(request["request_id"]),
        expected_context=expiry_view["command_context"],
        idempotency_key="s05-partial-expiry",
        now=1001,
    )
    service.refresh_projection()
    queue = service.queue_view(
        role="reviewer", scope="C-DEMO", subject=REVIEWER.subject, now=1002
    )
    successor = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=str(expired["successor_work_item_id"]),
        now=1002,
    )
    stale_old = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=partial_work_id,
        now=1002,
    )

    assert expired["status"] == "accepted"
    assert [item["work_item_id"] for item in queue["items"]] == [
        expired["successor_work_item_id"]
    ]
    assert brand["finding_id"] in {
        finding["finding_id"] for finding in successor["automatic_findings"]
    }
    assert stale_old["status"] == "invalidated"


def test_s04_correction_appends_exception_invalidation_and_new_run_cannot_revive_it(
    tmp_path: Path,
) -> None:
    service, application_id, request, _, _, work_item_id = (
        _ready_partial_routed_exception(tmp_path)
    )
    service.refresh_projection()
    work = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=105,
    )
    claim = service.claim_review_work_item(
        principal=REVIEWER,
        work_item_id=work_item_id,
        expected_context=work["command_context"],
        now=105,
    )
    workspace = service.workspace_view(
        application_id,
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=105,
    )
    engine = next(
        finding
        for finding in workspace["mandatory_blockers"]
        if finding["rule_id"] == "R_ENGINE_CROSS"
    )
    source = next(
        link for link in engine["evidence_links"] if link["document_id"] == "inv"
    )
    before_run = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )["runs"][0]
    corrected = service.correct_field_observation(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=work_item_id,
        expected_fence=int(claim["claim_fence"]),
        expected_context=work["command_context"],
        idempotency_key="s05-correction-invalidates-exception",
        correction={
            "schema_version": "field-observation-correction/1",
            "finding_id": engine["finding_id"],
            "observation_id": source["observation_id"],
            "document_id": source["document_id"],
            "document_role": source["document_role"],
            "field": source["field"],
            "raw": "ENG555555",
            "source_location": {
                key: source[key]
                for key in ("source_sha256", "source_page", "source_region")
            },
            "reason_code": "SOURCE_VALUE_MISREAD",
        },
        now=106,
    )
    invalidated = service.business_exception_view(
        principal=APPROVER,
        request_id=str(request["request_id"]),
        now=106,
    )
    service._reload_store()
    invalidation_records = [
        record
        for record in service._store.review_records
        if record.get("record_type") == "business_exception_invalidated"
        and record.get("request_id") == request["request_id"]
    ]

    assert corrected["status"] == "accepted"
    assert invalidated["status"] == "invalidated"
    assert invalidated["current"] is False
    assert len(invalidation_records) == 1
    assert invalidation_records[0]["reason_code"] == "EVIDENCE_CORRECTION_ACCEPTED"
    assert service.process_next_job().status == "complete"
    service.refresh_projection()
    after = service.business_exception_view(
        principal=APPROVER,
        request_id=str(request["request_id"]),
        now=107,
    )
    history = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    assert after["status"] == "invalidated"
    assert after["current"] is False
    assert before_run["authority_digest"] == history["runs"][0]["authority_digest"]
    assert history["runs"][0]["current"] is False
    assert history["runs"][1]["current"] is True


@pytest.mark.parametrize("decision_first", (False, True))
def test_policy_revocation_is_an_immutable_successor_and_old_commands_stay_stale(
    tmp_path: Path,
    decision_first: bool,
) -> None:
    service, _, work_item_id, claim, finding = _ready_brand_exception(tmp_path)
    request = _request_brand_exception(service, work_item_id, claim, finding)
    view = service.business_exception_view(
        principal=APPROVER,
        request_id=str(request["request_id"]),
        now=102,
    )
    decision = None
    if decision_first:
        decision = _approve_brand_exception(service, request)
        view = service.business_exception_view(
            principal=APPROVER,
            request_id=str(request["request_id"]),
            now=104,
        )

    revoked = service.invalidate_business_exception(
        principal=ROUTER,
        request_id=str(request["request_id"]),
        reason_code="POLICY_REVOKED",
        expected_context=view["command_context"],
        idempotency_key=f"s05-revoke-{decision_first}",
        now=105,
    )
    replay = service.invalidate_business_exception(
        principal=ROUTER,
        request_id=str(request["request_id"]),
        reason_code="POLICY_REVOKED",
        expected_context=view["command_context"],
        idempotency_key=f"s05-revoke-{decision_first}",
        now=106,
    )
    final = service.business_exception_view(
        principal=APPROVER,
        request_id=str(request["request_id"]),
        now=106,
    )

    assert revoked["status"] == "accepted"
    assert replay == {**revoked, "replayed": True}
    assert final["status"] == "invalidated"
    assert final["current"] is False
    assert final["actions"] == []
    if decision is not None:
        late_route = service.determine_business_exception_route(
            principal=ROUTER,
            request_id=str(request["request_id"]),
            expected_context=decision["routing_context"],
            idempotency_key="s05-route-after-revocation",
            now=106,
        )
        assert late_route["status"] == "stale"


def test_concurrent_requests_have_one_active_winner_and_stable_conflict(
    tmp_path: Path,
) -> None:
    service, _, work_item_id, claim, finding = _ready_brand_exception(tmp_path)
    initial = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=100,
    )
    barrier = threading.Barrier(2)

    def synchronize_publish(write_point: str) -> None:
        if write_point == "exception_request.publish":
            barrier.wait(timeout=5)

    contenders = [
        ControlledScenarioService(
            fixture_root=ROOT / "fixtures" / "applications",
            rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
            state_path=tmp_path / "target.sqlite3",
            scenario_id="app_bad_brand.json",
            exception_approver_subject=APPROVER.subject,
            fault_injector=synchronize_publish,
        )
        for _ in range(2)
    ]
    results: list[dict[str, object]] = []

    def request(contender: ControlledScenarioService, index: int) -> None:
        results.append(
            contender.request_business_exception(
                principal=REVIEWER,
                work_item_id=work_item_id,
                finding_id=str(finding["finding_id"]),
                reason_code="DOCUMENTED_BRAND_VARIANCE",
                expected_fence=int(claim["claim_fence"]),
                expected_context=initial["command_context"],
                idempotency_key=f"s05-concurrent-request-{index}",
                now=101,
            )
        )

    threads = [
        threading.Thread(target=request, args=(contender, index))
        for index, contender in enumerate(contenders)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(result["status"] for result in results) == ["accepted", "conflict"]
    service._reload_store()
    assert sum(
        record.get("record_type") == "business_exception_request"
        for record in service._store.review_records
    ) == 1
    assert sum(
        item.get("kind") == "exception_approval" for item in service._store.work_items
    ) == 1


def test_policy_digest_drift_fails_closed_before_decision_with_zero_effect(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    service, _, work_item_id, claim, finding = _ready_brand_exception(tmp_path)
    request = _request_brand_exception(service, work_item_id, claim, finding)
    view = service.business_exception_view(
        principal=APPROVER,
        request_id=str(request["request_id"]),
        now=102,
    )
    approver_claim = service.claim_exception_work_item(
        principal=APPROVER,
        work_item_id=str(request["work_item_id"]),
        expected_context=view["command_context"],
        now=102,
    )
    target = service._release["target_release"]
    service._release["target_release"] = replace(
        target,
        waiver_policy_digest="0" * 64,
    )
    before = copy.deepcopy(service._store)

    drifted = service.business_exception_view(
        principal=APPROVER,
        request_id=str(request["request_id"]),
        now=103,
    )
    denied = service.decide_business_exception(
        principal=APPROVER,
        request_id=str(request["request_id"]),
        work_item_id=str(request["work_item_id"]),
        decision="approved",
        reason_code="DOCUMENTED_VARIANCE_ACCEPTED",
        expected_fence=int(approver_claim["claim_fence"]),
        expected_context=view["command_context"],
        idempotency_key="s05-policy-drift-decision",
        now=103,
    )

    assert drifted["current"] is False
    assert drifted["currentness_reason"] == "CONTEXT_NOT_CURRENT"
    assert denied["status"] == "stale"
    assert service._store.lifecycle_events == before.lifecycle_events
    assert service._store.review_records == before.review_records
    assert service._store.audit_events == before.audit_events
    assert service._store.idempotency == before.idempotency


def test_restart_rebuild_preserves_minimized_exception_history_and_run_bytes(
    tmp_path: Path,
) -> None:
    service, application_id, work_item_id, claim, finding = _ready_brand_exception(
        tmp_path
    )
    before_run = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )["runs"][0]
    request = _request_brand_exception(service, work_item_id, claim, finding)
    decision = _approve_brand_exception(service, request)
    routed = service.determine_business_exception_route(
        principal=ROUTER,
        request_id=str(request["request_id"]),
        expected_context=decision["routing_context"],
        idempotency_key="s05-restart-route",
        now=104,
    )
    restarted = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "target.sqlite3",
        scenario_id="app_bad_brand.json",
        exception_approver_subject=APPROVER.subject,
    )
    rebuilt = restarted.business_exception_view(
        principal=APPROVER,
        request_id=str(request["request_id"]),
        now=105,
    )
    history = restarted.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    route = restarted.current_route_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    serialized = json.dumps(rebuilt, ensure_ascii=False)

    assert routed["phase"] == "Verification Completed"
    assert rebuilt["status"] == "approved"
    assert rebuilt["current"] is False
    assert rebuilt["currentness_reason"] == "PROCESSING_CYCLE_SEALED"
    assert rebuilt["finding"]["verdict"] == "inconsistent"
    assert rebuilt["actions"] == []
    assert history["runs"][0]["authority_digest"] == before_run["authority_digest"]
    assert request["request_id"] in history["runs"][0]["exception_ids"]
    assert history["runs"][0]["applicable_exception_ids"] == []
    assert route["route"] == "human_complete"
    fixture = (ROOT / "fixtures" / "applications" / "app_bad_brand.json").read_text(
        encoding="utf-8"
    )
    raw_values = [
        field["raw"]
        for document in json.loads(fixture)["documents"]
        for field in document["fields"].values()
    ]
    assert all(str(raw) not in serialized for raw in raw_values)


@pytest.mark.parametrize(
    "fault_point",
    (
        "exception_request.lifecycle",
        "exception_request.request",
        "exception_request.review_responsibility",
        "exception_request.work_item",
        "exception_request.audit",
        "exception_request.idempotency",
        "exception_request.publish",
    ),
)
def test_each_exception_request_write_fault_is_atomic(
    tmp_path: Path,
    fault_point: str,
) -> None:
    service, _, work_item_id, claim, finding = _ready_brand_exception(tmp_path)
    view = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=100,
    )
    before = _authority_snapshot(service)
    faulty = _faulty_service(service, tmp_path / "target.sqlite3", fault_point)
    failed = faulty.request_business_exception(
        principal=REVIEWER,
        work_item_id=work_item_id,
        finding_id=str(finding["finding_id"]),
        reason_code="DOCUMENTED_BRAND_VARIANCE",
        expected_fence=int(claim["claim_fence"]),
        expected_context=view["command_context"],
        idempotency_key=f"s05-request-fault-{fault_point}",
        now=101,
    )

    assert failed["status"] == "unavailable"
    assert failed["reason_code"] == (
        "AUDIT_UNAVAILABLE"
        if fault_point == "exception_request.audit"
        else "STORAGE_UNAVAILABLE"
    )
    assert _authority_snapshot(service) == before


def test_exception_claim_audit_fault_is_atomic(tmp_path: Path) -> None:
    service, _, work_item_id, claim, finding = _ready_brand_exception(tmp_path)
    request = _request_brand_exception(service, work_item_id, claim, finding)
    view = service.business_exception_view(
        principal=APPROVER,
        request_id=str(request["request_id"]),
        now=102,
    )
    before = _authority_snapshot(service)
    faulty = _faulty_service(
        service,
        tmp_path / "target.sqlite3",
        "exception_claim.audit",
    )
    failed = faulty.claim_exception_work_item(
        principal=APPROVER,
        work_item_id=str(request["work_item_id"]),
        expected_context=view["command_context"],
        now=102,
    )

    assert failed["status"] == "unavailable"
    assert failed["reason_code"] == "AUDIT_UNAVAILABLE"
    assert _authority_snapshot(service) == before


@pytest.mark.parametrize(
    "fault_point",
    (
        "exception_decision.lifecycle",
        "exception_decision.decision",
        "exception_decision.work_item",
        "exception_decision.review_successor",
        "exception_decision.audit",
        "exception_decision.idempotency",
        "exception_decision.publish",
    ),
)
def test_each_exception_decision_write_fault_is_atomic(
    tmp_path: Path,
    fault_point: str,
) -> None:
    service, _, work_item_id, claim, finding = _ready_brand_exception(tmp_path)
    request = _request_brand_exception(service, work_item_id, claim, finding)
    view = service.business_exception_view(
        principal=APPROVER,
        request_id=str(request["request_id"]),
        now=102,
    )
    approver_claim = service.claim_exception_work_item(
        principal=APPROVER,
        work_item_id=str(request["work_item_id"]),
        expected_context=view["command_context"],
        now=102,
    )
    before = _authority_snapshot(service)
    faulty = _faulty_service(service, tmp_path / "target.sqlite3", fault_point)
    failed = faulty.decide_business_exception(
        principal=APPROVER,
        request_id=str(request["request_id"]),
        work_item_id=str(request["work_item_id"]),
        decision="rejected",
        reason_code="DOCUMENTED_VARIANCE_REJECTED",
        expected_fence=int(approver_claim["claim_fence"]),
        expected_context=view["command_context"],
        idempotency_key=f"s05-decision-fault-{fault_point}",
        now=103,
    )

    assert failed["status"] == "unavailable"
    assert failed["reason_code"] == (
        "AUDIT_UNAVAILABLE"
        if fault_point == "exception_decision.audit"
        else "STORAGE_UNAVAILABLE"
    )
    assert _authority_snapshot(service) == before


@pytest.mark.parametrize(
    "fault_point",
    (
        "exception_route.lifecycle",
        "exception_route.review_successor",
        "exception_route.record",
        "exception_route.audit",
        "exception_route.idempotency",
        "exception_route.publish",
    ),
)
def test_each_exception_route_write_fault_is_atomic(
    tmp_path: Path,
    fault_point: str,
) -> None:
    service, _, request, decision, _ = _ready_approved_exception_with_extra_blocker(
        tmp_path
    )
    before = _authority_snapshot(service)
    faulty = _faulty_service(
        service, Path(service._store.state_path), fault_point
    )
    failed = faulty.determine_business_exception_route(
        principal=ROUTER,
        request_id=str(request["request_id"]),
        expected_context=decision["routing_context"],
        idempotency_key=f"s05-route-fault-{fault_point}",
        now=104,
    )

    assert failed["status"] == "unavailable"
    assert failed["reason_code"] == (
        "AUDIT_UNAVAILABLE"
        if fault_point == "exception_route.audit"
        else "STORAGE_UNAVAILABLE"
    )
    assert _authority_snapshot(service) == before


@pytest.mark.parametrize("mode", ("expiry", "invalidation"))
@pytest.mark.parametrize(
    "suffix",
    (
        "lifecycle",
        "record",
        "work_item",
        "review_successor",
        "audit",
        "idempotency",
        "publish",
    ),
)
def test_each_exception_deactivation_write_fault_is_atomic(
    tmp_path: Path,
    mode: str,
    suffix: str,
) -> None:
    service, _, work_item_id, claim, finding = _ready_brand_exception(tmp_path)
    request = _request_brand_exception(service, work_item_id, claim, finding)
    view = service.business_exception_view(
        principal=APPROVER,
        request_id=str(request["request_id"]),
        now=102,
    )
    fault_point = f"exception_{mode}.{suffix}"
    before = _authority_snapshot(service)
    faulty = _faulty_service(service, tmp_path / "target.sqlite3", fault_point)
    if mode == "expiry":
        failed = faulty.expire_business_exception(
            principal=ROUTER,
            request_id=str(request["request_id"]),
            expected_context=view["command_context"],
            idempotency_key=f"s05-expiry-fault-{suffix}",
            now=int(request["expires_at"]),
        )
    else:
        failed = faulty.invalidate_business_exception(
            principal=ROUTER,
            request_id=str(request["request_id"]),
            reason_code="POLICY_REVOKED",
            expected_context=view["command_context"],
            idempotency_key=f"s05-invalidation-fault-{suffix}",
            now=103,
        )

    assert failed["status"] == "unavailable"
    assert failed["reason_code"] == (
        "AUDIT_UNAVAILABLE" if suffix == "audit" else "STORAGE_UNAVAILABLE"
    )
    assert _authority_snapshot(service) == before


@pytest.mark.parametrize(
    ("audit_available", "storage_available", "reason_code"),
    (
        (False, True, "AUDIT_UNAVAILABLE"),
        (True, False, "STORAGE_UNAVAILABLE"),
    ),
)
def test_exception_request_write_gates_fail_closed_without_effect(
    tmp_path: Path,
    audit_available: bool,
    storage_available: bool,
    reason_code: str,
) -> None:
    service, _, work_item_id, claim, finding = _ready_brand_exception(tmp_path)
    view = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=100,
    )
    before = _authority_snapshot(service)
    gated = ControlledScenarioService(
        fixture_root=service.fixture_root,
        rules_path=service.rules_path,
        state_path=tmp_path / "target.sqlite3",
        scenario_id="app_bad_brand.json",
        exception_approver_subject=APPROVER.subject,
        audit_available=audit_available,
        storage_available=storage_available,
    )
    failed = gated.request_business_exception(
        principal=REVIEWER,
        work_item_id=work_item_id,
        finding_id=str(finding["finding_id"]),
        reason_code="DOCUMENTED_BRAND_VARIANCE",
        expected_fence=int(claim["claim_fence"]),
        expected_context=view["command_context"],
        idempotency_key=f"s05-gate-{reason_code}",
        now=101,
    )

    assert failed["status"] == "unavailable"
    assert failed["reason_code"] == reason_code
    assert _authority_snapshot(service) == before


@pytest.mark.parametrize(
    "fault_point",
    (
        "exception_operations.record",
        "exception_operations.lifecycle",
        "exception_operations.invalidation",
        "exception_operations.claim_fence",
        "exception_operations.review_successor",
        "exception_operations.audit",
        "exception_operations.idempotency",
        "exception_operations.publish",
    ),
)
def test_each_exception_operations_write_fault_is_atomic(
    tmp_path: Path,
    fault_point: str,
) -> None:
    service, _, work_item_id, claim, finding = _ready_brand_exception(tmp_path)
    request = _request_brand_exception(service, work_item_id, claim, finding)
    view = service.business_exception_view(
        principal=APPROVER,
        request_id=str(request["request_id"]),
        now=102,
    )
    service.claim_exception_work_item(
        principal=APPROVER,
        work_item_id=str(request["work_item_id"]),
        expected_context=view["command_context"],
        now=102,
    )
    before = _authority_snapshot(service)
    faulty = _faulty_service(service, tmp_path / "target.sqlite3", fault_point)

    failed = faulty.close_business_exception_operations(
        principal=ROUTER,
        idempotency_key=f"s05-operations-fault-{fault_point}",
        now=101,
    )

    assert failed["status"] == "unavailable"
    assert failed["reason_code"] == (
        "AUDIT_UNAVAILABLE"
        if fault_point == "exception_operations.audit"
        else "STORAGE_UNAVAILABLE"
    )
    assert _authority_snapshot(service) == before


def test_close_atomically_drains_multiple_requests_across_scopes_and_restart(
    tmp_path: Path,
) -> None:
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "target.sqlite3",
        scenario_id="app_bad_brand.json",
        exception_approver_subject=APPROVER.subject,
    )
    requests: list[dict[str, object]] = []
    reviewers: list[S01CommandPrincipal] = []
    for index in range(2):
        reviewer = S01CommandPrincipal(
            subject=f"s05-multi-reviewer-{index}",
            role="reviewer",
            scope=f"C-DEMO/session/{index:032x}",
            source_id="s05-review-console",
        )
        integrator = S01CommandPrincipal(
            subject=reviewer.subject,
            role="integrator",
            scope=reviewer.scope,
            source_id="s05-intake",
        )
        admitted = service.submit_demo(
            scenario_id="app_bad_brand.json",
            idempotency_key=f"s05-multi-intake-{index}",
            principal=integrator,
        )
        assert admitted.application_id is not None
        assert service.process_next_job().status == "complete"
        service.refresh_projection()
        queue = service.queue_view(
            role="reviewer",
            scope=reviewer.scope,
            subject=reviewer.subject,
            now=100,
        )
        assert len(queue["items"]) == 1
        review_work_item_id = queue["items"][0]["work_item_id"]
        work = service.review_work_item_view(
            principal=reviewer,
            work_item_id=review_work_item_id,
            now=100,
        )
        finding = next(
            item
            for item in work["automatic_findings"]
            if item["rule_id"] == "R_BRAND_CROSS"
        )
        claim = service.claim_review_work_item(
            principal=reviewer,
            work_item_id=review_work_item_id,
            expected_context=work["command_context"],
            now=100,
        )
        requested = service.request_business_exception(
            principal=reviewer,
            work_item_id=review_work_item_id,
            finding_id=str(finding["finding_id"]),
            reason_code="DOCUMENTED_BRAND_VARIANCE",
            expected_fence=int(claim["claim_fence"]),
            expected_context=work["command_context"],
            idempotency_key=f"s05-multi-request-{index}",
            now=101,
        )
        assert requested["status"] == "accepted"
        requests.append(requested)
        reviewers.append(reviewer)

    first_view = service.business_exception_view(
        principal=APPROVER,
        request_id=str(requests[0]["request_id"]),
        now=102,
    )
    first_claim = service.claim_exception_work_item(
        principal=APPROVER,
        work_item_id=str(requests[0]["work_item_id"]),
        expected_context=first_view["command_context"],
        now=102,
    )
    closed = service.close_business_exception_operations(
        principal=ROUTER,
        idempotency_key="s05-multi-close",
        now=103,
    )

    assert first_claim["status"] == "claimed"
    assert closed["invalidated_request_ids"] == sorted(
        request["request_id"] for request in requests
    )
    assert closed["unresolved_request_count"] == 0

    restarted = ControlledScenarioService(
        fixture_root=service.fixture_root,
        rules_path=service.rules_path,
        state_path=tmp_path / "target.sqlite3",
        scenario_id="app_bad_brand.json",
        exception_approver_subject=APPROVER.subject,
    )
    status = restarted.business_exception_operations_status(
        principal=ROUTER,
        now=104,
    )
    views = [
        restarted.business_exception_view(
            principal=APPROVER,
            request_id=str(request["request_id"]),
            now=104,
        )
        for request in requests
    ]
    restarted.refresh_projection()
    queues = [
        restarted.queue_view(
            role="reviewer",
            scope=reviewer.scope,
            subject=reviewer.subject,
            now=104,
        )
        for reviewer in reviewers
    ]
    resumed = restarted.resume_business_exception_operations(
        principal=ROUTER,
        idempotency_key="s05-multi-resume",
        now=105,
    )

    assert status["operations"] == "closed"
    assert status["unresolved_request_count"] == 0
    assert [view["status"] for view in views] == ["invalidated", "invalidated"]
    assert [view["claim_status"] for view in views] == [
        "invalidated",
        "invalidated",
    ]
    assert all(view["claim_subject"] is None for view in views)
    assert [len(queue["items"]) for queue in queues] == [1, 1]
    assert resumed["status"] == "accepted"


def test_independent_business_exception_approval_routes_without_mutating_run(
    tmp_path: Path,
) -> None:
    service, application_id, review_work_item_id, review_claim, finding = (
        _ready_brand_exception(tmp_path)
    )
    before = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=review_work_item_id,
        now=100,
    )
    history_before = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )

    requested = service.request_business_exception(
        principal=REVIEWER,
        work_item_id=review_work_item_id,
        finding_id=finding["finding_id"],
        reason_code="DOCUMENTED_BRAND_VARIANCE",
        expected_fence=review_claim["claim_fence"],
        expected_context=before["command_context"],
        idempotency_key="s05-brand-request",
        now=101,
    )
    approver_view = service.business_exception_view(
        principal=APPROVER,
        request_id=requested["request_id"],
        now=102,
    )
    approver_claim = service.claim_exception_work_item(
        principal=APPROVER,
        work_item_id=requested["work_item_id"],
        expected_context=approver_view["command_context"],
        now=102,
    )
    decided = service.decide_business_exception(
        principal=APPROVER,
        request_id=requested["request_id"],
        work_item_id=requested["work_item_id"],
        decision="approved",
        reason_code="DOCUMENTED_VARIANCE_ACCEPTED",
        expected_fence=approver_claim["claim_fence"],
        expected_context=approver_view["command_context"],
        idempotency_key="s05-brand-approve",
        now=103,
    )
    routed = service.determine_business_exception_route(
        principal=ROUTER,
        request_id=requested["request_id"],
        expected_context=decided["routing_context"],
        idempotency_key="s05-brand-route",
        now=104,
    )
    current = service.current_route_view(
        principal=REVIEWER,
        application_id=application_id,
    )
    history_after = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )

    assert requested["phase"] == "Pending Exception Approval"
    assert requested["expires_at"] == 1001
    assert approver_view["finding"] == {
        "finding_id": finding["finding_id"],
        "rule_id": "R_BRAND_CROSS",
        "verdict": "inconsistent",
        "severity": "major",
        "reason_code": finding["reason_code"],
    }
    assert approver_view["scope"] == "one_application_cycle_run_finding"
    assert approver_view["request_reason"] == "DOCUMENTED_BRAND_VARIANCE"
    assert decided["phase"] == "Routing Determination"
    assert routed["phase"] == "Verification Completed"
    assert routed["completion_basis"] == "business_exception"
    assert current["route"] == "human_complete"
    assert history_after["runs"][0]["authority_digest"] == history_before["runs"][0][
        "authority_digest"
    ]
    assert history_after["runs"][0]["finding_ids"] == history_before["runs"][0][
        "finding_ids"
    ]


def test_rejected_business_exception_returns_fresh_manual_review_work(
    tmp_path: Path,
) -> None:
    service, application_id, review_work_item_id, review_claim, finding = (
        _ready_brand_exception(tmp_path)
    )
    initial = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=review_work_item_id,
        now=100,
    )
    before_run = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )["runs"][0]
    requested = service.request_business_exception(
        principal=REVIEWER,
        work_item_id=review_work_item_id,
        finding_id=finding["finding_id"],
        reason_code="DOCUMENTED_BRAND_VARIANCE",
        expected_fence=review_claim["claim_fence"],
        expected_context=initial["command_context"],
        idempotency_key="s05-brand-request-reject",
        now=101,
    )
    approver_view = service.business_exception_view(
        principal=APPROVER,
        request_id=requested["request_id"],
        now=102,
    )
    approver_claim = service.claim_exception_work_item(
        principal=APPROVER,
        work_item_id=requested["work_item_id"],
        expected_context=approver_view["command_context"],
        now=102,
    )

    rejected = service.decide_business_exception(
        principal=APPROVER,
        request_id=requested["request_id"],
        work_item_id=requested["work_item_id"],
        decision="rejected",
        reason_code="DOCUMENTED_VARIANCE_REJECTED",
        expected_fence=approver_claim["claim_fence"],
        expected_context=approver_view["command_context"],
        idempotency_key="s05-brand-reject",
        now=103,
    )
    service.refresh_projection()
    queue = service.queue_view(
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=104,
    )
    old_work = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=review_work_item_id,
        now=104,
    )
    final_view = service.business_exception_view(
        principal=APPROVER,
        request_id=requested["request_id"],
        now=104,
    )
    after_run = service.application_history_view(
        principal=REVIEWER,
        application_id=application_id,
    )["runs"][0]

    assert rejected["phase"] == "Manual Review"
    assert rejected["route"] == "manual_review"
    assert rejected["successor_work_item_id"] != review_work_item_id
    assert [item["work_item_id"] for item in queue["items"]] == [
        rejected["successor_work_item_id"]
    ]
    assert old_work["status"] == "exception_requested"
    assert final_view["status"] == "rejected"
    assert final_view["current"] is False
    assert final_view["actions"] == []
    assert after_run["authority_digest"] == before_run["authority_digest"]


def test_business_exception_expires_at_equality_and_replays_without_resurrection(
    tmp_path: Path,
) -> None:
    service, application_id, review_work_item_id, review_claim, finding = (
        _ready_brand_exception(tmp_path)
    )
    initial = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=review_work_item_id,
        now=100,
    )
    requested = service.request_business_exception(
        principal=REVIEWER,
        work_item_id=review_work_item_id,
        finding_id=finding["finding_id"],
        reason_code="DOCUMENTED_BRAND_VARIANCE",
        expected_fence=review_claim["claim_fence"],
        expected_context=initial["command_context"],
        idempotency_key="s05-brand-request-expiry",
        now=101,
    )
    approver_view = service.business_exception_view(
        principal=APPROVER,
        request_id=requested["request_id"],
        now=1000,
    )
    before_counts = service.fact_counts()

    not_due = service.expire_business_exception(
        principal=ROUTER,
        request_id=requested["request_id"],
        expected_context=approver_view["command_context"],
        idempotency_key="s05-brand-expire-not-due",
        now=1000,
    )
    expired = service.expire_business_exception(
        principal=ROUTER,
        request_id=requested["request_id"],
        expected_context=approver_view["command_context"],
        idempotency_key="s05-brand-expire",
        now=1001,
    )
    replay = service.expire_business_exception(
        principal=ROUTER,
        request_id=requested["request_id"],
        expected_context=approver_view["command_context"],
        idempotency_key="s05-brand-expire",
        now=1002,
    )
    service.refresh_projection()
    final_view = service.business_exception_view(
        principal=APPROVER,
        request_id=requested["request_id"],
        now=1002,
    )
    queue = service.queue_view(
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=1002,
    )

    assert not_due == {
        "status": "not_due",
        "replayed": False,
        "request_id": requested["request_id"],
        "expires_at": 1001,
        "reason_code": "BUSINESS_EXCEPTION_NOT_EXPIRED",
    }
    assert service.fact_counts()["findings"] == before_counts["findings"]
    assert expired["status"] == "accepted"
    assert expired["phase"] == "Manual Review"
    assert expired["successor_work_item_id"] == queue["items"][0]["work_item_id"]
    assert replay == {**expired, "replayed": True}
    assert final_view["status"] == "expired"
    assert final_view["current"] is False
    assert final_view["actions"] == []
    assert service.current_route_view(
        principal=REVIEWER,
        application_id=application_id,
    )["route"] == "manual_review"


def test_close_new_exception_entry_persists_and_resume_reopens_normal_gates(
    tmp_path: Path,
) -> None:
    service, _, work_item_id, claim, finding = _ready_brand_exception(tmp_path)
    work = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=work_item_id,
        now=100,
    )

    closed = service.close_business_exception_operations(
        principal=ROUTER,
        idempotency_key="s05-close-before-request",
        now=101,
    )
    blocked = service.request_business_exception(
        principal=REVIEWER,
        work_item_id=work_item_id,
        finding_id=str(finding["finding_id"]),
        reason_code="DOCUMENTED_BRAND_VARIANCE",
        expected_fence=int(claim["claim_fence"]),
        expected_context=work["command_context"],
        idempotency_key="s05-request-while-closed",
        now=102,
    )
    restarted = ControlledScenarioService(
        fixture_root=service.fixture_root,
        rules_path=service.rules_path,
        state_path=tmp_path / "target.sqlite3",
        scenario_id="app_bad_brand.json",
        exception_approver_subject=APPROVER.subject,
    )

    assert closed["status"] == "accepted"
    assert closed["operations"] == "closed"
    assert blocked["status"] == "stopped"
    assert blocked["reason_code"] == "BUSINESS_EXCEPTION_OPERATIONS_CLOSED"
    assert restarted.business_exception_operations_status(
        principal=ROUTER, now=102
    )["operations"] == "closed"

    resumed = restarted.resume_business_exception_operations(
        principal=ROUTER,
        idempotency_key="s05-resume-before-request",
        now=103,
    )
    accepted = restarted.request_business_exception(
        principal=REVIEWER,
        work_item_id=work_item_id,
        finding_id=str(finding["finding_id"]),
        reason_code="DOCUMENTED_BRAND_VARIANCE",
        expected_fence=int(claim["claim_fence"]),
        expected_context=work["command_context"],
        idempotency_key="s05-request-after-resume",
        now=104,
    )

    assert resumed["status"] == "accepted"
    assert resumed["operations"] == "open"
    assert accepted["status"] == "accepted"


@pytest.mark.parametrize("decision_first", (False, True))
def test_close_drain_fence_invalidate_restart_and_resume(
    tmp_path: Path,
    decision_first: bool,
) -> None:
    service, _, work_item_id, claim, finding = _ready_brand_exception(tmp_path)
    request = _request_brand_exception(service, work_item_id, claim, finding)
    view = service.business_exception_view(
        principal=APPROVER,
        request_id=str(request["request_id"]),
        now=102,
    )
    approver_claim = service.claim_exception_work_item(
        principal=APPROVER,
        work_item_id=str(request["work_item_id"]),
        expected_context=view["command_context"],
        now=102,
    )
    decision = None
    if decision_first:
        decision = service.decide_business_exception(
            principal=APPROVER,
            request_id=str(request["request_id"]),
            work_item_id=str(request["work_item_id"]),
            decision="approved",
            reason_code="DOCUMENTED_VARIANCE_ACCEPTED",
            expected_fence=int(approver_claim["claim_fence"]),
            expected_context=view["command_context"],
            idempotency_key="s05-approve-before-close",
            now=103,
        )
        view = service.business_exception_view(
            principal=APPROVER,
            request_id=str(request["request_id"]),
            now=104,
        )

    closed = service.close_business_exception_operations(
        principal=ROUTER,
        idempotency_key=f"s05-close-with-work-{decision_first}",
        now=105,
    )
    closed_view = service.business_exception_view(
        principal=APPROVER,
        request_id=str(request["request_id"]),
        now=105,
    )
    closed_status = service.business_exception_operations_status(
        principal=ROUTER,
        now=105,
    )
    history_after_close = service.application_history_view(
        principal=REVIEWER,
        application_id=str(request["application_id"]),
    )
    if decision is None:
        blocked = service.decide_business_exception(
            principal=APPROVER,
            request_id=str(request["request_id"]),
            work_item_id=str(request["work_item_id"]),
            decision="approved",
            reason_code="DOCUMENTED_VARIANCE_ACCEPTED",
            expected_fence=int(approver_claim["claim_fence"]),
            expected_context=view["command_context"],
            idempotency_key="s05-decision-while-closed",
            now=106,
        )
    else:
        blocked = service.determine_business_exception_route(
            principal=ROUTER,
            request_id=str(request["request_id"]),
            expected_context=decision["routing_context"],
            idempotency_key="s05-route-while-closed",
            now=106,
        )

    assert closed["status"] == "accepted"
    assert closed["invalidated_request_ids"] == [request["request_id"]]
    assert closed["unresolved_request_count"] == 0
    assert closed_status["unresolved_request_count"] == 0
    assert closed_view["status"] == "invalidated"
    assert closed_view["current"] is False
    assert closed_view["claim_status"] == "invalidated"
    assert closed_view["claim_subject"] is None
    assert closed_view["actions"] == []
    assert blocked["status"] == "stopped"
    assert service.application_history_view(
        principal=REVIEWER,
        application_id=str(request["application_id"]),
    ) == history_after_close

    restarted = ControlledScenarioService(
        fixture_root=service.fixture_root,
        rules_path=service.rules_path,
        state_path=tmp_path / "target.sqlite3",
        scenario_id="app_bad_brand.json",
        exception_approver_subject=APPROVER.subject,
    )
    rebuilt = restarted.business_exception_view(
        principal=APPROVER,
        request_id=str(request["request_id"]),
        now=107,
    )
    rebuilt_status = restarted.business_exception_operations_status(
        principal=ROUTER,
        now=107,
    )
    restarted.refresh_projection()
    queue = restarted.queue_view(
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=107,
    )
    resumed = restarted.resume_business_exception_operations(
        principal=ROUTER,
        idempotency_key=f"s05-resume-after-drain-{decision_first}",
        now=108,
    )
    history_before_late = restarted.application_history_view(
        principal=REVIEWER,
        application_id=str(request["application_id"]),
    )
    if decision is None:
        late = restarted.decide_business_exception(
            principal=APPROVER,
            request_id=str(request["request_id"]),
            work_item_id=str(request["work_item_id"]),
            decision="approved",
            reason_code="DOCUMENTED_VARIANCE_ACCEPTED",
            expected_fence=int(approver_claim["claim_fence"]),
            expected_context=view["command_context"],
            idempotency_key="s05-late-old-decision-after-close",
            now=109,
        )
    else:
        late = restarted.determine_business_exception_route(
            principal=ROUTER,
            request_id=str(request["request_id"]),
            expected_context=decision["routing_context"],
            idempotency_key="s05-late-old-route-after-close",
            now=109,
        )

    assert rebuilt["status"] == "invalidated"
    assert rebuilt["current"] is False
    assert rebuilt_status["operations"] == "closed"
    assert rebuilt_status["unresolved_request_count"] == 0
    assert len(queue["items"]) == 1
    assert request["finding_id"] in {
        finding["finding_id"] for finding in queue["items"][0]["mandatory_blockers"]
    }
    assert restarted.business_exception_operations_status(
        principal=ROUTER, now=108
    )["operations"] == "open"
    assert resumed["status"] == "accepted"
    assert late["status"] == "stale"
    assert restarted.application_history_view(
        principal=REVIEWER,
        application_id=str(request["application_id"]),
    ) == history_before_late


def _workspace_eligibility(
    service: ControlledScenarioService,
    application_id: str,
    *,
    now: int = 100,
) -> dict[str, object]:
    workspace = service.workspace_view(
        application_id,
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=now,
    )
    return dict(workspace["business_exception_eligibility"])


def _reject_brand_exception(
    service: ControlledScenarioService,
    request: dict[str, object],
    *,
    now: int = 103,
) -> dict[str, object]:
    view = service.business_exception_view(
        principal=APPROVER,
        request_id=str(request["request_id"]),
        now=now - 1,
    )
    claim = service.claim_exception_work_item(
        principal=APPROVER,
        work_item_id=str(request["work_item_id"]),
        expected_context=view["command_context"],
        now=now - 1,
    )
    return service.decide_business_exception(
        principal=APPROVER,
        request_id=str(request["request_id"]),
        work_item_id=str(request["work_item_id"]),
        decision="rejected",
        reason_code="DOCUMENTED_VARIANCE_REJECTED",
        expected_fence=int(claim["claim_fence"]),
        expected_context=view["command_context"],
        idempotency_key=f"reject-{request['request_id']}",
        now=now,
    )


def test_workspace_projects_eligible_exception_for_claimed_brand_finding(
    tmp_path: Path,
) -> None:
    service, application_id, _, _, _ = _ready_brand_exception(tmp_path)

    assert _workspace_eligibility(service, application_id) == {
        "eligible": True,
        "request_reason": "DOCUMENTED_BRAND_VARIANCE",
        "ineligible_reason_code": None,
        "predecessor_request_id": None,
    }


def test_workspace_projects_unclaimed_review_as_stale_ineligible(
    tmp_path: Path,
) -> None:
    service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=tmp_path / "unclaimed.sqlite3",
        scenario_id="app_bad_brand.json",
        exception_approver_subject=APPROVER.subject,
    )
    admitted = service.submit_demo(
        scenario_id="app_bad_brand.json",
        idempotency_key="s05-ws-unclaimed-intake",
        principal=INTEGRATOR,
    )
    assert admitted.application_id is not None
    assert service.process_next_job().status == "complete"
    service.refresh_projection()

    eligibility = _workspace_eligibility(service, admitted.application_id)
    assert eligibility["eligible"] is False
    assert eligibility["request_reason"] is None
    assert eligibility["predecessor_request_id"] is None
    assert eligibility["ineligible_reason_code"] == "STALE_REVIEW_CONTEXT"


def test_workspace_projects_protected_vin_finding_as_ineligible(
    tmp_path: Path,
) -> None:
    service, application_id, _, _, _ = _ready_scenario_finding(
        tmp_path,
        scenario_id="app_s04_bad_vin.json",
        rule_id="R_VIN_CROSS",
    )

    eligibility = _workspace_eligibility(service, application_id)
    assert eligibility["eligible"] is False
    assert eligibility["request_reason"] is None
    assert eligibility["ineligible_reason_code"] == "PROTECTED_CHECK_NOT_WAIVABLE"


def test_workspace_projects_non_waivable_model_finding_as_ineligible(
    tmp_path: Path,
) -> None:
    service, application_id, _, _, _ = _ready_scenario_finding(
        tmp_path,
        scenario_id="app_bad_model.json",
        rule_id="R_MODEL_CROSS",
    )

    eligibility = _workspace_eligibility(service, application_id)
    assert eligibility["eligible"] is False
    assert eligibility["request_reason"] is None
    assert eligibility["ineligible_reason_code"] == (
        "CHECK_NOT_WAIVABLE_BY_PINNED_RELEASE"
    )


def test_workspace_projects_closed_exception_operations_as_ineligible(
    tmp_path: Path,
) -> None:
    service, application_id, _, _, _ = _ready_brand_exception(tmp_path)
    closed = service.close_business_exception_operations(
        principal=ROUTER,
        idempotency_key="s05-ws-close",
        now=101,
    )
    assert closed["status"] == "accepted"

    eligibility = _workspace_eligibility(service, application_id, now=102)
    assert eligibility["eligible"] is False
    assert eligibility["request_reason"] is None
    assert eligibility["ineligible_reason_code"] == (
        "BUSINESS_EXCEPTION_OPERATIONS_CLOSED"
    )


def test_workspace_projects_same_run_rerequest_as_not_material(
    tmp_path: Path,
) -> None:
    service, application_id, work_item_id, claim, finding = _ready_brand_exception(
        tmp_path
    )
    request = _request_brand_exception(service, work_item_id, claim, finding)
    rejection = _reject_brand_exception(service, request)
    assert rejection["status"] == "accepted"
    successor = str(rejection["successor_work_item_id"])
    successor_view = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=successor,
        now=105,
    )
    service.claim_review_work_item(
        principal=REVIEWER,
        work_item_id=successor,
        expected_context=successor_view["command_context"],
        now=105,
    )

    eligibility = _workspace_eligibility(service, application_id, now=105)
    assert eligibility["eligible"] is False
    assert eligibility["request_reason"] is None
    assert eligibility["predecessor_request_id"] is None
    assert eligibility["ineligible_reason_code"] == "EXCEPTION_REREQUEST_NOT_MATERIAL"


def test_workspace_projects_new_run_rerequest_as_eligible_with_predecessor(
    tmp_path: Path,
) -> None:
    service, application_id, work_item_id, claim, finding = _ready_brand_exception(
        tmp_path
    )
    request = _request_brand_exception(service, work_item_id, claim, finding)
    rejection = _reject_brand_exception(service, request)
    successor = str(rejection["successor_work_item_id"])
    successor_view = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=successor,
        now=105,
    )
    successor_claim = service.claim_review_work_item(
        principal=REVIEWER,
        work_item_id=successor,
        expected_context=successor_view["command_context"],
        now=105,
    )
    workspace = service.workspace_view(
        application_id,
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=105,
    )
    brand = next(
        item
        for item in workspace["mandatory_blockers"]
        if item["rule_id"] == "R_BRAND_CROSS"
    )
    source = next(
        link for link in brand["evidence_links"] if link["document_id"] == "pol"
    )
    corrected = service.correct_field_observation(
        principal=REVIEWER,
        application_id=application_id,
        work_item_id=successor,
        expected_fence=int(successor_claim["claim_fence"]),
        expected_context=successor_view["command_context"],
        idempotency_key="s05-ws-rerequest-correction",
        correction={
            "schema_version": "field-observation-correction/1",
            "finding_id": brand["finding_id"],
            "observation_id": source["observation_id"],
            "document_id": source["document_id"],
            "document_role": source["document_role"],
            "field": source["field"],
            "raw": "HONDA",
            "source_location": {
                key: source[key]
                for key in ("source_sha256", "source_page", "source_region")
            },
            "reason_code": "SOURCE_VALUE_MISREAD",
        },
        now=106,
    )
    assert corrected["status"] == "accepted"
    assert service.process_next_job().status == "complete"
    service.refresh_projection()
    queue = service.queue_view(
        role="reviewer",
        scope=REVIEWER.scope,
        subject=REVIEWER.subject,
        now=108,
    )
    new_item = queue["items"][0]
    new_view = service.review_work_item_view(
        principal=REVIEWER,
        work_item_id=str(new_item["work_item_id"]),
        now=108,
    )
    service.claim_review_work_item(
        principal=REVIEWER,
        work_item_id=str(new_item["work_item_id"]),
        expected_context=new_view["command_context"],
        now=108,
    )

    assert _workspace_eligibility(service, application_id, now=108) == {
        "eligible": True,
        "request_reason": "DOCUMENTED_BRAND_VARIANCE",
        "ineligible_reason_code": None,
        "predecessor_request_id": str(request["request_id"]),
    }
