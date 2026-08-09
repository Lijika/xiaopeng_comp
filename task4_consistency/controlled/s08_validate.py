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
import resource
import signal
import sys
from dataclasses import astuple, fields
from typing import Any

from task4_consistency.controlled.s01_checker import (
    TargetChecker,
    TargetCheckResult,
    TargetEvidenceLink,
    TargetNormalizationOutcome,
    TargetRelease,
    TargetSelectionOutcome,
)

# G4 resource boundary: the fresh-process worker reads at most one bounded
# stdin payload, accepts a bounded corpus, verifies per-outcome cardinality
# and declared runtime limits, and emits one canonical small outcome
# digest.  A finite memory/process boundary is enforced up front so a
# malformed child cannot grow without limit.
_MAX_STDIN_BYTES = 64 * 1024 * 1024
_MAX_CORPUS_ITEMS = 5000
_MAX_OUTPUT_BYTES = 32 * 1024 * 1024
_MAX_REASON_LENGTH = 512
_MEMORY_LIMIT_BYTES = 512 * 1024 * 1024
_CPU_LIMIT_SECONDS = 60
_OUTCOME_SCHEMA = {
    "check": tuple(field.name for field in fields(TargetCheckResult)),
    "evidence_link": tuple(field.name for field in fields(TargetEvidenceLink)),
    "selection": tuple(field.name for field in fields(TargetSelectionOutcome)),
    "normalization": tuple(
        field.name for field in fields(TargetNormalizationOutcome)
    ),
}


def _apply_process_boundaries() -> None:
    try:
        resource.setrlimit(
            resource.RLIMIT_AS,
            (_MEMORY_LIMIT_BYTES, _MEMORY_LIMIT_BYTES),
        )
        resource.setrlimit(
            resource.RLIMIT_CPU,
            (_CPU_LIMIT_SECONDS, _CPU_LIMIT_SECONDS + 1),
        )
    except (ValueError, OSError):
        pass


def _fail(message: str) -> int:
    sys.stderr.write(message[: _MAX_REASON_LENGTH] + "\n")
    return 2


def _deadline_expired(_signum: int, _frame: Any) -> None:
    raise TimeoutError("checker run deadline exceeded")


def _outcome_with_deadline(
    release: TargetRelease,
    fixture: dict[str, Any],
    max_runtime_ms: int,
) -> dict[str, Any]:
    previous = signal.signal(signal.SIGALRM, _deadline_expired)
    signal.setitimer(signal.ITIMER_REAL, max_runtime_ms / 1000.0)
    try:
        return _outcome_for(release, fixture)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _run_spec_for(release: TargetRelease, fixture: dict[str, Any]) -> dict[str, Any]:
    public = release.public_manifest()
    evidence: list[dict[str, Any]] = []
    skipped: str | None = None
    application_id = str(fixture.get("application_id", "corpus"))
    fixture_digest = hashlib.sha256(
        json.dumps(
            fixture, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    documents = fixture.get("documents")
    if not isinstance(documents, list) or not documents:
        skipped = "no_documents"
    else:
        for document_index, document in enumerate(documents):
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
                    raw = value
                    confidence = 1.0
                elif isinstance(value, dict):
                    raw = value.get("raw")
                    confidence = float(value.get("confidence", 1.0))
                else:
                    continue
                field = str(field_name)
                observation_digest = hashlib.sha256(
                    json.dumps(
                        (
                            fixture_digest,
                            document_index,
                            document_id,
                            field,
                            raw,
                        ),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                adapted_fields[field] = {
                    "raw": raw,
                    "confidence": confidence,
                    "observation_id": f"s08_corpus_obs_{observation_digest[:24]}",
                    "source_object_ref": (
                        f"s08-corpus:{fixture_digest}:{document_index}"
                    ),
                    "source_sha256": observation_digest,
                    "provenance_manifest_digest": fixture_digest,
                    "source_page": document_index + 1,
                    "source_region": f"field:{field}",
                    "evidence_eligible": True,
                    "eligibility_reason": "S08_FROZEN_CORPUS_PROVENANCE_VERIFIED",
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
        "application_id": application_id,
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
    checks = tuple(astuple(check) for check in result.checks)
    applicable = tuple(check.rule_id for check in result.checks)
    verdicts = tuple(
        (check.rule_id, check.verdict, tuple(check.reason_codes))
        for check in result.checks
    )
    selection = tuple(astuple(outcome) for outcome in result.selection_outcomes)
    normalization = tuple(
        astuple(outcome) for outcome in result.normalization_outcomes
    )
    mandatory_blocked = any(
        check.verdict != "consistent"
        and check.severity in {"critical", "major"}
        for check in result.checks
    )
    return {
        "application_id": run_spec["application_id"],
        "checks": checks,
        "applicable": applicable,
        "verdicts": verdicts,
        "selection": selection,
        "normalization": normalization,
        "route": "manual_review" if mandatory_blocked else "auto_complete",
    }


def main() -> int:
    _apply_process_boundaries()
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
    max_runtime_ms = int(declared.get("max_runtime_ms", 0))
    if max_runtime_ms <= 0:
        return _fail("checker max_runtime_ms limit is invalid")
    outcomes: list[dict[str, Any]] = []
    seen_item_ids: set[str] = set()
    for corpus_item in corpus:
        if not isinstance(corpus_item, dict):
            return _fail("corpus item is not an object")
        item_id = corpus_item.get("item_id")
        fixture = corpus_item.get("fixture")
        if (
            not isinstance(item_id, str)
            or not item_id
            or item_id in seen_item_ids
            or not isinstance(fixture, dict)
        ):
            return _fail("corpus item identity or fixture is invalid")
        seen_item_ids.add(item_id)
        try:
            outcome = _outcome_with_deadline(
                release, fixture, max_runtime_ms
            )
        except TimeoutError:
            return _fail(
                "checker run exceeded the declared max_runtime_ms limit"
            )
        except (TypeError, ValueError, KeyError):
            return _fail("corpus fixture produced a malformed checker run")
        outcome = {"corpus_item_id": item_id, **outcome}
        # Skipped corpus entries stay in the raw outcomes so the parent's
        # corpus comparison counts them instead of comparing empty sets.
        if "skipped" in outcome:
            outcomes.append(outcome)
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
    material = {"outcome_schema": _OUTCOME_SCHEMA, "outcomes": outcomes}
    digest = hashlib.sha256(
        json.dumps(
            material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    output = json.dumps({"digest": digest, **material})
    if len(output.encode("utf-8")) > _MAX_OUTPUT_BYTES:
        return _fail("validation output exceeds the output byte limit")
    sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
