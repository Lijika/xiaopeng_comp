# Ticket 31/S15 Fix Brief R8

Source review `docs/ROUND31_REVIEW_R8.md`

The R8 verdict is **FAIL**. `GOAL.md` and `STATUS.md` remain Manager-only and must stay unchanged.

## Required correction

1. Repair `test_reveal_admitted_evidence_damage_is_stopped` at `tests/test_s15_policy_owner.py:778-867`. The reveal path calls `_admitted_evidence` once at `task4_consistency/controlled/s01.py:11925-11927`; make the injected wrapper raise on that sole call and assert a call count of one.
2. Update the stale comments at `tests/test_s15_policy_owner.py:795-807` so they describe a post-C19 single evidence-load failure. Keep the exact governed vocabulary assertions at `:837-844`.
3. Preserve the stopped `SOURCE_EVIDENCE_UNAVAILABLE` outcome, one sanitized audit event, unchanged lifecycle/evidence revisions, stable idempotent replay, and absence of raw value, locator, internal path, credential, and caller sentinel.
4. Preserve the accepted R6 transaction-internal SQLite fault, rollback/baseline/retry evidence, both HTTP persistence/recovery variants, complete unknown-work-item baseline comparison, app-none v2 and historical v1 compatibility, R2 metadata-first/targeted-read boundaries, grouped frontend bundle, and GOAL/STATUS protection.

## File boundary

- The correction belongs in `tests/test_s15_policy_owner.py`.
- Production S01 code, frontend source and generated assets, OpenAPI files, `GOAL.md`, `STATUS.md`, and legacy raw/direct-object/bulk/download/export/print/copy surfaces should remain unchanged.

## Delivery evidence

- Report the focused admitted-evidence test and replay result together with the prior R6 transaction, HTTP, baseline, v1/v2, S04, and frontend verification results.
- Follow with a read-only Standards and Spec review. R8 review executed no tests, builds, evaluate, attack scripts, project scripts, or gates.
