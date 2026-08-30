"""Local-only composition of the existing controlled browser fixtures.

This module is selected by ``scripts/run_web.sh``.  It gives a human a single
process containing the released React shells and representative authorities;
the normal ``task4_consistency.web.app:app`` entry point keeps its production
configuration gates unchanged.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

# Local exhibit identities.  The browser role switcher posts one of these
# credentials; FullDemoAuthMiddleware still fills a default when none is sent.
EXHIBIT_ROLES: list[dict[str, str]] = [
    {
        "id": "reviewer",
        "label": "审核员",
        "duty": "核验结论、人工复核、发起补件或特批",
        "credential": "full-demo-integrator",
        "subject": "s14-reviewer",
    },
    {
        "id": "material",
        "label": "材料岗",
        "duty": "提交新一版附件",
        "credential": "full-demo-source",
        "subject": "full-demo-source-operator",
    },
    {
        "id": "exception-approver",
        "label": "特批人",
        "duty": "对业务例外签字放行或驳回",
        "credential": "full-demo-exception-approver",
        "subject": "full-demo-exception-approver",
    },
    {
        "id": "policy-admin",
        "label": "规则管理员",
        "duty": "维护并发布校验规则",
        "credential": "full-demo-admin",
        "subject": "full-demo-admin",
    },
    {
        "id": "policy-approver",
        "label": "规则审批人",
        "duty": "批准规则版本生效",
        "credential": "full-demo-policy-approver",
        "subject": "full-demo-policy-approver",
    },
    {
        "id": "operator",
        "label": "运营操作员",
        "duty": "投递、取消、终止清算",
        "credential": "full-demo-operator",
        "subject": "full-demo-operator",
    },
    {
        "id": "evaluation",
        "label": "质量管理",
        "duty": "查看覆盖率、误报、漏报",
        "credential": "full-demo-evaluation",
        "subject": "full-demo-evaluation",
    },
    {
        "id": "governance",
        "label": "数据治理",
        "duty": "预览并提交合规删除",
        "credential": "full-demo-governance",
        "subject": "full-demo-governance",
    },
    {
        "id": "exporter",
        "label": "导出申请人",
        "duty": "发起受控导出",
        "credential": "t19-requester-credential",
        "subject": "t19-requester",
    },
]


def create_app():
    import task4_consistency.web.app as web
    from task4_consistency.controlled.s01 import ControlledScenarioService
    from tests.test_t16_react_app import _build_workflows
    from tests.test_t17_react_app import _build_fixture
    from tests.test_t19_react_app import _service as build_export_service
    from tests.test_s02_controlled import TENANT_SCOPE

    state_root = Path(
        os.environ.get("TASK4_FULL_DEMO_ROOT", str(ROOT / "out" / "full_demo"))
    ).resolve()
    state_root.mkdir(parents=True, exist_ok=True)
    # Each process gets a fresh fixture ledger.  A previous browser session
    # can remain on disk for inspection without becoming the next session's
    # input.
    work_root = Path(tempfile.mkdtemp(prefix="session-", dir=state_root))

    # T16 provides one active C-DEMO lifecycle plus a persisted history.  Add
    # the registered-source boundary from T17 to the same SQLite authority so
    # S02 and S14 remain available in one browser session.
    t16_root = work_root / "lifecycle"
    _build_workflows(t16_root)
    t17 = _build_fixture(work_root / "deletion")
    boundary = t17["service"].registered_source_boundary
    main_service = ControlledScenarioService(
        fixture_root=ROOT / "fixtures" / "applications",
        rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        state_path=t16_root / "target.sqlite3",
        registered_sources=boundary._registrations,
        controlled_objects=tuple(boundary._objects.values()),
        controlled_object_absence_store=t16_root / "s02_absence.sqlite3",
    )

    web.S01_SERVICE = main_service
    web.S01_TEST_DRIVER = None
    web.S01_BACKGROUND_ENABLED = False
    web.S01_REQUIRE_CONFIGURED_STARTUP = False
    web.S01_DEMO_CREDENTIAL = "full-demo-integrator"
    web.S01_DEMO_SUBJECT = "s14-reviewer"
    web.S01_OPERATOR_CREDENTIAL = "full-demo-operator"
    web.S01_OPERATOR_SUBJECT = "full-demo-operator"
    web.S01_AUDITOR_CREDENTIAL = "full-demo-auditor"
    web.S01_AUDITOR_SUBJECT = "full-demo-auditor"
    web.S02_CREDENTIAL = "full-demo-source"
    web.S02_SUBJECT = "full-demo-source-operator"
    web.S02_TENANT_ID = "tenant-test"
    web.S02_SOURCE_SYSTEM_ID = "registered-source"
    web.S02_REGISTERED_SOURCES = boundary._registrations
    web.S02_CONTROLLED_OBJECTS = tuple(boundary._objects.values())
    web.S02_CONFIGURED = True
    web.S05_EXCEPTION_APPROVER_CREDENTIAL = "full-demo-exception-approver"
    web.S05_EXCEPTION_APPROVER_SUBJECT = "full-demo-exception-approver"
    web.S13_OPERATOR_CREDENTIAL = "full-demo-delivery-operator"
    web.S13_OPERATOR_SUBJECT = "full-demo-operator"
    web.S13_OPERATOR_SCOPE = "C-DEMO"

    # S08/S09 use their own governance ledger while resolving lifecycle reads
    # through the live S01 service above.
    web.S08_ADMIN_CREDENTIAL = "full-demo-admin"
    web.S08_ADMIN_SUBJECT = "full-demo-admin"
    web.S08_APPROVER_CREDENTIAL = "full-demo-policy-approver"
    web.S08_APPROVER_SUBJECT = "full-demo-policy-approver"
    web.S08_OPERATOR_CREDENTIAL = "full-demo-policy-operator"
    web.S08_OPERATOR_SUBJECT = "full-demo-policy-operator"
    web.S01_AUDITOR_CREDENTIAL = "full-demo-auditor"
    web.S01_AUDITOR_SUBJECT = "full-demo-auditor"
    web.S09_REPLAY_CREDENTIAL = "full-demo-replay"
    web.S09_REPLAY_SUBJECT = "full-demo-replay"
    web.S09_SIMULATION_CREDENTIAL = "full-demo-simulation"
    web.S09_SIMULATION_SUBJECT = "full-demo-simulation"
    web.S08_CONFIGURED = True
    web.S09_GOVERNANCE_CONFIGURED = True
    web.S08_SERVICE = web.PolicyGovernanceService(
        state_path=work_root / "governance.sqlite3",
        audit_available=True,
        storage_available=True,
        migration_admin_subject="full-demo-migration-admin",
        admin_subject=web.S08_ADMIN_SUBJECT,
        approver_subject=web.S08_APPROVER_SUBJECT,
        operator_subject=web.S08_OPERATOR_SUBJECT,
        source_rules_path=ROOT / "configs" / "rules_auto_lease.yaml",
        source_kb_path=ROOT / "configs" / "kb" / "entity_kb.json",
        corpus_root=ROOT / "fixtures" / "applications",
        clock=lambda: 1_800_000_000,
        lifecycle_snapshot_provider=web._s09_lifecycle_impact_snapshot,
        diagnostic_snapshot_provider=web._s09_lifecycle_diagnostic_snapshot,
    )
    web.S08_SERVICE.bootstrap_once()

    # T17's empty evaluation plane is sufficient for the operator shell and
    # keeps evaluation state isolated from the lifecycle and deletion ledgers.
    web.S12_SERVICE = t17["evaluation"]
    web.S12_CREDENTIAL = "full-demo-evaluation"
    web.S12_SUBJECT = "full-demo-evaluation"
    web.S12_WORKER_SUBJECT = "full-demo-evaluation-worker"
    web.S12_CONFIGURATION_ERROR = None

    web.S16_SERVICE = t17["s16"]
    web.S16_CONFIGURED = True
    web.S16_CONFIGURATION_ERROR = None
    web.S16_GOVERNANCE_CREDENTIAL = "full-demo-governance"
    web.S16_GOVERNANCE_SUBJECT = "full-demo-governance"
    web.S16_APPROVER1_CREDENTIAL = "full-demo-deletion-approver-1"
    web.S16_APPROVER1_SUBJECT = "full-demo-deletion-approver-1"
    web.S16_APPROVER2_CREDENTIAL = "full-demo-deletion-approver-2"
    web.S16_APPROVER2_SUBJECT = "full-demo-deletion-approver-2"
    web.S16_GOVERNANCE_SCOPE = "R-OBSERVED/tenant-test"

    web.S17_SERVICE = build_export_service(work_root / "export")
    web.S17_CONFIGURATION_ERROR = None
    web.S17_REQUESTER_CREDENTIAL = "t19-requester-credential"
    web.S17_REQUESTER_SUBJECT = "t19-requester"
    web.S17_APPROVER_CREDENTIAL = "t19-approver-credential"
    web.S17_APPROVER_SUBJECT = "t19-approver"
    web.S17_WORKER_CREDENTIAL = "t19-worker-credential"
    web.S17_WORKER_SUBJECT = "t19-worker"
    web.S17_RECIPIENT_CREDENTIAL = "t19-recipient-credential"
    web.S17_RECIPIENT_SUBJECT = "s17-recipient-1"
    web.S17_EXPORT_SCOPE = "C-DEMO"

    fixture_path = t16_root / "fixture.json"
    if fixture_path.is_file():
        payload = json.loads(fixture_path.read_text(encoding="utf-8"))
        payload["deletion_reference"] = t17["reference"]
        payload["deletion_application_id"] = t17["application_id"]
        payload["exhibit_roles"] = EXHIBIT_ROLES
        fixture_path.write_text(
            json.dumps(payload, sort_keys=True, ensure_ascii=False),
            encoding="utf-8",
        )
        web.DEMO_DIRECTORY_PATH = fixture_path

    os.environ["TASK4_FULL_DEMO"] = "1"
    os.environ["TASK4_EXHIBIT"] = "1"
    return web.app
