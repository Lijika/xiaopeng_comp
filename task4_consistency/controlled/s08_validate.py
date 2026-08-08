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

# G4 resource boundary: the fresh-process worker reads at most one bounded
# stdin payload, accepts a bounded corpus, verifies per-outcome cardinality
# against the release's declared limits and emits one canonical small
# outcome digest.  Anything outside these bounds is a rejected validation.
_MAX_STDIN_BYTES = 64 * 1024 * 1024
_MAX_CORPUS_ITEMS = 5000
_MAX_OUTPUT_BYTES = 32 * 1024 * 1024
_MAX_REASON_LENGTH = 512


def _fail(message: str) -> int:
    sys.stderr.write(message[: _MAX_REASON_LENGTH] + "\n")
    return 2


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
    stdin_bytes = sys.stdin.buffer.read(_MAX_STDIN_BYTES + 1)
    if len(stdin_bytes) > _MAX_STDIN_BYTES:
        return _fail("validation payload exceeds the input byte limit")
    try:
        payload = json.loads(stdin_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _fail("validation payload is not valid JSON")
    if not isinstance(payload, dict):
        return _fail("validation payload is not an object")
    try:
        release = TargetRelease.from_artifact(payload["checker_artifact"])
    except (KeyError, TypeError, ValueError):
        return _fail("checker artifact is not materializable")
    corpus = payload.get("corpus")
    if not isinstance(corpus, list) or len(corpus) > _MAX_CORPUS_ITEMS:
        return _fail("corpus is missing or exceeds the item limit")
    declared = dict(release.limits)
    outcomes: list[dict[str, Any]] = []
    for fixture in corpus:
        if not isinstance(fixture, dict):
            return _fail("corpus fixture is not an object")
        outcome = _outcome_for(release, fixture)
        if "skipped" in outcome:
            continue
        if len(outcome["applicable"]) > int(declared.get("max_findings", 0)):
            return _fail("outcome exceeds the declared finding limit")
        if len(outcome["verdicts"]) > int(declared.get("max_findings", 0)):
            return _fail("outcome exceeds the declared verdict limit")
        if (
            len(outcome["selection"]) > int(declared.get("max_documents", 0)) * 64
            or len(outcome["normalization"])
            > int(declared.get("max_documents", 0)) * 64
        ):
            return _fail("outcome exceeds the declared evidence limit")
        outcomes.append(outcome)
    digest = hashlib.sha256(
        json.dumps(
            outcomes, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    output = json.dumps({"digest": digest, "outcomes": outcomes})
    if len(output.encode("utf-8")) > _MAX_OUTPUT_BYTES:
        return _fail("validation output exceeds the output byte limit")
    sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
