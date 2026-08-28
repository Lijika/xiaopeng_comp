# Ticket 31/S15 Fix Brief R3

Source review `docs/ROUND31_REVIEW_R3.md`

The R3 verdict is **FAIL**. The same OMP session owns these corrections. `GOAL.md` and `STATUS.md` remain Manager-only and must stay unchanged.

## Required corrections

1. Version the reveal audit contract. Publish `s15-reveal-audit/2` for nullable revisions and optional governed vocabulary while retaining historical `/1` readability, or preserve the established `/1` field shape and value domains. Add focused compatibility assertions.

2. Repair `_record_reveal_outcome` for `app=None`. Use `work_item["application_id"]` in the unavailable response and contain failures from recovery reload. Add separate audit-write, persistence, and recovery-reload fault cases that assert a stable no-value response, `no-store`, zero raw content, and no partial idempotency binding.

3. Audit visible work-item authority damage. Establish a minimally trusted, scope-checked work reference before full reconstruction and route reconstruction exceptions through the common outcome writer when audit storage is available. Preserve `QueryNotFound` existence hiding for unauthorized, cross-tenant, and unidentifiable resources. Cover bootstrap storage outage, visible authority damage, unauthorized access, and cross-tenant access separately.

4. Make the admitted-evidence damage regression reachable. Use `MANUAL_REVIEW`, `EVIDENCE_VERIFICATION`, and `RESTRICTED` in `test_reveal_admitted_evidence_damage_is_stopped` and its replay call. Keep caller-sentinel leak assertions in the pre-C19 eligibility or region cases. Retain the exact stopped reason, one audit event, idempotent replay, unchanged revisions, and raw-data absence assertions.

5. Preserve the statically accepted R2 behavior. Keep metadata-only eligibility before every evidence copy and source read, exact result-plus-selected-object reads, governed audit vocabulary, safe result filtering, audit/idempotency atomic persistence, unchanged business revisions, expiry-time rendering, per-link UI eligibility, `no-store`, and all retired raw/direct-object/bulk/download/export/print/copy boundaries.

6. Deliver the frontend generated group together. Include the modified `task4_consistency/web/static/react/index.html`, deletion of `assets/index-ChVt9ebc.js`, and addition of `assets/index-DwR5zzmb.js`. Exclude unrelated untracked files.

## Allowed file boundary

- Production fixes belong in `task4_consistency/controlled/s01.py`.
- Focused regressions belong in `tests/test_s15_policy_owner.py` and the existing S15 HTTP test location when adapter coverage requires it.
- A schema-reader or schema fixture may change only when the chosen audit versioning approach requires it.
- Existing frontend source and generated files already satisfy the reviewed UI requirements.
- `GOAL.md`, `STATUS.md`, unrelated slices, and public raw/direct-object/bulk/download/export/print/copy surfaces remain protected boundaries.

## Delivery evidence

- Report the exact focused test commands and results for S15 policy ownership, S15 HTTP mapping, audit schema compatibility, affected S04 assertions, and `ReviewWorkPanel`.
- Report the three-file generated bundle group and the untracked-file selection.
- A subsequent read-only Standards and Spec review must inspect the repaired paths. The current R3 review intentionally executed no tests, builds, evaluate, scripts, or gates.
