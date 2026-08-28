"""Registered real-source verification and deterministic S02 adaptation.

This module has no persistence or lifecycle authority. It verifies registered
source objects and translates one observed producer shape into the canonical
evidence envelope owned by :mod:`task4_consistency.controlled.s01`.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import sqlite3
import struct
import time
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from task4_consistency.controlled.s16 import (
    COPY_CLASS_DERIVED_OBJECT,
    copy_identity_fingerprint,
)


ENVELOPE_VERSION = "registered-observation-envelope/1"
SCHEMA_VERSION = "1.0.0"
SEMANTIC_VERSION = "1.0.0"
ADAPTER_BUILD = "s02-registered-source-adapters/1"
R_OBSERVED_SCOPE_PREFIX = "R-OBSERVED/"
RUNTIME_REGISTRY_SCHEMA = "s02-runtime-registry/1"
_MAX_REGISTRY_BYTES = 1024 * 1024
_MAX_DECODED_IMAGE_BYTES = 128 * 1024 * 1024

_CANONICAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_BANNED_TRANSPORT_KEYS = {
    "base64",
    "callback",
    "credential",
    "credentials",
    "endpoint",
    "file_path",
    "locator",
    "password",
    "path",
    "rule_path",
    "secret",
    "token",
    "url",
    "uri",
}
_BANNED_LABEL_KEYS = {
    "expected",
    "expected_verdict",
    "expected_verdicts",
    "ground_truth",
    "label",
    "labels",
    "target_verdict",
}
_SUBMISSION_KEYS = {
    "attachments",
    "command_type",
    "document_binding",
    "envelope_id",
    "must_understand",
    "predecessor_revision",
    "producer",
    "result_object",
    "schema_version",
    "semantic_version",
    "source_revision",
    "stream_id",
    "upstream_application_ref",
    "workload_identity_id",
}
_ATTACHMENT_VERSION_SUBMISSION_KEYS = _SUBMISSION_KEYS | {
    "attachment_lineage",
    "batch",
    "request_binding",
}
_REQUEST_BINDING_KEYS = {
    "material_requirement_id",
    "request_context_digest",
    "request_progress_revision",
    "supplement_request_id",
}
_ATTACHMENT_LINEAGE_KEYS = {
    "attachment_version",
    "operation",
    "predecessor_attachment_id",
    "predecessor_attachment_version",
}
_REQUEST_BATCH_KEYS = {
    "batch_id",
    "closed",
    "final_sequence",
    "item_count",
    "item_sequence",
    "manifest_digest",
    "scope_mode",
}
_DOCUMENT_BINDING_KEYS = {
    "document_role",
    "document_type",
    "source_document_ref",
}
_OBJECT_DESCRIPTOR_KEYS = {
    "controlled_object_ref",
    "media_type",
    "sha256",
    "size_bytes",
}
_ATTACHMENT_KEYS = {
    "object",
    "page_ordinal",
    "page_ref",
    "source_attachment_ref",
    "source_name_sha256",
}
_PRODUCER_KEYS = {
    "confidence_semantics",
    "coordinate_system",
    "model_id",
    "model_version",
    "producer_family",
    "producer_id",
    "run_id",
    "task_id",
    "task_version",
}
_COORDINATE_SYSTEM_KEYS = {"name", "origin", "unit"}
_CONFIDENCE_SEMANTICS_KEYS = {
    "calibration",
    "granularity",
    "higher_is",
    "maximum",
    "meaning",
    "minimum",
}
_FIELD_ALIASES = {
    "vin": "vin",
    "vehicle_identifier": "vin",
    "车辆识别代号": "vin",
    "车辆识别代号/车架号": "vin",
    "engine_no": "engine_no",
    "发动机号": "engine_no",
    "reg_cert_no": "reg_cert_no",
    "登记证书编号": "reg_cert_no",
    "plate_no": "plate_no",
    "号牌号码": "plate_no",
    "owner_name": "owner_name",
    "所有人": "owner_name",
    "姓名": "owner_name",
    "address": "address",
    "住址": "address",
    "reg_date": "reg_date",
    "登记日期": "reg_date",
    "brand": "brand",
    "车辆品牌": "brand",
    "model": "model",
    "车辆型号": "model",
}
_PAGE_ORDER_ROOT_KEYS = {"generated_time", "pages", "sample_id", "statistics", "warnings"}
_PAGE_ORDER_PAGE_KEYS = {
    "detections",
    "filename",
    "has_rec_marker",
    "has_reg_marker",
    "inferred",
    "order",
    "page_numbers",
    "page_type",
}
_PAGE_ORDER_DETECTION_KEYS = {
    "bbox",
    "class_id",
    "class_name",
    "class_name_cn",
    "confidence",
}
_SLOTS_ROOT_KEYS = {"doc_type", "n_slots", "note", "sample_id", "schema", "slots", "source"}
_SLOT_KEYS = {
    "bbox",
    "class_name_cn",
    "confidence_det",
    "field",
    "image_filename",
    "page_order",
    "page_type",
    "raw",
    "zip_member",
}
_AGGREGATE_ROOT_KEYS = {"fields", "sample_id", "statistics", "warnings"}
_AGGREGATE_FIELD_KEYS = {"consistent", "sources", "value"}
_AGGREGATE_SOURCE_KEYS = {"filename"}
_DETECTION_ROOT_KEYS = {
    "generated_time",
    "per_image_results",
    "sample_id",
    "statistics",
    "warnings",
}
_DETECTION_PAGE_KEYS = {"detections", "image_path", "image_size"}
_DETECTION_SIZE_KEYS = {"height", "width"}
_DETECTION_KEYS = {
    "bbox",
    "class_id",
    "class_name",
    "class_name_cn",
    "confidence",
    "field_key",
    "ocr_text",
    "value",
}
_EXTERNAL_ROOT_KEYS = {
    "application_id",
    "documents",
    "expected_verdicts",
    "label",
    "ocr_model",
    "ocr_version",
    "schema_version",
}
_EXTERNAL_DOCUMENT_KEYS = {"doc_id", "doc_type", "fields"}
_EXTERNAL_FIELD_KEYS = {"bbox", "confidence", "field_type", "raw", "source_page"}


@dataclass(frozen=True)
class RegisteredSource:
    tenant_id: str
    source_system_id: str
    workload_identity_id: str
    adapter_id: str
    adapter_version: str
    source_shape: str
    producer_family: str
    enabled: bool = True
    max_result_bytes: int = 2 * 1024 * 1024
    max_attachment_bytes: int = 32 * 1024 * 1024
    max_pages: int = 64
    max_observations: int = 10_000
    allowed_media_types: tuple[str, ...] = ("image/jpeg", "image/png")


@dataclass(frozen=True)
class ControlledObject:
    tenant_id: str
    source_system_id: str
    object_ref: str
    media_type: str
    content: bytes = field(repr=False)


@dataclass(frozen=True)
class S02CanonicalEnvelope:
    envelope_version: str
    schema_version: str
    semantic_version: str
    command_type: str
    upstream_application_reference: str
    envelope_id: str
    stream_id: str
    source_revision: int
    predecessor_revision: int | None
    fingerprint: str
    payload: dict[str, Any]
    adapter_id: str
    adapter_version: str
    registration_digest: str
    observation_count: int
    attachment_count: int
    provenance_eligible: bool


class S02IntakeError(ValueError):
    def __init__(
        self,
        disposition: str,
        reason_code: str,
        *,
        responsible_party: str,
        recovery_action: str,
        retryable: bool = False,
        gate_results: tuple[str, ...] = (),
        adapter_id: str | None = None,
        adapter_version: str | None = None,
        registration_digest: str | None = None,
    ) -> None:
        self.disposition = disposition
        self.reason_code = reason_code
        self.responsible_party = responsible_party
        self.recovery_action = recovery_action
        self.retryable = retryable
        self.gate_results = gate_results
        self.adapter_id = adapter_id
        self.adapter_version = adapter_version
        self.registration_digest = registration_digest
        super().__init__(reason_code)


def load_runtime_registry(
    registry_path: str | Path,
    object_root: str | Path,
) -> tuple[tuple[RegisteredSource, ...], tuple[ControlledObject, ...]]:
    """Load one deployment-owned source registry without exposing file locators."""
    registry = Path(registry_path)
    root = Path(object_root)
    if (
        not registry.is_absolute()
        or not root.is_absolute()
        or not registry.is_file()
        or not root.is_dir()
        or registry.stat().st_size > _MAX_REGISTRY_BYTES
    ):
        raise ValueError("S02 runtime registry configuration is invalid")
    try:
        payload = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("S02 runtime registry configuration is invalid") from error
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "sources", "objects"}
        or payload.get("schema_version") != RUNTIME_REGISTRY_SCHEMA
        or not isinstance(payload.get("sources"), list)
        or not payload["sources"]
        or not isinstance(payload.get("objects"), list)
        or not payload["objects"]
    ):
        raise ValueError("S02 runtime registry contract is invalid")

    source_keys = {
        "adapter_id",
        "adapter_version",
        "allowed_media_types",
        "enabled",
        "max_attachment_bytes",
        "max_observations",
        "max_pages",
        "max_result_bytes",
        "producer_family",
        "source_shape",
        "source_system_id",
        "tenant_id",
        "workload_identity_id",
    }
    required_source_keys = {
        "adapter_id",
        "adapter_version",
        "producer_family",
        "source_shape",
        "source_system_id",
        "tenant_id",
        "workload_identity_id",
    }
    registrations: list[RegisteredSource] = []
    for value in payload["sources"]:
        if (
            not isinstance(value, dict)
            or not required_source_keys.issubset(value)
            or not set(value).issubset(source_keys)
        ):
            raise ValueError("S02 runtime source registration is invalid")
        values = dict(value)
        if "allowed_media_types" in values:
            media = values["allowed_media_types"]
            if not isinstance(media, list) or not media:
                raise ValueError("S02 runtime source registration is invalid")
            values["allowed_media_types"] = tuple(media)
        try:
            registration = RegisteredSource(**values)
        except TypeError as error:
            raise ValueError("S02 runtime source registration is invalid") from error
        limits = (
            registration.max_result_bytes,
            registration.max_attachment_bytes,
            registration.max_pages,
            registration.max_observations,
        )
        identifiers = (
            registration.tenant_id,
            registration.source_system_id,
            registration.workload_identity_id,
            registration.adapter_id,
            registration.adapter_version,
            registration.producer_family,
        )
        if (
            not all(_valid_id(item) for item in identifiers)
            or not isinstance(registration.source_shape, str)
            or not registration.source_shape
            or registration.source_shape.strip() != registration.source_shape
            or len(registration.source_shape) > 200
            or type(registration.enabled) is not bool
            or any(type(limit) is not int or limit < 1 for limit in limits)
            or any(
                not isinstance(media, str) or not media
                for media in registration.allowed_media_types
            )
        ):
            raise ValueError("S02 runtime source registration is invalid")
        registrations.append(registration)

    resolved_root = root.resolve()
    object_keys = {
        "file",
        "media_type",
        "object_ref",
        "source_system_id",
        "tenant_id",
    }
    objects: list[ControlledObject] = []
    for value in payload["objects"]:
        if not isinstance(value, dict) or set(value) != object_keys:
            raise ValueError("S02 runtime object registration is invalid")
        relative_file = value.get("file")
        if (
            not isinstance(relative_file, str)
            or not relative_file
            or Path(relative_file).is_absolute()
        ):
            raise ValueError("S02 runtime object registration is invalid")
        source = (resolved_root / relative_file).resolve()
        if resolved_root not in source.parents or not source.is_file():
            raise ValueError("S02 runtime object registration is invalid")
        matching = [
            item
            for item in registrations
            if item.tenant_id == value.get("tenant_id")
            and item.source_system_id == value.get("source_system_id")
        ]
        media_type = value.get("media_type")
        if not matching or not isinstance(media_type, str) or not media_type:
            raise ValueError("S02 runtime object registration is invalid")
        maximum = max(
            item.max_result_bytes
            if media_type == "application/json"
            else item.max_attachment_bytes
            for item in matching
        )
        if source.stat().st_size < 1 or source.stat().st_size > maximum:
            raise ValueError("S02 runtime object exceeds its registered limit")
        try:
            content = source.read_bytes()
        except OSError as error:
            raise ValueError("S02 runtime object is unavailable") from error
        objects.append(
            ControlledObject(
                tenant_id=str(value["tenant_id"]),
                source_system_id=str(value["source_system_id"]),
                object_ref=str(value["object_ref"]),
                media_type=media_type,
                content=content,
            )
        )
    return tuple(registrations), tuple(objects)


class RegisteredSourceBoundary:
    """Small, stateless-facing boundary for registered source translation."""

    def __init__(
        self,
        registrations: Iterable[RegisteredSource] = (),
        objects: Iterable[ControlledObject] = (),
        absence_store_path: str | Path | None = None,
    ) -> None:
        self._registrations = tuple(registrations)
        self._objects: dict[str, ControlledObject] = {}
        for item in objects:
            if item.object_ref in self._objects:
                raise ValueError("controlled object references must be unique")
            if not isinstance(item.content, bytes):
                raise ValueError("controlled object content must be bytes")
            self._objects[item.object_ref] = item
        self.absence_store_path = (
            str(absence_store_path) if absence_store_path is not None else None
        )
        self._absent_fingerprints: set[str] = set()
        if self.absence_store_path is not None:
            self._ensure_absence_schema()
            self._load_absence()
        manifest = {
            "schema_version": "s02-source-registry/1",
            "adapter_build": ADAPTER_BUILD,
            "registrations": [asdict(item) for item in self._registrations],
        }
        self.manifest_digest = _digest(manifest)

    # -- S16 governed-deletion absence seam ------------------------------
    # The absence store is a small SQLite ledger owned by the S02 boundary
    # (an S16 orchestration concern, kept outside the business backup): a
    # deleted object's content fingerprint persists there, so a process
    # restart that re-registers the same objects from the runtime registry
    # still fails every direct object read and proves absence.

    def _ensure_absence_schema(self) -> None:
        if self.absence_store_path is None:
            return
        Path(self.absence_store_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.absence_store_path, timeout=10.0) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS s02_object_absence (
                    fingerprint TEXT PRIMARY KEY,
                    deleted_at INTEGER NOT NULL,
                    schema_version TEXT NOT NULL
                )
                """
            )

    def _load_absence(self) -> None:
        if self.absence_store_path is None:
            return
        with sqlite3.connect(self.absence_store_path, timeout=10.0) as connection:
            rows = connection.execute(
                "SELECT fingerprint FROM s02_object_absence"
            ).fetchall()
        self._absent_fingerprints = {str(row[0]) for row in rows}

    def _persist_absence(self, fingerprint: str, *, deleted_at: int) -> None:
        if self.absence_store_path is None:
            return
        with sqlite3.connect(self.absence_store_path, timeout=10.0) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO s02_object_absence("
                "fingerprint, deleted_at, schema_version) VALUES (?, ?, ?)",
                (fingerprint, deleted_at, "s02-object-absence/1"),
            )

    @staticmethod
    def _object_fingerprint(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    @classmethod
    def _identity_fingerprint(cls, content: bytes) -> str:
        """The S16 copy identity fingerprint of one registered object."""
        return copy_identity_fingerprint(
            "s02", COPY_CLASS_DERIVED_OBJECT, cls._object_fingerprint(content)
        )

    def s02_inventory(self) -> dict[str, Any]:
        """Value-free object inventory: content digest + count per object."""
        return {
            "schema_version": "s02-object-inventory/1",
            "objects": [
                {
                    "content_sha256": self._object_fingerprint(item.content),
                    "count": 1,
                }
                for item in self._objects.values()
                if self._identity_fingerprint(item.content)
                not in self._absent_fingerprints
            ],
        }

    def s02_delete(self, fingerprints: Iterable[str]) -> dict[str, Any]:
        """Persist absence for the named copy identity fingerprints and drop
        the live mappings (monotonic: absent is already a success)."""
        deleted = 0
        now = int(time.time())
        target = set(fingerprints)
        removed: list[str] = []
        for object_ref, item in list(self._objects.items()):
            fingerprint = self._identity_fingerprint(item.content)
            if fingerprint in target:
                removed.append(object_ref)
                if fingerprint not in self._absent_fingerprints:
                    self._absent_fingerprints.add(fingerprint)
                    self._persist_absence(fingerprint, deleted_at=now)
                    deleted += 1
        for object_ref in removed:
            del self._objects[object_ref]
        return {"status": "complete", "deleted_counts": {"derived_object": deleted}}

    def s02_verify_absent(self, fingerprints: Iterable[str]) -> dict[str, Any]:
        target = set(fingerprints)
        live = {
            self._identity_fingerprint(item.content)
            for item in self._objects.values()
        }
        absent = bool(target) and target.issubset(self._absent_fingerprints) and not (
            target & live
        )
        return {"absent": absent, "absent_count": len(target & self._absent_fingerprints)}

    def s02_replay(self, fingerprints: Iterable[str]) -> dict[str, Any]:
        """Restore-time replay: idempotently re-persist absence and drop any
        live mapping for the named fingerprints."""
        now = int(time.time())
        target = set(fingerprints)
        removed: list[str] = []
        for object_ref, item in list(self._objects.items()):
            fingerprint = self._identity_fingerprint(item.content)
            if fingerprint in target:
                removed.append(object_ref)
                self._absent_fingerprints.add(fingerprint)
                self._persist_absence(fingerprint, deleted_at=now)
        for object_ref in removed:
            del self._objects[object_ref]
        return {"status": "replayed"}

    def s02_verify_repair(self, repair_fact: str) -> bool:
        if repair_fact != "s02-repair-verified":
            return False
        try:
            if self.absence_store_path is not None:
                self._ensure_absence_schema()
                self._load_absence()
            return True
        except Exception:
            return False

    @staticmethod
    def command_fingerprint(submission: Any) -> str:
        try:
            encoded = json.dumps(
                submission,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise S02IntakeError(
                "rejected",
                "intake.schema_unsupported",
                responsible_party="integrator",
                recovery_action="submit_a_valid_json_contract",
                gate_results=("identity:verified", "contract:failed"),
            ) from error
        return hashlib.sha256(encoded).hexdigest()

    def read_object(
        self,
        *,
        tenant_id: str,
        source_system_id: str,
        object_ref: str,
    ) -> bytes:
        """Read one currently configured object through its registered authority."""
        item = self._objects.get(object_ref)
        if (
            item is None
            or item.tenant_id != tenant_id
            or item.source_system_id != source_system_id
        ):
            raise LookupError("controlled object is unavailable")
        if self._identity_fingerprint(item.content) in self._absent_fingerprints:
            raise LookupError("controlled object is unavailable")
        return item.content

    def canonicalize(
        self,
        submission: Any,
        *,
        scope: str,
        source_system_id: str,
    ) -> S02CanonicalEnvelope:
        if not isinstance(submission, dict):
            self._fail("rejected", "intake.schema_unsupported", "integrator", "submit_a_valid_json_contract", gates=("identity:verified", "contract:failed"))
        tenant_id = tenant_from_scope(scope)
        workload_id = _required_id(submission, "workload_identity_id")
        matches = [
            registration
            for registration in self._registrations
            if registration.tenant_id == tenant_id
            and registration.source_system_id == source_system_id
            and registration.workload_identity_id == workload_id
        ]
        if len(matches) != 1:
            reason = (
                "adapter.mapping_ambiguous"
                if len(matches) > 1
                else "intake.source_disabled"
            )
            disposition = "quarantined" if len(matches) > 1 else "rejected"
            self._fail(
                disposition,
                reason,
                "integration_owner",
                "repair_source_registration",
                gates=("identity:verified", "tenant_source_binding:failed"),
            )
        registration = matches[0]
        registration_digest = _digest(asdict(registration))
        failure_context = {
            "adapter_id": registration.adapter_id,
            "adapter_version": registration.adapter_version,
            "registration_digest": registration_digest,
        }
        if not registration.enabled:
            self._fail(
                "rejected",
                "intake.source_disabled",
                "integration_owner",
                "enable_or_migrate_the_registered_source",
                gates=("identity:verified", "tenant_source_binding:failed"),
                **failure_context,
            )

        self._validate_transport(submission, failure_context)
        self._require_contract_keys(
            submission,
            allowed=_SUBMISSION_KEYS,
            required=_SUBMISSION_KEYS,
            failure_context=failure_context,
        )
        if submission.get("schema_version") != SCHEMA_VERSION:
            self._fail(
                "rejected",
                "intake.schema_unsupported",
                "integrator",
                "submit_a_supported_schema_version",
                gates=("identity:verified", "contract:failed"),
                **failure_context,
            )
        if submission.get("semantic_version") != SEMANTIC_VERSION:
            self._fail(
                "rejected",
                "intake.semantic_version_unsupported",
                "integrator",
                "submit_a_supported_semantic_version",
                gates=("identity:verified", "contract:failed"),
                **failure_context,
            )
        if submission.get("command_type") != "submit_observation_result":
            self._fail(
                "rejected",
                "intake.schema_unsupported",
                "integrator",
                "submit_a_supported_command",
                gates=("identity:verified", "contract:failed"),
                **failure_context,
            )
        must_understand = submission.get("must_understand")
        if must_understand != []:
            self._fail(
                "rejected",
                "intake.must_understand_unsupported",
                "integrator",
                "remove_unsupported_required_extensions",
                gates=("identity:verified", "contract:failed"),
                **failure_context,
            )

        envelope_id = _required_id(submission, "envelope_id")
        upstream_ref = _required_id(submission, "upstream_application_ref")
        stream_id = _required_id(submission, "stream_id")
        source_revision = submission.get("source_revision")
        predecessor = submission.get("predecessor_revision")
        if (
            isinstance(source_revision, bool)
            or not isinstance(source_revision, int)
            or source_revision < 1
            or (
                predecessor is not None
                and (
                    isinstance(predecessor, bool)
                    or not isinstance(predecessor, int)
                    or predecessor < 1
                    or predecessor >= source_revision
                )
            )
        ):
            self._fail(
                "rejected",
                "intake.source_revision_conflict",
                "integrator",
                "submit_a_valid_source_revision_chain",
                gates=("identity:verified", "contract:verified", "causality:failed"),
                **failure_context,
            )

        document = submission.get("document_binding")
        self._require_contract_keys(
            document,
            allowed=_DOCUMENT_BINDING_KEYS,
            required=_DOCUMENT_BINDING_KEYS,
            failure_context=failure_context,
        )
        document_id = _required_id(document, "source_document_ref")
        document_type = _required_id(document, "document_type")
        document_role = _required_id(document, "document_role")

        result_object = self._verify_object(
            submission.get("result_object"),
            registration=registration,
            expected_media="application/json",
            max_bytes=registration.max_result_bytes,
            failure_context=failure_context,
        )
        try:
            result_payload = json.loads(result_object.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise S02IntakeError(
                "quarantined",
                "adapter.source_format_unsupported",
                responsible_party="source_owner",
                recovery_action="reproduce_a_registered_result_artifact",
                gate_results=(
                    "identity:verified",
                    "contract:verified",
                    "object:verified",
                    "provenance:failed",
                ),
                **failure_context,
            ) from error
        if not isinstance(result_payload, dict):
            self._fail(
                "quarantined",
                "adapter.source_format_unsupported",
                "source_owner",
                "reproduce_a_registered_result_artifact",
                gates=("identity:verified", "contract:verified", "object:verified", "provenance:failed"),
                **failure_context,
            )
        if _has_label_keys(result_payload):
            self._fail(
                "rejected",
                "intake.data_track_mismatch",
                "integrator",
                "remove_evaluation_labels_from_business_input",
                gates=("identity:verified", "contract:verified", "object:verified", "provenance:failed"),
                **failure_context,
            )

        attachments = submission.get("attachments")
        if (
            not isinstance(attachments, list)
            or not attachments
            or len(attachments) > registration.max_pages
        ):
            self._fail(
                "rejected",
                "intake.resource_limit_exceeded",
                "integrator",
                "submit_a_bounded_nonempty_page_set",
                gates=("identity:verified", "contract:verified", "object:failed"),
                **failure_context,
            )
        pages: dict[str, dict[str, Any]] = {}
        seen_page_refs: set[str] = set()
        graph_attachments: list[dict[str, Any]] = []
        for attachment in attachments:
            self._require_contract_keys(
                attachment,
                allowed=_ATTACHMENT_KEYS,
                required=_ATTACHMENT_KEYS,
                failure_context=failure_context,
            )
            attachment_ref = _required_id(attachment, "source_attachment_ref")
            page_ref = _required_id(attachment, "page_ref")
            page_ordinal = attachment.get("page_ordinal")
            source_name_sha256 = attachment.get("source_name_sha256")
            if (
                page_ref in seen_page_refs
                or isinstance(page_ordinal, bool)
                or not isinstance(page_ordinal, int)
                or page_ordinal < 1
                or not isinstance(source_name_sha256, str)
                or not _SHA256.fullmatch(source_name_sha256)
                or source_name_sha256 in pages
            ):
                self._provenance_failure(failure_context)
            page_object = self._verify_object(
                attachment.get("object"),
                registration=registration,
                expected_media=None,
                max_bytes=registration.max_attachment_bytes,
                failure_context=failure_context,
            )
            if page_object.media_type not in registration.allowed_media_types:
                self._fail(
                    "quarantined",
                    "evidence.media_type_unsupported",
                    "source_owner",
                    "replace_with_a_registered_single_page_media_object",
                    gates=("identity:verified", "contract:verified", "object:failed"),
                    **failure_context,
                )
            width, height = _image_dimensions(page_object.content, page_object.media_type)
            page = {
                "attachment_ref": attachment_ref,
                "page_ref": page_ref,
                "page_ordinal": page_ordinal,
                "source_name_sha256": source_name_sha256,
                "source_object_ref": page_object.object_ref,
                "source_sha256": hashlib.sha256(page_object.content).hexdigest(),
                "media_type": page_object.media_type,
                "size_bytes": len(page_object.content),
                "width": width,
                "height": height,
            }
            pages[source_name_sha256] = page
            seen_page_refs.add(page_ref)
            graph_attachments.append(copy.deepcopy(page))

        producer = self._producer(submission.get("producer"), registration, failure_context)
        adapters = {
            "step2-page-order/unversioned": self._adapt_page_order,
            "step2-slots/v1": self._adapt_slots,
            "ocr-aggregate/unversioned": self._adapt_aggregate,
            "ocr-detection/unversioned": self._adapt_detection,
            "external-ocr/v1": self._adapt_external_ocr,
        }
        adapter = adapters.get(registration.source_shape)
        if adapter is None:
            self._fail(
                "quarantined",
                "adapter.source_format_unsupported",
                "source_owner",
                "use_the_registered_shape_or_publish_a_new_adapter",
                gates=("identity:verified", "contract:verified", "object:verified", "provenance:failed"),
                **failure_context,
            )
        observations = adapter(
            result_payload,
            pages=pages,
            producer=producer,
            result_object=result_object,
            registration=registration,
            failure_context=failure_context,
        )
        if not observations:
            self._fail(
                "quarantined",
                "adapter.output_contract_invalid",
                "source_owner",
                "submit_a_nonempty_registered_result",
                gates=("identity:verified", "contract:verified", "object:verified", "provenance:failed"),
                **failure_context,
            )

        provenance_manifest = {
            "schema_version": "s02-provenance-manifest/1",
            "registration_digest": registration_digest,
            "result_object_ref": result_object.object_ref,
            "result_sha256": hashlib.sha256(result_object.content).hexdigest(),
            "producer": producer,
            "attachments": graph_attachments,
            "observation_count": len(observations),
        }
        provenance_digest = _digest(provenance_manifest)
        for observation in observations:
            observation["provenance_manifest_digest"] = provenance_digest

        # S10: map every step2 page_order page into one immutable candidate
        # page-membership claim (provenance-bearing; never inferred).  The page
        # identity is the admitted attachment reference plus page ordinal;
        # content hash remains the page-integrity evidence.
        page_memberships: list[dict[str, Any]] = []
        if registration.source_shape == "step2-page-order/unversioned":
            def resolve_page_binding(
                source_page: dict[str, Any],
            ) -> tuple[str, str]:
                source_name = _source_name(source_page.get("filename"))
                binding = pages.get(
                    hashlib.sha256(source_name.encode("utf-8")).hexdigest()
                )
                if (
                    not isinstance(binding, dict)
                    or not isinstance(binding.get("attachment_ref"), str)
                    or not isinstance(binding.get("source_sha256"), str)
                ):
                    RegisteredSourceBoundary._provenance_failure(failure_context)
                return str(binding["attachment_ref"]), str(binding["source_sha256"])

            page_memberships = step2_page_order_membership_claims(
                result_payload,
                application_id=upstream_ref,
                resolve_page_binding=resolve_page_binding,
                document_instance_id=document_id,
                document_role=document_role,
            )

        authenticated_context = {
            "scope": scope,
            "source_id": source_system_id,
            "tenant_id": tenant_id,
            "workload_identity_id": workload_id,
        }
        envelope = {
            "version": ENVELOPE_VERSION,
            "schema_version": SCHEMA_VERSION,
            "semantic_version": SEMANTIC_VERSION,
            "command_type": "submit_observation_result",
            "envelope_id": envelope_id,
            "stream_id": stream_id,
            "source_revision": source_revision,
            "predecessor_revision": predecessor,
            "upstream_application_reference": upstream_ref,
            "must_understand": [],
            "authenticated_context": authenticated_context,
            "scope": {
                "mode": "full_snapshot",
                "track": "R-OBSERVED",
                "document_reference": document_id,
                "fact_kinds": [
                    "attachment",
                    "page",
                    "producer_result",
                    "field_observation",
                ],
            },
        }
        source = {
            "adapter_id": registration.adapter_id,
            "adapter_version": registration.adapter_version,
            "adapter_build": ADAPTER_BUILD,
            "source_shape": registration.source_shape,
            "source_registration_digest": registration_digest,
            "source_registry_manifest_digest": self.manifest_digest,
            "source_result_object_ref": result_object.object_ref,
            "source_result_sha256": hashlib.sha256(result_object.content).hexdigest(),
            "source_result_size_bytes": len(result_object.content),
            "provenance_manifest_digest": provenance_digest,
        }
        canonical_payload = {
            "track": "R-OBSERVED",
            "envelope": envelope,
            "source": source,
            "application": {
                "evidence": [
                    {
                        "document_id": document_id,
                        "document_type": document_type,
                        "document_role": document_role,
                        "fields": {},
                        "observations": observations,
                    }
                ],
                "graph": {
                    "attachments": graph_attachments,
                    "producer_result": provenance_manifest,
                    **(
                        {"page_memberships": page_memberships}
                        if page_memberships
                        else {}
                    ),
                },
            },
        }
        fingerprint = _digest(canonical_payload)
        eligible = all(item["evidence_eligible"] is True for item in observations)
        return S02CanonicalEnvelope(
            envelope_version=ENVELOPE_VERSION,
            schema_version=SCHEMA_VERSION,
            semantic_version=SEMANTIC_VERSION,
            command_type="submit_observation_result",
            upstream_application_reference=upstream_ref,
            envelope_id=envelope_id,
            stream_id=stream_id,
            source_revision=source_revision,
            predecessor_revision=predecessor,
            fingerprint=fingerprint,
            payload=canonical_payload,
            adapter_id=registration.adapter_id,
            adapter_version=registration.adapter_version,
            registration_digest=registration_digest,
            observation_count=len(observations),
            attachment_count=len(graph_attachments),
            provenance_eligible=eligible,
        )

    def canonicalize_attachment_version(
        self,
        submission: Any,
        *,
        scope: str,
        source_system_id: str,
    ) -> S02CanonicalEnvelope:
        """Verify one request-bound attachment using the registered S02 path."""
        if (
            not isinstance(submission, dict)
            or submission.get("command_type") != "submit_attachment_version"
        ):
            self._fail(
                "rejected",
                "intake.schema_unsupported",
                "integrator",
                "submit_a_supported_command",
                gates=("identity:verified", "contract:failed"),
            )
        base_submission = {
            key: copy.deepcopy(submission.get(key)) for key in _SUBMISSION_KEYS
        }
        base_submission["command_type"] = "submit_observation_result"
        base = self.canonicalize(
            base_submission,
            scope=scope,
            source_system_id=source_system_id,
        )
        failure_context = {
            "adapter_id": base.adapter_id,
            "adapter_version": base.adapter_version,
            "registration_digest": base.registration_digest,
        }
        self._validate_transport(submission, failure_context)
        self._require_contract_keys(
            submission,
            allowed=_ATTACHMENT_VERSION_SUBMISSION_KEYS,
            required=_ATTACHMENT_VERSION_SUBMISSION_KEYS,
            failure_context=failure_context,
        )

        request = submission.get("request_binding")
        self._require_contract_keys(
            request,
            allowed=_REQUEST_BINDING_KEYS,
            required=_REQUEST_BINDING_KEYS,
            failure_context=failure_context,
        )
        request_id = _required_id(request, "supplement_request_id")
        requirement_id = request.get("material_requirement_id")
        request_context_digest = request.get("request_context_digest")
        progress_revision = request.get("request_progress_revision")
        if (
            not isinstance(requirement_id, str)
            or not requirement_id
            or requirement_id.strip() != requirement_id
            or len(requirement_id) > 200
            or not isinstance(request_context_digest, str)
            or not _SHA256.fullmatch(request_context_digest)
            or isinstance(progress_revision, bool)
            or not isinstance(progress_revision, int)
            or progress_revision < 1
        ):
            self._provenance_failure(failure_context)

        lineage = submission.get("attachment_lineage")
        self._require_contract_keys(
            lineage,
            allowed=_ATTACHMENT_LINEAGE_KEYS,
            required=_ATTACHMENT_LINEAGE_KEYS,
            failure_context=failure_context,
        )
        predecessor_attachment_id = _required_id(
            lineage, "predecessor_attachment_id"
        )
        predecessor_version = lineage.get("predecessor_attachment_version")
        attachment_version = lineage.get("attachment_version")
        if (
            lineage.get("operation") != "replacement"
            or isinstance(predecessor_version, bool)
            or not isinstance(predecessor_version, int)
            or predecessor_version < 1
            or isinstance(attachment_version, bool)
            or not isinstance(attachment_version, int)
            or attachment_version != predecessor_version + 1
        ):
            self._provenance_failure(failure_context)

        batch = submission.get("batch")
        self._require_contract_keys(
            batch,
            allowed=_REQUEST_BATCH_KEYS,
            required=_REQUEST_BATCH_KEYS,
            failure_context=failure_context,
        )
        batch_id = _required_id(batch, "batch_id")
        item_sequence = batch.get("item_sequence")
        item_count = batch.get("item_count")
        final_sequence = batch.get("final_sequence")
        closed = batch.get("closed")
        manifest_digest = batch.get("manifest_digest")
        scope_mode = batch.get("scope_mode")
        if (
            any(
                isinstance(value, bool) or not isinstance(value, int) or value < 1
                for value in (item_sequence, item_count, final_sequence)
            )
            or item_count != final_sequence
            or item_sequence > final_sequence
            or type(closed) is not bool
            or closed != (item_sequence == final_sequence)
            or scope_mode not in {"full", "incremental"}
            or not isinstance(manifest_digest, str)
            or not _SHA256.fullmatch(manifest_digest)
        ):
            self._fail(
                "rejected",
                "intake.batch_contract_invalid",
                "integrator",
                "submit_a_causally_valid_request_batch",
                gates=(
                    "identity:verified",
                    "contract:verified",
                    "object:verified",
                    "causality:failed",
                ),
                **failure_context,
            )
        manifest = {
            "batch_id": batch_id,
            "final_sequence": final_sequence,
            "item_count": item_count,
            "scope_mode": scope_mode,
            "stream_id": base.stream_id,
            "supplement_request_id": request_id,
        }
        if _digest(manifest) != manifest_digest:
            self._fail(
                "quarantined",
                "intake.batch_manifest_conflict",
                "source_owner",
                "reconcile_the_request_batch_manifest",
                gates=(
                    "identity:verified",
                    "contract:verified",
                    "object:verified",
                    "causality:failed",
                ),
                **failure_context,
            )

        canonical_payload = copy.deepcopy(base.payload)
        canonical_payload["envelope"].update(
            {
                "command_type": "submit_attachment_version",
                "request_binding": copy.deepcopy(request),
                "attachment_lineage": copy.deepcopy(lineage),
                "batch": copy.deepcopy(batch),
            }
        )
        canonical_payload["envelope"]["scope"].update(
            {
                "mode": f"request_bound_{scope_mode}",
                "supplement_request_id": request_id,
                "material_requirement_id": requirement_id,
            }
        )
        canonical_payload["request_binding"] = copy.deepcopy(request)
        canonical_payload["attachment_lineage"] = copy.deepcopy(lineage)
        canonical_payload["batch"] = copy.deepcopy(batch)
        canonical_payload["application"]["evidence"][0][
            "attachment_lineage"
        ] = copy.deepcopy(lineage)
        return S02CanonicalEnvelope(
            **{
                **base.__dict__,
                "command_type": "submit_attachment_version",
                "fingerprint": _digest(canonical_payload),
                "payload": canonical_payload,
            }
        )

    def _verify_object(
        self,
        descriptor: Any,
        *,
        registration: RegisteredSource,
        expected_media: str | None,
        max_bytes: int,
        failure_context: dict[str, str],
    ) -> ControlledObject:
        self._require_contract_keys(
            descriptor,
            allowed=_OBJECT_DESCRIPTOR_KEYS,
            required=_OBJECT_DESCRIPTOR_KEYS,
            failure_context=failure_context,
        )
        object_ref = descriptor.get("controlled_object_ref")
        declared_media = descriptor.get("media_type")
        declared_size = descriptor.get("size_bytes")
        declared_sha256 = descriptor.get("sha256")
        if (
            not _valid_id(object_ref)
            or not isinstance(declared_media, str)
            or isinstance(declared_size, bool)
            or not isinstance(declared_size, int)
            or declared_size < 1
            or not isinstance(declared_sha256, str)
            or not _SHA256.fullmatch(declared_sha256)
        ):
            self._provenance_failure(failure_context)
        item = self._objects.get(object_ref)
        if (
            item is None
            or item.tenant_id != registration.tenant_id
            or item.source_system_id != registration.source_system_id
        ):
            self._provenance_failure(failure_context)
        if len(item.content) > max_bytes:
            self._fail(
                "rejected",
                "intake.resource_limit_exceeded",
                "source_owner",
                "submit_an_object_within_registered_limits",
                gates=("identity:verified", "contract:verified", "object:failed"),
                **failure_context,
            )
        actual_sha256 = hashlib.sha256(item.content).hexdigest()
        if declared_size != len(item.content) or declared_sha256 != actual_sha256:
            self._fail(
                "quarantined",
                "evidence.integrity_failed",
                "source_owner",
                "reconcile_and_resubmit_the_immutable_object",
                gates=("identity:verified", "contract:verified", "object:failed"),
                **failure_context,
            )
        if declared_media != item.media_type or (
            expected_media is not None and item.media_type != expected_media
        ):
            self._fail(
                "quarantined",
                "evidence.content_type_mismatch",
                "source_owner",
                "correct_the_media_declaration_or_object",
                gates=("identity:verified", "contract:verified", "object:failed"),
                **failure_context,
            )
        return item

    @staticmethod
    def _producer(
        value: Any,
        registration: RegisteredSource,
        failure_context: dict[str, str],
    ) -> dict[str, Any]:
        RegisteredSourceBoundary._require_contract_keys(
            value,
            allowed=_PRODUCER_KEYS,
            required={"producer_family"},
            failure_context=failure_context,
        )
        if value.get("producer_family") != registration.producer_family:
            raise S02IntakeError(
                "quarantined",
                "adapter.mapping_ambiguous",
                responsible_party="source_owner",
                recovery_action="bind_one_registered_producer_family",
                gate_results=("identity:verified", "contract:verified", "object:verified", "provenance:failed"),
                **failure_context,
            )
        result: dict[str, Any] = {"producer_family": registration.producer_family}
        for key in (
            "producer_id",
            "task_id",
            "task_version",
            "run_id",
            "model_id",
            "model_version",
        ):
            item = value.get(key)
            result[key] = item if _valid_id(item) else None
        coordinate = value.get("coordinate_system")
        if isinstance(coordinate, dict):
            RegisteredSourceBoundary._require_contract_keys(
                coordinate,
                allowed=_COORDINATE_SYSTEM_KEYS,
                required=set(),
                failure_context=failure_context,
            )
        if isinstance(coordinate, dict) and all(
            _valid_id(coordinate.get(key)) for key in ("name", "unit", "origin")
        ):
            result["coordinate_system"] = {
                key: coordinate[key] for key in ("name", "unit", "origin")
            }
        else:
            result["coordinate_system"] = None
        confidence = value.get("confidence_semantics")
        if isinstance(confidence, dict):
            RegisteredSourceBoundary._require_contract_keys(
                confidence,
                allowed=_CONFIDENCE_SEMANTICS_KEYS,
                required=set(),
                failure_context=failure_context,
            )
        valid_confidence = (
            isinstance(confidence, dict)
            and _finite_number(confidence.get("minimum"))
            and _finite_number(confidence.get("maximum"))
            and float(confidence["minimum"]) < float(confidence["maximum"])
            and all(
                _valid_id(confidence.get(key))
                for key in ("higher_is", "meaning", "granularity", "calibration")
            )
        )
        result["confidence_semantics"] = (
            copy.deepcopy(confidence) if valid_confidence else None
        )
        return result

    def _adapt_detection(
        self,
        payload: dict[str, Any],
        *,
        pages: dict[str, dict[str, Any]],
        producer: dict[str, Any],
        result_object: ControlledObject,
        registration: RegisteredSource,
        failure_context: dict[str, str],
    ) -> list[dict[str, Any]]:
        self._require_shape_keys(
            payload,
            allowed=_DETECTION_ROOT_KEYS,
            required={"per_image_results"},
            failure_context=failure_context,
        )
        per_image = payload.get("per_image_results")
        if not isinstance(per_image, list):
            self._source_shape_failure(failure_context)
        observations: list[dict[str, Any]] = []
        for page_index, page_result in enumerate(per_image):
            if not isinstance(page_result, dict):
                self._source_shape_failure(failure_context)
            self._require_shape_keys(
                page_result,
                allowed=_DETECTION_PAGE_KEYS,
                required={"detections", "image_path"},
                failure_context=failure_context,
            )
            source_name = _source_name(page_result.get("image_path"))
            binding = pages.get(hashlib.sha256(source_name.encode("utf-8")).hexdigest())
            if binding is None:
                self._provenance_failure(failure_context)
            source_size = page_result.get("image_size")
            if source_size is not None:
                self._require_shape_keys(
                    source_size,
                    allowed=_DETECTION_SIZE_KEYS,
                    required={"height", "width"},
                    failure_context=failure_context,
                )
            if isinstance(source_size, dict) and (
                source_size.get("width") != binding["width"]
                or source_size.get("height") != binding["height"]
            ):
                self._provenance_failure(failure_context)
            detections = page_result.get("detections")
            if not isinstance(detections, list):
                self._source_shape_failure(failure_context)
            for detection_index, detection in enumerate(detections):
                if not isinstance(detection, dict):
                    self._source_shape_failure(failure_context)
                self._require_shape_keys(
                    detection,
                    allowed=_DETECTION_KEYS,
                    required={"bbox", "confidence"},
                    failure_context=failure_context,
                )
                raw = _detection_raw(detection, failure_context)
                bbox = detection.get("bbox")
                valid_bbox = (
                    isinstance(bbox, list)
                    and len(bbox) == 4
                    and all(_finite_number(item) for item in bbox)
                    and float(bbox[0]) <= float(bbox[2])
                    and float(bbox[1]) <= float(bbox[3])
                    and float(bbox[0]) >= 0
                    and float(bbox[1]) >= 0
                    and float(bbox[2]) <= binding["width"]
                    and float(bbox[3]) <= binding["height"]
                )
                confidence = detection.get("confidence")
                valid_score = _finite_number(confidence) and 0 <= float(confidence) <= 1
                field_name = _canonical_field(
                    detection.get("field_key") or detection.get("class_name")
                )
                complete = all(
                    (
                        producer.get("producer_id"),
                        producer.get("run_id"),
                        producer.get("model_id"),
                        producer.get("model_version"),
                        producer.get("coordinate_system"),
                        producer.get("confidence_semantics"),
                        valid_bbox,
                        valid_score,
                        raw is not None,
                    )
                )
                if raw is None:
                    eligibility_reason = "evidence.value_not_observed"
                elif not producer.get("run_id") or not producer.get("model_id"):
                    eligibility_reason = "evidence.producer_metadata_incomplete"
                elif not valid_bbox or not producer.get("coordinate_system"):
                    eligibility_reason = "evidence.location_incomplete"
                elif not valid_score or not producer.get("confidence_semantics"):
                    eligibility_reason = "evidence.confidence_semantics_unknown"
                elif field_name.startswith("unmapped:"):
                    eligibility_reason = "adapter.mapping_ambiguous"
                else:
                    eligibility_reason = "REGISTERED_SOURCE_PROVENANCE_VERIFIED"
                complete = complete and not field_name.startswith("unmapped:")
                observation_material = {
                    "result_sha256": hashlib.sha256(result_object.content).hexdigest(),
                    "page_ref": binding["page_ref"],
                    "page_index": page_index,
                    "detection_index": detection_index,
                    "field": field_name,
                    "raw_type": _json_type(raw),
                    "raw_lexeme": raw if isinstance(raw, str) else _canonical_json(raw),
                    "bbox": bbox if valid_bbox else None,
                    "confidence": confidence if valid_score else None,
                }
                observations.append(
                    {
                        "observation_id": "observation_" + _digest(observation_material)[:24],
                        "field": field_name,
                        "raw": copy.deepcopy(raw),
                        "raw_type": _json_type(raw),
                        "raw_lexeme": raw if isinstance(raw, str) else _canonical_json(raw),
                        "value_state": _value_state(raw),
                        "attempt_status": "observed" if raw is not None else "not_detected",
                        "confidence": float(confidence) if valid_score else None,
                        "confidence_semantics": copy.deepcopy(
                            producer.get("confidence_semantics")
                        ),
                        "source_object_ref": binding["source_object_ref"],
                        "source_sha256": binding["source_sha256"],
                        "source_page": binding["page_ordinal"],
                        "source_region": _bbox_text(bbox) if valid_bbox else None,
                        "coordinate_system": copy.deepcopy(
                            producer.get("coordinate_system")
                        ),
                        "producer_id": producer.get("producer_id"),
                        "producer_family": producer.get("producer_family"),
                        "producer_task_id": producer.get("task_id"),
                        "producer_task_version": producer.get("task_version"),
                        "producer_run_id": producer.get("run_id"),
                        "model_id": producer.get("model_id"),
                        "model_version": producer.get("model_version"),
                        "source_result_object_ref": result_object.object_ref,
                        "source_result_sha256": hashlib.sha256(
                            result_object.content
                        ).hexdigest(),
                        "source_pointer": (
                            f"/per_image_results/{page_index}/detections/{detection_index}"
                        ),
                        "evidence_eligible": complete,
                        "eligibility_reason": eligibility_reason,
                    }
                )
                if len(observations) > registration.max_observations:
                    self._fail(
                        "rejected",
                        "intake.resource_limit_exceeded",
                        "source_owner",
                        "split_the_result_within_registered_limits",
                        gates=("identity:verified", "contract:verified", "object:verified", "provenance:failed"),
                        **failure_context,
                    )
        return observations

    def _adapt_page_order(
        self,
        payload: dict[str, Any],
        *,
        pages: dict[str, dict[str, Any]],
        producer: dict[str, Any],
        result_object: ControlledObject,
        registration: RegisteredSource,
        failure_context: dict[str, str],
    ) -> list[dict[str, Any]]:
        self._require_shape_keys(
            payload,
            allowed=_PAGE_ORDER_ROOT_KEYS,
            required={"pages"},
            failure_context=failure_context,
        )
        source_pages = payload.get("pages")
        if not isinstance(source_pages, list):
            self._source_shape_failure(failure_context)
        observations: list[dict[str, Any]] = []
        for page_index, source_page in enumerate(source_pages):
            if not isinstance(source_page, dict):
                self._source_shape_failure(failure_context)
            self._require_shape_keys(
                source_page,
                allowed=_PAGE_ORDER_PAGE_KEYS,
                required={"detections", "filename"},
                failure_context=failure_context,
            )
            binding = self._binding_for_source(
                source_page.get("filename"), pages, failure_context
            )
            detections = source_page.get("detections")
            if not isinstance(detections, list):
                self._source_shape_failure(failure_context)
            for detection_index, detection in enumerate(detections):
                if not isinstance(detection, dict):
                    self._source_shape_failure(failure_context)
                self._require_shape_keys(
                    detection,
                    allowed=_PAGE_ORDER_DETECTION_KEYS,
                    required={"bbox", "confidence"},
                    failure_context=failure_context,
                )
                observations.append(
                    self._legacy_observation(
                        field_source=(
                            detection.get("class_name_cn")
                            or detection.get("class_name")
                        ),
                        raw=None,
                        bbox=detection.get("bbox"),
                        confidence=detection.get("confidence"),
                        binding=binding,
                        producer=producer,
                        result_object=result_object,
                        source_pointer=(
                            f"/pages/{page_index}/detections/{detection_index}"
                        ),
                        attempt_status="not_detected",
                    )
                )
                self._check_observation_limit(
                    observations, registration, failure_context
                )
        return observations

    def _adapt_slots(
        self,
        payload: dict[str, Any],
        *,
        pages: dict[str, dict[str, Any]],
        producer: dict[str, Any],
        result_object: ControlledObject,
        registration: RegisteredSource,
        failure_context: dict[str, str],
    ) -> list[dict[str, Any]]:
        self._require_shape_keys(
            payload,
            allowed=_SLOTS_ROOT_KEYS,
            required={"schema", "slots"},
            failure_context=failure_context,
        )
        slots = payload.get("slots")
        if (
            payload.get("schema") != "task4.external_ocr_slots.v1"
            or not isinstance(slots, list)
            or not slots
            or (
                payload.get("n_slots") is not None
                and payload.get("n_slots") != len(slots)
            )
        ):
            self._source_shape_failure(failure_context)
        observations: list[dict[str, Any]] = []
        for index, slot in enumerate(slots):
            if not isinstance(slot, dict):
                self._source_shape_failure(failure_context)
            self._require_shape_keys(
                slot,
                allowed=_SLOT_KEYS,
                required={"bbox", "field", "raw"},
                failure_context=failure_context,
            )
            binding = self._binding_for_source(
                slot.get("image_filename") or slot.get("zip_member"),
                pages,
                failure_context,
            )
            raw = slot.get("raw") if "raw" in slot else None
            observations.append(
                self._legacy_observation(
                    field_source=slot.get("field"),
                    raw=raw,
                    bbox=slot.get("bbox"),
                    confidence=slot.get("confidence_det"),
                    binding=binding,
                    producer=producer,
                    result_object=result_object,
                    source_pointer=f"/slots/{index}",
                    attempt_status=(
                        "observed" if raw is not None else "not_detected"
                    ),
                )
            )
            self._check_observation_limit(observations, registration, failure_context)
        return observations

    def _adapt_aggregate(
        self,
        payload: dict[str, Any],
        *,
        pages: dict[str, dict[str, Any]],
        producer: dict[str, Any],
        result_object: ControlledObject,
        registration: RegisteredSource,
        failure_context: dict[str, str],
    ) -> list[dict[str, Any]]:
        self._require_shape_keys(
            payload,
            allowed=_AGGREGATE_ROOT_KEYS,
            required={"fields"},
            failure_context=failure_context,
        )
        fields = payload.get("fields")
        if isinstance(fields, dict) and isinstance(fields.get("fields"), dict):
            self._require_shape_keys(
                fields,
                allowed={"fields"},
                required={"fields"},
                failure_context=failure_context,
            )
            fields = fields["fields"]
        if not isinstance(fields, dict):
            self._source_shape_failure(failure_context)
        observations: list[dict[str, Any]] = []
        for field_index, (field_name, aggregate) in enumerate(fields.items()):
            if not isinstance(aggregate, dict) or "value" not in aggregate:
                self._source_shape_failure(failure_context)
            self._require_shape_keys(
                aggregate,
                allowed=_AGGREGATE_FIELD_KEYS,
                required={"sources", "value"},
                failure_context=failure_context,
            )
            sources = aggregate.get("sources")
            if not isinstance(sources, list) or not sources:
                self._provenance_failure(failure_context)
            for source_index, source in enumerate(sources):
                if isinstance(source, dict):
                    self._require_shape_keys(
                        source,
                        allowed=_AGGREGATE_SOURCE_KEYS,
                        required={"filename"},
                        failure_context=failure_context,
                    )
                source_name = (
                    source.get("filename") if isinstance(source, dict) else source
                )
                binding = self._binding_for_source(
                    source_name, pages, failure_context
                )
                observations.append(
                    self._legacy_observation(
                        field_source=field_name,
                        raw=aggregate.get("value"),
                        bbox=None,
                        confidence=None,
                        binding=binding,
                        producer=producer,
                        result_object=result_object,
                        source_pointer=(
                            f"/fields/{field_index}/sources/{source_index}"
                        ),
                        attempt_status="observed",
                    )
                )
                self._check_observation_limit(
                    observations, registration, failure_context
                )
        return observations

    def _adapt_external_ocr(
        self,
        payload: dict[str, Any],
        *,
        pages: dict[str, dict[str, Any]],
        producer: dict[str, Any],
        result_object: ControlledObject,
        registration: RegisteredSource,
        failure_context: dict[str, str],
    ) -> list[dict[str, Any]]:
        self._require_shape_keys(
            payload,
            allowed=_EXTERNAL_ROOT_KEYS,
            required={"documents", "ocr_model", "ocr_version", "schema_version"},
            failure_context=failure_context,
        )
        documents = payload.get("documents")
        if (
            payload.get("schema_version") != 1
            or not isinstance(documents, list)
            or len(documents) != 1
            or payload.get("ocr_model") != producer.get("model_id")
            or payload.get("ocr_version") != producer.get("model_version")
        ):
            self._source_shape_failure(failure_context)
        document = documents[0]
        self._require_shape_keys(
            document,
            allowed=_EXTERNAL_DOCUMENT_KEYS,
            required={"fields"},
            failure_context=failure_context,
        )
        fields = document.get("fields") if isinstance(document, dict) else None
        if not isinstance(fields, dict):
            self._source_shape_failure(failure_context)
        by_ordinal = {page["page_ordinal"]: page for page in pages.values()}
        observations: list[dict[str, Any]] = []
        for field_index, (field_name, value) in enumerate(fields.items()):
            if isinstance(value, dict):
                self._require_shape_keys(
                    value,
                    allowed=_EXTERNAL_FIELD_KEYS,
                    required={"raw"},
                    failure_context=failure_context,
                )
                if "raw" not in value:
                    self._source_shape_failure(failure_context)
                raw = value.get("raw")
                confidence = value.get("confidence")
                bbox = value.get("bbox")
                page_ordinal = value.get("source_page")
            elif isinstance(value, str) or value is None:
                raw = value
                confidence = None
                bbox = None
                page_ordinal = None
            else:
                self._source_shape_failure(failure_context)
            binding = by_ordinal.get(page_ordinal)
            if binding is None:
                self._provenance_failure(failure_context)
            observations.append(
                self._legacy_observation(
                    field_source=field_name,
                    raw=raw,
                    bbox=bbox,
                    confidence=confidence,
                    binding=binding,
                    producer=producer,
                    result_object=result_object,
                    source_pointer=f"/documents/0/fields/{field_index}",
                    attempt_status=(
                        "observed" if raw is not None else "not_detected"
                    ),
                )
            )
            self._check_observation_limit(observations, registration, failure_context)
        return observations

    @staticmethod
    def _binding_for_source(
        value: Any,
        pages: dict[str, dict[str, Any]],
        failure_context: dict[str, str],
    ) -> dict[str, Any]:
        try:
            source_name = _source_name(value)
        except S02IntakeError as error:
            raise S02IntakeError(
                error.disposition,
                error.reason_code,
                responsible_party=error.responsible_party,
                recovery_action=error.recovery_action,
                gate_results=error.gate_results,
                **failure_context,
            ) from error
        binding = pages.get(hashlib.sha256(source_name.encode("utf-8")).hexdigest())
        if binding is None:
            RegisteredSourceBoundary._provenance_failure(failure_context)
        return binding

    @staticmethod
    def _legacy_observation(
        *,
        field_source: Any,
        raw: Any,
        bbox: Any,
        confidence: Any,
        binding: dict[str, Any],
        producer: dict[str, Any],
        result_object: ControlledObject,
        source_pointer: str,
        attempt_status: str,
    ) -> dict[str, Any]:
        valid_bbox = (
            isinstance(bbox, list)
            and len(bbox) == 4
            and all(_finite_number(item) for item in bbox)
            and float(bbox[0]) <= float(bbox[2])
            and float(bbox[1]) <= float(bbox[3])
            and float(bbox[0]) >= 0
            and float(bbox[1]) >= 0
            and float(bbox[2]) <= binding["width"]
            and float(bbox[3]) <= binding["height"]
        )
        valid_score = _finite_number(confidence) and 0 <= float(confidence) <= 1
        field_name = _canonical_field(field_source)
        complete = bool(
            producer.get("producer_id")
            and producer.get("run_id")
            and producer.get("model_id")
            and producer.get("model_version")
            and producer.get("coordinate_system")
            and producer.get("confidence_semantics")
            and valid_bbox
            and valid_score
            and raw is not None
            and not field_name.startswith("unmapped:")
        )
        if raw is None:
            reason = "evidence.value_not_observed"
        elif not producer.get("run_id") or not producer.get("model_id"):
            reason = "evidence.producer_metadata_incomplete"
        elif not valid_bbox or not producer.get("coordinate_system"):
            reason = "evidence.location_incomplete"
        elif not valid_score or not producer.get("confidence_semantics"):
            reason = "evidence.confidence_semantics_unknown"
        elif field_name.startswith("unmapped:"):
            reason = "adapter.mapping_ambiguous"
        else:
            reason = "REGISTERED_SOURCE_PROVENANCE_VERIFIED"
        result_sha256 = hashlib.sha256(result_object.content).hexdigest()
        material = {
            "result_sha256": result_sha256,
            "page_ref": binding["page_ref"],
            "source_pointer": source_pointer,
            "field": field_name,
            "raw_type": _json_type(raw),
            "raw_lexeme": raw if isinstance(raw, str) else _canonical_json(raw),
            "bbox": bbox if valid_bbox else None,
            "confidence": confidence if valid_score else None,
        }
        return {
            "observation_id": "observation_" + _digest(material)[:24],
            "field": field_name,
            "raw": copy.deepcopy(raw),
            "raw_type": _json_type(raw),
            "raw_lexeme": (
                raw if isinstance(raw, str) else _canonical_json(raw)
            ),
            "value_state": (
                _value_state(raw) if attempt_status == "observed" else "not_detected"
            ),
            "attempt_status": attempt_status,
            "confidence": float(confidence) if valid_score else None,
            "confidence_semantics": copy.deepcopy(
                producer.get("confidence_semantics")
            ),
            "source_object_ref": binding["source_object_ref"],
            "source_sha256": binding["source_sha256"],
            "source_page": binding["page_ordinal"],
            "source_region": _bbox_text(bbox) if valid_bbox else None,
            "coordinate_system": copy.deepcopy(producer.get("coordinate_system")),
            "producer_id": producer.get("producer_id"),
            "producer_family": producer.get("producer_family"),
            "producer_task_id": producer.get("task_id"),
            "producer_task_version": producer.get("task_version"),
            "producer_run_id": producer.get("run_id"),
            "model_id": producer.get("model_id"),
            "model_version": producer.get("model_version"),
            "source_result_object_ref": result_object.object_ref,
            "source_result_sha256": result_sha256,
            "source_pointer": source_pointer,
            "evidence_eligible": complete,
            "eligibility_reason": reason,
        }

    @staticmethod
    def _check_observation_limit(
        observations: list[dict[str, Any]],
        registration: RegisteredSource,
        failure_context: dict[str, str],
    ) -> None:
        if len(observations) > registration.max_observations:
            RegisteredSourceBoundary._fail(
                "rejected",
                "intake.resource_limit_exceeded",
                "source_owner",
                "split_the_result_within_registered_limits",
                gates=("identity:verified", "contract:verified", "object:verified", "provenance:failed"),
                **failure_context,
            )

    @staticmethod
    def _require_shape_keys(
        value: Any,
        *,
        allowed: set[str],
        required: set[str],
        failure_context: dict[str, str],
    ) -> None:
        if (
            not isinstance(value, dict)
            or not required.issubset(value)
            or not set(value).issubset(allowed)
        ):
            RegisteredSourceBoundary._source_shape_failure(failure_context)

    @staticmethod
    def _require_contract_keys(
        value: Any,
        *,
        allowed: set[str],
        required: set[str],
        failure_context: dict[str, str],
    ) -> None:
        if (
            not isinstance(value, dict)
            or not required.issubset(value)
            or not set(value).issubset(allowed)
        ):
            RegisteredSourceBoundary._fail(
                "rejected",
                "intake.schema_unsupported",
                "integrator",
                "submit_a_valid_json_contract",
                gates=("identity:verified", "contract:failed"),
                **failure_context,
            )

    @staticmethod
    def _validate_transport(
        submission: dict[str, Any], failure_context: dict[str, str]
    ) -> None:
        stack: list[Any] = [submission]
        nodes = 0
        while stack:
            item = stack.pop()
            nodes += 1
            if nodes > 50_000:
                raise S02IntakeError(
                    "rejected",
                    "intake.resource_limit_exceeded",
                    responsible_party="integrator",
                    recovery_action="submit_a_bounded_contract",
                    gate_results=("identity:verified", "contract:failed"),
                    **failure_context,
                )
            if isinstance(item, dict):
                for key, value in item.items():
                    normalized = str(key).lower()
                    if normalized in _BANNED_TRANSPORT_KEYS:
                        raise S02IntakeError(
                            "rejected",
                            "intake.forbidden_locator",
                            responsible_party="integrator",
                            recovery_action="use_only_controlled_object_references",
                            gate_results=("identity:verified", "contract:failed"),
                            **failure_context,
                        )
                    stack.append(value)
            elif isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, str) and (
                item.lower().startswith(("http://", "https://", "file://", "ftp://"))
                or item.startswith(("/", "\\"))
                or "../" in item
                or "..\\" in item
            ):
                raise S02IntakeError(
                    "rejected",
                    "intake.forbidden_locator",
                    responsible_party="integrator",
                    recovery_action="use_only_controlled_object_references",
                    gate_results=("identity:verified", "contract:failed"),
                    **failure_context,
                )

    @staticmethod
    def _fail(
        disposition: str,
        reason_code: str,
        responsible_party: str,
        recovery_action: str,
        *,
        gates: tuple[str, ...],
        **context: str,
    ) -> None:
        raise S02IntakeError(
            disposition,
            reason_code,
            responsible_party=responsible_party,
            recovery_action=recovery_action,
            gate_results=gates,
            **context,
        )

    @staticmethod
    def _provenance_failure(context: dict[str, str]) -> None:
        RegisteredSourceBoundary._fail(
            "quarantined",
            "evidence.provenance_invalid",
            "source_owner",
            "repair_object_and_page_binding",
            gates=("identity:verified", "contract:verified", "object:failed"),
            **context,
        )

    @staticmethod
    def _source_shape_failure(context: dict[str, str]) -> None:
        RegisteredSourceBoundary._fail(
            "quarantined",
            "adapter.source_format_unsupported",
            "source_owner",
            "publish_or_use_a_registered_source_shape",
            gates=("identity:verified", "contract:verified", "object:verified", "provenance:failed"),
            **context,
        )


def tenant_from_scope(scope: str) -> str:
    if not isinstance(scope, str) or not scope.startswith(R_OBSERVED_SCOPE_PREFIX):
        raise S02IntakeError(
            "rejected",
            "intake.forbidden",
            responsible_party="integrator",
            recovery_action="authenticate_with_a_registered_tenant_scope",
            gate_results=("identity:failed",),
        )
    tenant_id = scope[len(R_OBSERVED_SCOPE_PREFIX) :]
    if not _valid_id(tenant_id):
        raise S02IntakeError(
            "rejected",
            "intake.forbidden",
            responsible_party="integrator",
            recovery_action="authenticate_with_a_registered_tenant_scope",
            gate_results=("identity:failed",),
        )
    return tenant_id


def is_registered_scope(scope: object) -> bool:
    try:
        tenant_from_scope(scope)  # type: ignore[arg-type]
    except S02IntakeError:
        return False
    return True


def _required_id(owner: dict[str, Any], key: str) -> str:
    value = owner.get(key)
    if not _valid_id(value):
        raise S02IntakeError(
            "rejected",
            "intake.schema_unsupported",
            responsible_party="integrator",
            recovery_action="submit_required_canonical_identifiers",
            gate_results=("identity:verified", "contract:failed"),
        )
    return value


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and _CANONICAL_ID.fullmatch(value) is not None


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _has_label_keys(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).lower() in _BANNED_LABEL_KEYS or _has_label_keys(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_has_label_keys(item) for item in value)
    return False


def _source_name(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise S02IntakeError(
            "quarantined",
            "evidence.provenance_invalid",
            responsible_party="source_owner",
            recovery_action="repair_object_and_page_binding",
            gate_results=("identity:verified", "contract:verified", "object:verified", "provenance:failed"),
        )
    name = re.split(r"[/\\]", value)[-1]
    if not name or name in {".", ".."}:
        raise S02IntakeError(
            "quarantined",
            "evidence.provenance_invalid",
            responsible_party="source_owner",
            recovery_action="repair_object_and_page_binding",
            gate_results=("identity:verified", "contract:verified", "object:verified", "provenance:failed"),
        )
    return name


def step2_page_order_membership_claims(
    payload: dict[str, Any],
    *,
    application_id: str,
    resolve_page_binding: Callable[[dict[str, Any]], tuple[str, str]] | None = None,
    document_instance_id: str | None = None,
    document_role: str | None = None,
) -> list[dict[str, Any]]:
    """Map each step2 page_order page into one candidate page-membership claim.

    S10 migration: a non-inferred page with a recognised ``page_type`` becomes
    one candidate claim keyed by its page identity and explicit provenance.
    Document instance and role come only from the registered submission
    binding.  When that binding or the admitted attachment binding is absent,
    the corpus projection records ``unknown``.  No claim is inferred from
    confidence, order, count, or last write."""
    source_pages = payload.get("pages")
    if not isinstance(source_pages, list):
        return []
    claims: list[dict[str, Any]] = []
    for page_index, source_page in enumerate(source_pages):
        if not isinstance(source_page, dict):
            continue
        if source_page.get("inferred") is True:
            continue
        page_type = source_page.get("page_type")
        if page_type not in {"登记页", "注册页"}:
            continue
        source_name = _source_name(source_page.get("filename"))
        attachment_id, source_sha256 = (
            resolve_page_binding(source_page)
            if resolve_page_binding is not None
            else ("unknown", "unknown")
        )
        if (
            not isinstance(attachment_id, str)
            or not attachment_id
            or not isinstance(source_sha256, str)
            or not source_sha256
            or (resolve_page_binding is not None and len(source_sha256) != 64)
        ):
            continue
        page_ordinal = source_page.get("order")
        if isinstance(page_ordinal, bool) or not isinstance(page_ordinal, int):
            page_ordinal = None
        if page_ordinal is None or page_ordinal < 1:
            page_ordinal = page_index + 1
        claim_material = {
            "application_id": application_id,
            "attachment_id": attachment_id,
            "source_sha256": source_sha256,
            "page_ordinal": page_ordinal,
            "page_type": page_type,
            "document_instance_id": document_instance_id or "unknown",
            "document_role": document_role or "unknown",
            "source_filename": source_name,
            "source_pointer": f"/pages/{page_index}",
        }
        claims.append(
            {
                "record_kind": "candidate",
                "claim_id": "membership_claim_"
                + _digest(_canonical_json(claim_material))[:24],
                "application_id": application_id,
                "page": {
                    "attachment_id": attachment_id,
                    "source_sha256": source_sha256,
                    "page_ordinal": page_ordinal,
                },
                "candidate_document": {
                    "document_instance_id": document_instance_id or "unknown",
                    "document_role": document_role or "unknown",
                },
                "provenance": {
                    "adapter_id": "step2-page-order",
                    "adapter_version": "1",
                    "source_filename": source_name,
                    "source_pointer": f"/pages/{page_index}",
                    "fact": "page.page_type",
                    "page_type": page_type,
                    "inferred": False,
                },
            }
        )
    return claims


def _image_dimensions(content: bytes, media_type: str) -> tuple[int, int]:
    try:
        if media_type == "image/png":
            width, height = _png_dimensions(content)
        elif media_type == "image/jpeg":
            width, height = _jpeg_dimensions(content)
        else:
            raise ValueError
    except (ValueError, struct.error, IndexError, zlib.error) as error:
        raise S02IntakeError(
            "quarantined",
            "evidence.content_type_mismatch",
            responsible_party="source_owner",
            recovery_action="replace_with_a_readable_registered_media_object",
            gate_results=("identity:verified", "contract:verified", "object:failed"),
        ) from error
    if width < 1 or height < 1 or width > 20_000 or height > 20_000 or width * height > 100_000_000:
        raise S02IntakeError(
            "rejected",
            "intake.resource_limit_exceeded",
            responsible_party="source_owner",
            recovery_action="submit_an_image_within_registered_pixel_limits",
            gate_results=("identity:verified", "contract:verified", "object:failed"),
        )
    return width, height


def _png_dimensions(content: bytes) -> tuple[int, int]:
    if len(content) < 57 or content[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError
    offset = 8
    header: tuple[int, int, int, int, int] | None = None
    idat: list[bytes] = []
    idat_closed = False
    palette_seen = False
    iend_seen = False
    while offset < len(content):
        if offset + 12 > len(content):
            raise ValueError
        length = struct.unpack(">I", content[offset : offset + 4])[0]
        end = offset + 12 + length
        if end > len(content):
            raise ValueError
        kind = content[offset + 4 : offset + 8]
        payload = content[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", content[offset + 8 + length : end])[0]
        if (
            not all(65 <= byte <= 90 or 97 <= byte <= 122 for byte in kind)
            or zlib.crc32(kind + payload) & 0xFFFFFFFF != expected_crc
        ):
            raise ValueError
        if header is None and kind != b"IHDR":
            raise ValueError
        if kind == b"IHDR":
            if header is not None or offset != 8 or length != 13:
                raise ValueError
            width, height, bit_depth, color_type, compression, filtering, interlace = (
                struct.unpack(">IIBBBBB", payload)
            )
            allowed_depths = {
                0: {1, 2, 4, 8, 16},
                2: {8, 16},
                3: {1, 2, 4, 8},
                4: {8, 16},
                6: {8, 16},
            }
            if (
                bit_depth not in allowed_depths.get(color_type, set())
                or compression != 0
                or filtering != 0
                or interlace not in {0, 1}
            ):
                raise ValueError
            header = (width, height, bit_depth, color_type, interlace)
        elif kind == b"PLTE":
            if idat or palette_seen or length < 3 or length > 768 or length % 3:
                raise ValueError
            palette_seen = True
        elif kind == b"IDAT":
            if idat_closed or not payload:
                raise ValueError
            idat.append(payload)
        elif kind == b"IEND":
            if length or not idat or end != len(content):
                raise ValueError
            iend_seen = True
            offset = end
            break
        else:
            if kind[0] & 0x20 == 0:
                raise ValueError
            if idat:
                idat_closed = True
        offset = end
    if header is None or not idat or not iend_seen or offset != len(content):
        raise ValueError
    width, height, bit_depth, color_type, interlace = header
    if color_type == 3 and not palette_seen:
        raise ValueError

    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    bits_per_pixel = channels * bit_depth
    rows = _png_scanline_lengths(width, height, bits_per_pixel, interlace)
    decoded_size = sum(rows)
    if decoded_size > _MAX_DECODED_IMAGE_BYTES:
        raise S02IntakeError(
            "rejected",
            "intake.resource_limit_exceeded",
            responsible_party="source_owner",
            recovery_action="submit_an_image_within_registered_pixel_limits",
            gate_results=("identity:verified", "contract:verified", "object:failed"),
        )
    decoder = zlib.decompressobj()
    decoded = decoder.decompress(b"".join(idat), decoded_size + 1)
    if decoder.unconsumed_tail or len(decoded) > decoded_size:
        raise ValueError
    decoded += decoder.flush(decoded_size + 1 - len(decoded))
    if (
        not decoder.eof
        or decoder.unused_data
        or len(decoded) != decoded_size
    ):
        raise ValueError
    cursor = 0
    for row_length in rows:
        if decoded[cursor] > 4:
            raise ValueError
        cursor += row_length
    return width, height


def _png_scanline_lengths(
    width: int, height: int, bits_per_pixel: int, interlace: int
) -> list[int]:
    if interlace == 0:
        return [((width * bits_per_pixel + 7) // 8) + 1] * height
    rows: list[int] = []
    for x_start, y_start, x_step, y_step in (
        (0, 0, 8, 8),
        (4, 0, 8, 8),
        (0, 4, 4, 8),
        (2, 0, 4, 4),
        (0, 2, 2, 4),
        (1, 0, 2, 2),
        (0, 1, 1, 2),
    ):
        pass_width = max(0, (width - x_start + x_step - 1) // x_step)
        pass_height = max(0, (height - y_start + y_step - 1) // y_step)
        if pass_width and pass_height:
            row_length = ((pass_width * bits_per_pixel + 7) // 8) + 1
            rows.extend([row_length] * pass_height)
    return rows


def _jpeg_dimensions(content: bytes) -> tuple[int, int]:
    if len(content) < 4 or content[:2] != b"\xff\xd8" or content[-2:] != b"\xff\xd9":
        raise ValueError
    offset = 2
    sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    while offset + 4 <= len(content):
        if content[offset] != 0xFF:
            offset += 1
            continue
        marker = content[offset + 1]
        offset += 2
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        length = struct.unpack(">H", content[offset : offset + 2])[0]
        if length < 2 or offset + length > len(content):
            raise ValueError
        if marker in sof:
            if length < 7:
                raise ValueError
            height, width = struct.unpack(">HH", content[offset + 3 : offset + 7])
            return width, height
        offset += length
    raise ValueError


def _finite_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _detection_raw(value: dict[str, Any], context: dict[str, str]) -> Any:
    has_value = "value" in value
    has_ocr = "ocr_text" in value
    if has_value and has_ocr and value["value"] != value["ocr_text"]:
        raise S02IntakeError(
            "quarantined",
            "adapter.mapping_ambiguous",
            responsible_party="source_owner",
            recovery_action="resolve_conflicting_source_values",
            gate_results=("identity:verified", "contract:verified", "object:verified", "provenance:failed"),
            **context,
        )
    return value.get("value") if has_value else value.get("ocr_text")


def _canonical_field(value: Any) -> str:
    source = str(value) if value is not None else "unknown"
    mapped = _FIELD_ALIASES.get(source)
    if mapped is not None:
        return mapped
    return "unmapped:" + hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unsupported"


def _value_state(value: Any) -> str:
    if value is None:
        return "explicit_null"
    if value == "":
        return "empty"
    return "present"


def _bbox_text(value: list[Any]) -> str:
    return "bbox:[" + ",".join(format(float(item), ".15g") for item in value) + "]"
