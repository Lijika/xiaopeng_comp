# Ticket #32 / S16 R20 Delivery

## Commits

- Implementation: `d00db1a` (`fix(s16): close R20 fence and replay gaps`)
- Delivery: this document, committed separately after implementation.

## Implemented fixes

1. Existing backup operation fences are revalidated against the complete binding history before stale checks, active-fence CAS use, worker health decisions, and related runtime paths. Scope, digest, source fence, status, identity JSON, and manifest JSON inconsistencies fail closed through the migration-failure record.
2. Operation-fence migration requires a non-null positive integer `source_fence`. NULL, malformed, zero, negative, and out-of-range values remain unavailable and do not create a derived fence.
3. Restore replay derives a positive fence from the completed job fence and uses the same value for replay delete and verification. Replay bindings therefore satisfy the operation-fence schema and can form a verified replay fact.
4. Regression snapshots now include operation-fence rows. Coverage includes NULL source fences, runtime metadata tampering, cross-table status conflicts, old-schema damage, replay/restore readiness, cross-scope references, crash recovery, and repair-forward paths.

## Allowed targeted verification

All commands below were run from the repository root.

- `.venv/bin/pytest -q tests/test_s16_controlled.py -k "operation_fence or stale_fence or active_fence or owner_health"`
  - Result: `8 passed, 101 deselected`
- `.venv/bin/pytest -q tests/test_s16_controlled.py -k "source_fence or migration or legacy_schema"`
  - Result: `6 passed, 103 deselected`
- `.venv/bin/pytest -q tests/test_s16_controlled.py -k "replay or restore or readiness or fence_zero or repair_forward"`
  - Result: `19 passed, 90 deselected`
- `.venv/bin/pytest -q tests/test_s16_controlled.py -k "effect_state or staged_complete or staged_committed or source_fence or replay or cross_scope or repair_forward"`
  - Result: `18 passed, 91 deselected`

## Scope and remaining verification

- Modified paths are limited to `task4_consistency/controlled/s16.py`, `tests/test_s16_controlled.py`, and this delivery document.
- GOAL.md, STATUS.md, `docs/ROUND32_PLAN.md`, prior R1-R20 review/brief/delivery documents, and unrelated paths were left unchanged.
- Full pytest, full Playwright, `ci_gate.sh`, evaluate, attack probes, build, lint, typecheck, generate, and other project commands were not executed.
- HTTP, React, OpenAPI, deployment, and production-backend behavior remain unverified in this implementation-only delivery.
