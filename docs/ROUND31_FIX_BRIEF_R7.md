# Ticket 31/S15 Fix Brief R7

Source review `docs/ROUND31_REVIEW_R7.md`

The R7 verdict is **FAIL**. `GOAL.md` and `STATUS.md` remain Manager-only and must stay unchanged.

## Required correction

1. Correct `test_reveal_admitted_evidence_damage_is_stopped` at `tests/test_s15_policy_owner.py:778-850`. The `_admitted_evidence` fault occurs after C19 has accepted the governed defaults. Assert exact audit values `purpose == "MANUAL_REVIEW"`, `verification_reason == "EVIDENCE_VERIFICATION"`, and `classification == "RESTRICTED"`.
2. Replace the stale pre-C19 comment at `tests/test_s15_policy_owner.py:823-825` and align the test docstring with its post-C19 execution point.
3. Preserve the exact stopped result and reason, one audit event, unchanged lifecycle/evidence revisions, stable idempotent replay, and absence of raw value, source locator, internal path, credential, and caller-controlled sentinel.
4. Preserve the accepted R6 fixes for in-transaction SQLite rollback, full baseline reload comparison, same-key retry, both HTTP persistence/recovery variants, and unknown work-item persisted-state equality.
5. Preserve app-none v2, historical v1 compatibility, metadata-first zero reads, selected binding reads, UI expiry/link state, grouped bundle delivery, and the protected legacy/GOAL/STATUS boundaries.

## File boundary

- The required correction belongs only in `tests/test_s15_policy_owner.py`.
- Production S15 code, frontend source and bundle, OpenAPI generated files, `GOAL.md`, `STATUS.md`, and raw/direct-object/bulk/download/export/print/copy surfaces should remain unchanged for this fix.

## Delivery evidence

- Report the focused S15 test command and result for the corrected admitted-evidence case and its replay, along with the R6 transaction, HTTP, baseline, v1/v2, and frontend checks required by the prior brief.
- Follow with a read-only Standards and Spec review. R7 review executed no tests, builds, evaluate, attack scripts, project scripts, or gates.
