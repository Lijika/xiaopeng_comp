from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "configs" / "s02_source_conformance.json"


def _shape(payload: dict[str, Any]) -> str | None:
    if "per_image_results" in payload:
        return "ocr-detection/unversioned"
    if payload.get("schema") == "task4.external_ocr_slots.v1":
        return "step2-slots/v1"
    if "pages" in payload:
        return "step2-page-order/unversioned"
    if payload.get("schema_version") == 1 and "documents" in payload:
        return "external-ocr/v1"
    if "fields" in payload:
        return "ocr-aggregate/unversioned"
    return None


def _occurrences(shape: str, payload: dict[str, Any]) -> int:
    if shape == "step2-page-order/unversioned":
        return sum(len(page.get("detections") or []) for page in payload.get("pages") or [])
    if shape == "step2-slots/v1":
        return len(payload.get("slots") or [])
    if shape == "ocr-detection/unversioned":
        return sum(
            len(page.get("detections") or [])
            for page in payload.get("per_image_results") or []
        )
    if shape == "external-ocr/v1":
        return sum(len(document.get("fields") or {}) for document in payload.get("documents") or [])
    fields = payload.get("fields") or {}
    if isinstance(fields, dict) and isinstance(fields.get("fields"), dict):
        fields = fields["fields"]
    return sum(
        len(value.get("sources") or [])
        for value in fields.values()
        if isinstance(value, dict)
    )


def _manifest() -> dict[str, Any]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_conformance_manifest_registers_observed_shapes_and_legacy_callers() -> None:
    manifest = _manifest()
    shapes = {item["source_shape"] for item in manifest["registered_shapes"]}

    assert manifest["schema_version"] == "s02-source-conformance/1"
    assert manifest["adapter_build"] == "s02-registered-source-adapters/1"
    assert shapes == {
        "step2-page-order/unversioned",
        "step2-slots/v1",
        "ocr-aggregate/unversioned",
        "ocr-detection/unversioned",
        "external-ocr/v1",
    }
    assert {item["legacy_id"] for item in manifest["caller_reconciliation"]} == {
        "A07",
        "A08",
        "A09",
    }
    assert all(
        item["legacy_target_authority_writes"] is False
        for item in manifest["caller_reconciliation"]
    )
    assert all(item["unsupported_facts"] for item in manifest["registered_shapes"])


