# Ticket 31/S15 Fix Brief R5

Source review `docs/ROUND31_REVIEW_R5.md`

The R5 verdict is **FAIL**. `GOAL.md` and `STATUS.md` remain Manager-only and must stay unchanged.

## Required corrections

1. Contain real SQLite persistence failures. Convert `sqlite3.Error` from `SQLiteTargetStore.persist()` into the existing domain storage failure, or catch it at `_record_reveal_outcome`. Preserve transaction rollback, contain recovery reload failure, and return stable `unavailable/STORAGE_UNAVAILABLE` with no raw value.

2. Replace the false persistence-failure injection at `tests/test_s15_policy_owner.py:1120-1185` with a fault from the actual staged `persist()` call after audit and idempotency are staged. Assert persisted-state rollback, zero partial audit, zero idempotency binding, and stable replay behavior.

3. Add HTTP coverage for actual persistence failure and recovery-reload failure. Assert 503, exact `STORAGE_UNAVAILABLE`, `Cache-Control: no-store`, and absence of raw value, source locator, credential, and internal path.

4. Add successful v2 nullable-shape evidence. Persist and reload a visible-work-item `app=None` outcome, then assert schema `/2`, `lifecycle_revision is None`, `evidence_revision is None`, omitted `purpose`, `verification_reason`, and `classification`, no raw fields, and one stable replay binding. Retain governed v2 success and historical v1 readability.

5. Add an unknown `work_item_id` case to existence-hiding tests. Assert `QueryNotFound` and an unchanged reveal-audit event set and persisted state.

6. Preserve the accepted R4/R3/R2 behavior. Keep metadata-first eligibility and zero source reads for false/missing links, selected result-plus-observation reads, governed vocabulary ordering, raw filtering, revision and idempotency invariants, visible authority auditing, expiry and per-link UI behavior, `no-store`, historical v1 readability, and retired raw/direct-object/bulk/download/export/print/copy boundaries.

## File boundary

- Production exception normalization belongs in `task4_consistency/controlled/s01.py` or `task4_consistency/controlled/s01_store.py`, using one existing storage boundary.
- Regression changes belong in `tests/test_s15_policy_owner.py` and the existing S15 HTTP coverage.
- Frontend source and generated files already satisfy the reviewed UI requirements.
- `GOAL.md`, `STATUS.md`, unrelated slices, and public raw/direct-object/bulk/download/export/print/copy surfaces remain protected.

## Delivery evidence

- Report exact focused commands and results for actual SQLite rollback, S15 policy ownership, HTTP persistence/recovery mapping, v1/v2 audit compatibility, affected S04 assertions, and `ReviewWorkPanel`.
- Deliver updated `task4_consistency/web/static/react/index.html`, deletion of `assets/index-ChVt9ebc.js`, and addition of `assets/index-DwR5zzmb.js` as one group.
- A subsequent read-only Standards and Spec review must inspect the repaired paths. R5 review executed no tests, builds, evaluate, attack scripts, project scripts, or gates.
