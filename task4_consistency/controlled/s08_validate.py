"""Fresh-process validation worker for S08 candidate validation.

The policy worker materializes the pinned checker purely from the canonical
artifact and the frozen corpus passed over stdin, and prints a canonical
outcome digest.  The subprocess performs no file I/O and never touches the
process-global KB or any mutable singleton, so repeated runs prove checker
determinism and isolation.
"""

from __future__ import annotations

import hashlib
import json
import sys
from typing import Any

from task4_consistency.controlled.s01_checker import TargetChecker, TargetRelease


def _run_spec_for(release: TargetRelease, fixture: dict[str, Any]) -> dict[str, Any]:
    public = release.public_manifest()
    evidence: list[dict[str, Any]] = []
    skipped: str | None = None
    documents = fixture.get("documents")
    if not isinstance(documents, list) or not documents:
        skipped = "no_documents"
    else:
        for document in documents:
            if not isinstance(document, dict):
                skipped = "invalid_document"
                break
            document_id = document.get("doc_id")
            document_role = document.get("doc_type")
            fields = document.get("fields")
            if (
                not isinstance(document_id, str)
                or not document_id
                or not isinstance(document_role, str)
                or not document_role
                or not isinstance(fields, dict)
            ):
                skipped = "invalid_document_shape"
                break
            adapted_fields: dict[str, Any] = {}
            for field_name, value in fields.items():
                if isinstance(value, str):
                    adapted_fields[str(field_name)] = {"raw": value, "confidence": 1.0}
                elif isinstance(value, dict):
                    adapted_fields[str(field_name)] = {
                        "raw": value.get("raw"),
                        "confidence": float(value.get("confidence", 1.0)),
                    }
            evidence.append(
                {
                    "document_id": str(document_id),
                    "document_role": str(document_role),
                    "fields": adapted_fields,
                }
            )
    if skipped is not None:
        return {"skipped": skipped, "application_id": str(fixture.get("application_id", ""))}
    if not evidence:
        return {"skipped": "no_evidence", "application_id": str(fixture.get("application_id", ""))}
    snapshot_payload = {
        "schema_version": "s01-evidence-snapshot/1",
        "evidence": evidence,
    }
    snapshot_bytes = json.dumps(
        snapshot_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    snapshot_digest = hashlib.sha256(snapshot_bytes).hexdigest()
    return {
        "run_id": "s08_validation_corpus_run",
        "application_id": str(fixture.get("application_id", "corpus")),
        "cycle": 1,
        "lifecycle_revision": 1,
        "evidence_snapshot_id": f"snapshot_sha256_{snapshot_digest}",
        "evidence_snapshot_digest": snapshot_digest,
        "evidence_snapshot": snapshot_payload,
        "evidence_revision": 1,
        "evidence_readiness_policy": "c-demo-readiness/1",
        "baseline_release": {
            "release_id": public["release_id"],
            "digest": public["digest"],
            "checker_build": public["checker_build"],
            "rules_digest": public["rules_digest"],
            "knowledge_digest": public["knowledge_digest"],
            "normalizer_digest": public["normalizer_digest"],
            "waiver_policy_id": public["waiver_policy_id"],
            "waiver_policy_digest": public["waiver_policy_digest"],
            "limits": public["limits"],
            "applicable_check_count": public["applicable_check_count"],
            "applicable_check_ids": public["applicable_check_ids"],
        },
        "release_id": public["release_id"],
        "release_digest": public["digest"],
        "checker_build": public["checker_build"],
        "fence": 1,
        "limits": public["limits"],
        "applicable_check_ids": public["applicable_check_ids"],
        "applicable_check_count": public["applicable_check_count"],
    }


def _outcome_for(
    release: TargetRelease, fixture: dict[str, Any]
) -> dict[str, Any]:
    run_spec = _run_spec_for(release, fixture)
    if "skipped" in run_spec:
        return {
            "application_id": run_spec["application_id"],
            "skipped": run_spec["skipped"],
        }
    result = TargetChecker(release).run(run_spec)
    applicable = tuple(check.rule_id for check in result.checks)
    verdicts = tuple(
        (check.rule_id, check.verdict, tuple(check.reason_codes))
        for check in result.checks
    )
    selection = tuple(
        (outcome.document_id, outcome.field, outcome.selected, outcome.reason_code)
        for outcome in result.selection_outcomes
    )
    normalization = tuple(
        (outcome.document_id, outcome.field, outcome.normalized)
        for outcome in result.normalization_outcomes
    )
    mandatory_blocked = any(
        check.verdict != "consistent"
        and check.severity in {"critical", "major"}
        for check in result.checks
    )
    return {
        "application_id": run_spec["application_id"],
        "applicable": applicable,
        "verdicts": verdicts,
        "selection": selection,
        "normalization": normalization,
        "route": "manual_review" if mandatory_blocked else "auto_complete",
    }


def main() -> int:
    payload = json.load(sys.stdin)
    release = TargetRelease.from_artifact(payload["checker_artifact"])
    outcomes = [_outcome_for(release, fixture) for fixture in payload["corpus"]]
    digest = hashlib.sha256(
        json.dumps(
            outcomes, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    sys.stdout.write(json.dumps({"digest": digest, "outcomes": outcomes}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