def test_environment_materials_have_registered_shapes_and_no_loss_records() -> None:
    configured = os.environ.get("TASK4_S02_CONFORMANCE_ROOTS")
    if not configured:
        pytest.skip("S02 conformance roots are not configured")
    roots = [Path(value) for value in configured.split(os.pathsep) if value]
    manifest = _manifest()
    registrations = {
        item["source_shape"]: item for item in manifest["registered_shapes"]
    }
    records: list[tuple[str, str, int]] = []
    unknown_shapes = 0
    unknown_keys = 0
    observed_shapes: set[str] = set()

    for root in roots:
        for source in sorted(root.rglob("*.json")):
            source_bytes = source.read_bytes()
            payload = json.loads(source_bytes.decode("utf-8"))
            shape = _shape(payload) if isinstance(payload, dict) else None
            if shape is None or shape not in registrations:
                unknown_shapes += 1
                continue
            registration = registrations[shape]
            known = set(registration["accepted_top_level"]) | set(
                registration["unsupported_top_level"]
            )
            unknown_keys += len(set(payload).difference(known))
            count = _occurrences(shape, payload)
            if count < 1:
                unknown_shapes += 1
                continue
            records.append((shape, hashlib.sha256(source_bytes).hexdigest(), count))
            observed_shapes.add(shape)
            assert source.read_bytes() == source_bytes

    expected = {
        value
        for value in os.environ.get("TASK4_S02_CONFORMANCE_EXPECTED_SHAPES", "").split(",")
        if value
    }
    aggregate = hashlib.sha256(
        json.dumps(records, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()
    assert records
    assert unknown_shapes == 0
    assert unknown_keys == 0
    assert not expected or observed_shapes == expected
    assert len(aggregate) == 64


S10_STEP2_DIR = ROOT / "data" / "step2"

# The complete content-ordered S10 Step2 corpus fact rows.  This is the exact
# expected content (not a digest-length check): ten page_order files, forty-
# four pages, 335 detections, 33 ``登记页``, 11 ``注册页`` and zero inferred
# pages.  Each row is [filename, page_index, page_type, page_ordinal,
# detection_count, inferred].
S10_STEP2_CONTENT = [
    ["JFL25P02L080310-01_page_order.json", 0, "注册页", 1, 16, False],
    ["JFL25P02L080310-01_page_order.json", 1, "注册页", 2, 16, False],
    ["JFL25P02L080310-01_page_order.json", 2, "登记页", 3, 5, False],
    ["JFL25P02L080310-01_page_order.json", 3, "登记页", 4, 5, False],
    ["JFL25P02L080310-01_page_order.json", 4, "登记页", 5, 5, False],
    ["JFL25P02L080310-01_page_order.json", 5, "登记页", 6, 5, False],
    ["JFL25P02L080310-01_page_order.json", 6, "登记页", 7, 4, False],
    ["JFL25P02L086208-01_page_order.json", 0, "注册页", 1, 17, False],
    ["JFL25P02L086208-01_page_order.json", 1, "登记页", 2, 5, False],
    ["JFL25P02L086208-01_page_order.json", 2, "登记页", 3, 4, False],
    ["JFL25P02L089660-01_page_order.json", 0, "注册页", 1, 16, False],
    ["JFL25P02L089660-01_page_order.json", 1, "登记页", 2, 5, False],
    ["JFL25P02L089660-01_page_order.json", 2, "登记页", 3, 5, False],
    ["JFL25P02L089660-01_page_order.json", 3, "登记页", 4, 4, False],
    ["JFL25P02L092898-01_page_order.json", 0, "注册页", 1, 18, False],
    ["JFL25P02L092898-01_page_order.json", 1, "登记页", 2, 5, False],
    ["JFL25P02L092898-01_page_order.json", 2, "登记页", 3, 6, False],
    ["JFL25P02L092898-01_page_order.json", 3, "登记页", 4, 4, False],
    ["JFL25P02L092898-01_page_order.json", 4, "登记页", 5, 2, False],
    ["JFL25P02L096143-01_page_order.json", 0, "注册页", 1, 18, False],
    ["JFL25P02L096143-01_page_order.json", 1, "登记页", 2, 5, False],
    ["JFL25P02L096143-01_page_order.json", 2, "登记页", 3, 5, False],
    ["JFL25P02L096143-01_page_order.json", 3, "登记页", 4, 4, False],
    ["JFL25P02L099690-01_page_order.json", 0, "注册页", 1, 18, False],
    ["JFL25P02L099690-01_page_order.json", 1, "登记页", 2, 6, False],
    ["JFL25P02L099690-01_page_order.json", 2, "登记页", 3, 6, False],
    ["JFL25P02L099690-01_page_order.json", 3, "登记页", 4, 4, False],
    ["JFL25P02L099690-01_page_order.json", 4, "登记页", 5, 2, False],
    ["JFL25P02L102655-01_page_order.json", 0, "注册页", 1, 16, False],
    ["JFL25P02L102655-01_page_order.json", 1, "登记页", 2, 5, False],
    ["JFL25P02L102655-01_page_order.json", 2, "登记页", 3, 4, False],
    ["JFL25P02L102655-01_page_order.json", 3, "登记页", 4, 4, False],
    ["JFL26P02L001460-01_page_order.json", 0, "注册页", 1, 17, False],
    ["JFL26P02L001460-01_page_order.json", 1, "登记页", 2, 5, False],
    ["JFL26P02L001460-01_page_order.json", 2, "登记页", 3, 5, False],
    ["JFL26P02L001460-01_page_order.json", 3, "登记页", 4, 4, False],
    ["JFL26P02L004481-01_page_order.json", 0, "注册页", 1, 17, False],
    ["JFL26P02L004481-01_page_order.json", 1, "登记页", 2, 5, False],
    ["JFL26P02L004481-01_page_order.json", 2, "登记页", 3, 5, False],
    ["JFL26P02L004481-01_page_order.json", 3, "登记页", 4, 4, False],
    ["JFL26P02L006588-01_page_order.json", 0, "注册页", 1, 17, False],
    ["JFL26P02L006588-01_page_order.json", 1, "登记页", 2, 5, False],
    ["JFL26P02L006588-01_page_order.json", 2, "登记页", 3, 5, False],
    ["JFL26P02L006588-01_page_order.json", 3, "登记页", 4, 2, False],
]

# Full aggregate sha256 over the 44 mapped candidate claims (content equality,
# never a digest-length check).
S10_MEMBERSHIP_CLAIMS_DIGEST = (
    "7e3a737ecd7efca6c86abce92eef9cc19ea838cff0d693ee17807df000a9eacd"
)


def test_s10_step2_page_membership_migration_no_loss() -> None:
    from task4_consistency.controlled.s02 import step2_page_order_membership_claims

    # The corpus-level migration pins candidate-claim counts and provenance.
    # The Step2 corpus ships no image objects, so attachment/content identity
    # remains explicitly unknown while the original filename/pointer survives.
    # Production canonicalize binds the same claims to the admitted
    # attachment reference/hash via ``resolve_page_binding``, which the S10
    # HTTP/controlled and browser (Playwright) evidence exercise end to end.
    files = sorted(S10_STEP2_DIR.glob("*_page_order.json"))
    records: list[list[Any]] = []
    claims: list[dict[str, Any]] = []
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        sample_id = str(payload.get("sample_id") or path.stem)
        claims.extend(
            step2_page_order_membership_claims(payload, application_id=sample_id)
        )
        pages = payload.get("pages") or []
        for index, page in enumerate(pages):
            records.append(
                [
                    path.name,
                    index,
                    page.get("page_type"),
                    int(page.get("order") or index + 1),
                    len(page.get("detections") or []),
                    page.get("inferred") is True,
                ]
            )

    # Exact content equality of the corpus fact rows.
    assert records == S10_STEP2_CONTENT
    assert len(files) == 10
    assert len(records) == 44
    assert sum(row[4] for row in records) == 335
    assert sum(row[2] == "登记页" for row in records) == 33
    assert sum(row[2] == "注册页" for row in records) == 11
    assert sum(row[5] for row in records) == 0
    # Exactly one provenance-bearing candidate claim per non-inferred page.
    assert len(claims) == 44
    assert all(claim["record_kind"] == "candidate" for claim in claims)
    assert all(claim["page"]["source_sha256"] == "unknown" for claim in claims)
    assert all(claim["page"]["attachment_id"] == "unknown" for claim in claims)
    assert all(
        claim["candidate_document"]
        == {"document_instance_id": "unknown", "document_role": "unknown"}
        for claim in claims
    )
    assert all(claim["provenance"]["fact"] == "page.page_type" for claim in claims)
    assert all(claim["provenance"]["source_filename"] for claim in claims)
    assert all(
        claim["provenance"]["source_pointer"].startswith("/pages/")
        for claim in claims
    )
    assert sum(claim["provenance"]["page_type"] == "登记页" for claim in claims) == 33
    assert sum(claim["provenance"]["page_type"] == "注册页" for claim in claims) == 11
    ordered = sorted(claims, key=lambda claim: (claim["page"]["page_ordinal"], claim["claim_id"]))
    aggregate = hashlib.sha256(
        json.dumps(
            ordered, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    assert aggregate == S10_MEMBERSHIP_CLAIMS_DIGEST
    # No inferred page may ever produce a claim.
    assert all(claim["provenance"]["inferred"] is False for claim in claims)
