"""S09 isolated diagnostic runners.

Replay and simulation never execute inside the Governance service with a
writable store.  The Governance service resolves the exact governed release
and one fixed evidence snapshot into a least-privilege ``S09DiagnosticView``
capability object, and hands ONLY that view to an isolated runner.  The
runner exposes no ``persist``, no Governance/Lifecycle collections, no
current-state resolver and no business audit writer; it can only execute the
exact release over the fixed snapshot and produce its own immutable,
content-addressed diagnostic bundle.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .s01_checker import TargetChecker, TargetRelease
from .s09_impact import content_digest

BUNDLE_SCHEMA = "s09-diagnostic-bundle/1"


@dataclass(frozen=True)
class S09DiagnosticView:
    """The read-only capability object handed to an isolated diagnostic
    runner: the exact release plus the fixed application snapshot only.

    The object is frozen and carries no store reference, no collection, no
    ``persist``, no current-state resolver and no audit writer -- a runner
    can never write a business fact through it."""

    namespace: str
    release_candidate_id: str
    application_id: str
    release_manifest_id: str | None
    release_manifest_digest: str | None
    approval_binding_id: str | None
    worker_identity: str
    release: TargetRelease
    fixed_run_spec: str


@dataclass(frozen=True)
class S09DiagnosticBundleWriter:
    """Seal one diagnostic bundle inside one worker-owned namespace."""

    namespace: str
    worker_identity: str

    def write(
        self, bundle: dict[str, Any], *, worker_identity: str
    ) -> dict[str, Any]:
        if worker_identity != self.worker_identity:
            raise ValueError("diagnostic bundle worker identity mismatch")
        if bundle.get("namespace") != self.namespace:
            raise ValueError("diagnostic bundle namespace mismatch")
        if bundle.get("schema_version") != BUNDLE_SCHEMA:
            raise ValueError("diagnostic bundle schema mismatch")
        if bundle.get("business_revision_delta") != 0:
            raise ValueError("diagnostic bundle cannot contain a business revision")
        sealed = dict(bundle)
        digest = content_digest(sealed)
        sealed["bundle_digest"] = digest
        sealed["bundle_id"] = f"{self.namespace}_sha256_{digest}"
        return sealed


class S09DiagnosticRunner:
    """One isolated diagnostic runner per command.  ``run`` executes the
    exact release over the fixed snapshot and writes only its own immutable
    diagnostic bundle (content-addressed by digest); it never persists, never
    resolves current state and never writes business audit."""

    def __init__(
        self,
        worker_identity: str,
        bundle_writer: S09DiagnosticBundleWriter,
    ) -> None:
        if not isinstance(worker_identity, str) or not worker_identity:
            raise ValueError("diagnostic worker identity is invalid")
        self.worker_identity = worker_identity
        self._bundle_writer = bundle_writer

    def run(self, view: S09DiagnosticView) -> dict[str, Any]:
        if view.worker_identity != self.worker_identity:
            raise ValueError("diagnostic worker identity mismatch")
        if self._bundle_writer.namespace != view.namespace:
            raise ValueError("diagnostic bundle namespace mismatch")
        try:
            fixed_run_spec = json.loads(view.fixed_run_spec)
        except (TypeError, json.JSONDecodeError) as error:
            return {
                "schema_version": BUNDLE_SCHEMA,
                "namespace": view.namespace,
                "release_candidate_id": view.release_candidate_id,
                "application_id": view.application_id,
                "bundle_id": None,
                "outcome": "UNREPRODUCIBLE",
                "reason_code": "FIXED_SNAPSHOT_UNAVAILABLE",
                "business_revision_delta": 0,
            }
        fixed_spec_digest = hashlib.sha256(
            view.fixed_run_spec.encode("utf-8")
        ).hexdigest()
        try:
            result = TargetChecker(view.release).run(fixed_run_spec)
        except Exception:
            return {
                "schema_version": BUNDLE_SCHEMA,
                "namespace": view.namespace,
                "release_candidate_id": view.release_candidate_id,
                "application_id": view.application_id,
                "bundle_id": None,
                "outcome": "UNREPRODUCIBLE",
                "reason_code": "CHECKER_EXECUTION_FAILED",
                "business_revision_delta": 0,
            }
        checks = [
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
        ]
        selection_outcomes = [
            {
                "rule_id": outcome.rule_id,
                "observation_id": outcome.observation_id,
                "document_id": outcome.document_id,
                "document_role": outcome.document_role,
                "field": outcome.field,
                "selected": outcome.selected,
                "reason_code": outcome.reason_code,
            }
            for outcome in result.selection_outcomes
        ]
        normalization_outcomes = [
            {
                "rule_id": outcome.rule_id,
                "observation_id": outcome.observation_id,
                "document_id": outcome.document_id,
                "document_role": outcome.document_role,
                "field": outcome.field,
                "ocr_fix": outcome.ocr_fix,
                # The normalized value never leaves the runner: only its
                # digest is exposed for machine-decidable differentials.
                "normalized_sha256": (
                    hashlib.sha256(
                        str(outcome.normalized).encode("utf-8")
                    ).hexdigest()
                    if outcome.normalized is not None
                    else None
                ),
            }
            for outcome in result.normalization_outcomes
        ]
        finding_count = sum(
            item["severity"] in {"critical", "major"}
            and item["verdict"] != "consistent"
            for item in checks
        )
        bundle: dict[str, Any] = {
            "schema_version": BUNDLE_SCHEMA,
            "namespace": view.namespace,
            "release_candidate_id": view.release_candidate_id,
            "release_manifest_id": view.release_manifest_id,
            "release_manifest_digest": view.release_manifest_digest,
            "application_id": view.application_id,
            "outcome": "REPRODUCED",
            "check_count": len(checks),
            "finding_count": finding_count,
            "checks": checks,
            "selection_outcomes": selection_outcomes,
            "normalization_outcomes": normalization_outcomes,
            "route": "manual_review" if finding_count else "auto_complete",
            "approval_binding_id": view.approval_binding_id,
            "run_identity": (
                f"{view.namespace}:{view.release_candidate_id}:"
                f"{view.application_id}:{fixed_spec_digest}"
            ),
            "business_revision_delta": 0,
        }
        return self._bundle_writer.write(
            bundle,
            worker_identity=self.worker_identity,
        )
