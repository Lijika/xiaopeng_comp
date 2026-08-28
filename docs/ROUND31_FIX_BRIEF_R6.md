# Ticket 31/S15 Fix Brief R6

Source review `docs/ROUND31_REVIEW_R6.md`

The R6 verdict is **FAIL**. `GOAL.md` and `STATUS.md` remain Manager-only and must stay unchanged.

## Required corrections

1. Exercise a genuine `sqlite3.OperationalError` from the staged `SQLiteTargetStore.persist()` transaction after audit and idempotency SQL has been staged or written. Preserve the existing production `sqlite3.Error` catch, transaction rollback, contained recovery reload, sanitized `503` / `STORAGE_UNAVAILABLE` / `Cache-Control: no-store` contract, and raw/locator/credential/path absence.
2. Assert the complete persisted baseline after the in-transaction fault. Reload the store and compare store revision, audit events, idempotency bindings, projections, lifecycle/evidence/review facts, and work items. Assert a same-key retry has no poisoned binding and follows the documented fresh-attempt/replay behavior.
3. Apply the same in-transaction persistence fault to both HTTP variants. Keep the recovery-reload failure variant and its exact status, reason, header, and sanitized-body assertions.
4. Extend the unknown `work_item_id` regression to snapshot and compare complete persisted state before and after `QueryNotFound`, then reload and compare again. Keep existence hiding and zero audit/idempotency side effects.
5. Preserve the accepted R5/R4/R3/R2 behavior, including metadata-first eligibility with zero source reads, selected result-plus-observation reads, authority and C19 ordering, revision/idempotency invariants, raw filtering, expiry/per-link UI state, historical v1 readability, grouped frontend bundle delivery, and retired raw/direct-object/bulk/download/export/print/copy boundaries.

## File boundary

- Fault-injection and regression evidence belongs in `tests/test_s15_policy_owner.py` and the existing S15 HTTP coverage. A production exception normalization change is unnecessary unless the existing `sqlite3.Error` boundary regresses.
- Keep `GOAL.md`, `STATUS.md`, OpenAPI generated files, unrelated slices, and protected raw/direct-object/bulk/download/export/print/copy surfaces unchanged.
- Deliver `task4_consistency/web/static/react/index.html`, deletion of `assets/index-ChVt9ebc.js`, and addition of `assets/index-DwR5zzmb.js` as one generated group.

## Delivery evidence

- Report exact focused commands and results for the in-transaction SQLite rollback, both HTTP persistence/recovery variants, unknown-resource baseline, v1/v2 compatibility, affected S04 assertions, and `ReviewWorkPanel`.
- Follow with a read-only Standards and Spec review of the repaired paths. R6 review itself executed no tests, builds, evaluate, attack scripts, project scripts, or gates.
