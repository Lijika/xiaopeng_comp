# Ticket 31/S15 Fix Brief R4

Source review `docs/ROUND31_REVIEW_R4.md`

The R4 verdict is **FAIL**. The same OMP session owns these evidence corrections. `GOAL.md` and `STATUS.md` remain Manager-only and must stay unchanged.

## Required corrections

1. Contain real SQLite persistence failures. Convert `sqlite3.Error` from `SQLiteTargetStore.persist()` to the existing domain storage failure or catch it at `_record_reveal_outcome`, then return stable `unavailable/STORAGE_UNAVAILABLE` after rollback and contained recovery reload.

2. Replace the case labelled persistence failure at `tests/test_s15_policy_owner.py:1120-1148` with a fault raised by the actual staged `SQLiteTargetStore.persist()` transaction, using `sqlite3.OperationalError` or the converted domain exception. Assert recovery restores the persisted baseline with zero partial audit and zero idempotency binding.

3. Add HTTP adapter coverage for the actual persistence-failure and recovery-reload variants. Assert 503, the exact stable error and reason code, `Cache-Control: no-store`, and absence of raw value, source locator, credential, and internal path.

4. Complete v2 shape evidence. Persist and reload one safe pre-C19 `s15-reveal-audit/2` event with `app=None`, then assert nullable lifecycle/evidence revisions and omitted purpose, verification reason, and classification. Retain the governed v2 success and historical v1 reload assertions.

5. Add an unidentifiable work-item case to the existence-hiding regression. Use an unknown `work_item_id`, assert `QueryNotFound`, and prove the reveal-audit event set remains unchanged.

6. Preserve the accepted R3 production behavior and every R2 boundary. Keep schema `/2`, historical `/1` readability, stable `app=None` response construction, contained recovery reload, visible authority audit and replay, metadata-first eligibility, zero source read on ineligible links, selected-object reads, governed vocabulary, raw filtering, revision/idempotency behavior, expiry UI, per-link UI, and retired raw/direct-object/bulk/download/export/print/copy surfaces.

## File boundary

- The production exception conversion belongs in `task4_consistency/controlled/s01.py` or `task4_consistency/controlled/s01_store.py`, using one existing domain boundary.
- Regression changes belong in `tests/test_s15_policy_owner.py` and its existing HTTP coverage.
- Frontend source and generated files already satisfy the reviewed R2 and R3 UI requirements.
- `GOAL.md`, `STATUS.md`, unrelated slices, and public raw/direct-object/bulk/download/export/print/copy surfaces remain protected.

## Delivery evidence

- Report exact focused commands and results for S15 policy ownership, actual SQLite persistence rollback, S15 HTTP mapping, v1/v2 audit compatibility, affected S04 assertions, and `ReviewWorkPanel`.
- Include `task4_consistency/web/static/react/index.html`, deletion of `assets/index-ChVt9ebc.js`, and addition of `assets/index-DwR5zzmb.js` as one generated group.
- A subsequent read-only Standards and Spec review must inspect the repaired cases. R4 executed no tests, builds, evaluate, attack scripts, project scripts, or gates.
