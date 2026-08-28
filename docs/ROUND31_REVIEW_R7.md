# Ticket 31/S15 Read-only Review R7

Date 2026-08-28

## Fixed point and scope

- The fixed point remains `2b4092195ffa643b17b3c17b62f6fe1971d144d4`, equal to `HEAD` and recorded in `docs/ROUND31_REVIEW_R6.md`.
- The review covers the current worktree relative to the R6 review and fix brief, with emphasis on the added R6 regression evidence in `tests/test_s15_policy_owner.py`.
- The `code-review` skill was applied on independent Standards and Spec axes.
- Standards sources are `AGENTS.md`, `CONTEXT.md`, `ARCHITECTURE.md`, the applicable ADRs, and the skill's Fowler smell baseline.
- Spec sources are GitHub issue #31, `docs/ROUND31_REVIEW_R6.md`, `docs/ROUND31_FIX_BRIEF_R6.md`, `CONTEXT.md`, `ARCHITECTURE.md`, and ADR-0002, ADR-0003, ADR-0004, ADR-0006, ADR-0007, and ADR-0008.
- Evidence is limited to static source, call-path, diff, generated-file, and worktree inspection. Tests, builds, evaluate, attack scripts, project scripts, and gates were not executed.

## Verdict

**FAIL**

## Standards

**PASS with non-blocking judgement findings.**

No documented standards breach was found. R6 production behavior and OpenAPI generated files are unchanged. `GOAL.md` and `STATUS.md` have no worktree change. The S15 delivery remains within the reviewed production, test, and generated frontend boundaries.

Non-blocking Fowler judgements follow.

- Possible Feature Envy appears at `tests/test_s15_policy_owner.py:115-130`. `_persisted_baseline` reads the private store revision and internal authority collections. The helper directly serves the R6 persisted-state requirement.
- Possible Duplicated Code appears at `tests/test_s15_policy_owner.py:1155-1161`, `:1590-1594`, and `:1625-1630`. Each wrapper calls `_sync_idempotency` and raises the same `sqlite3.OperationalError`. A local fault helper could reduce drift.
- Possible Feature Envy / long setup appears at `tests/test_s15_policy_owner.py:1500-1665`. The HTTP regression replaces `_connect` and `_sync_idempotency` and depends on the documented request connection sequence. This coupling serves the specified persistence and recovery fault evidence.
- The existing possible Duplicated Code remains at `task4_consistency/controlled/s01.py:10111-10240` and at `:9894-9909` versus `:11614-11630`.

## Spec

**FAIL with one P1 regression defect.**

### P1 — admitted-evidence regression asserts the wrong post-C19 audit shape

Evidence follows.

- `tests/test_s15_policy_owner.py:793-801` deliberately uses the governed defaults `MANUAL_REVIEW`, `EVIDENCE_VERIFICATION`, and `RESTRICTED`, then injects `_admitted_evidence` failure at `:789-792`.
- Production resolves C19 and assigns `governed_vocabulary` at `task4_consistency/controlled/s01.py:11811-11834`. `_admitted_evidence` is called later at `:11925-11927`.
- The stopped outcome therefore reaches `_record_reveal_outcome` with governed vocabulary. The writer persists `purpose`, `verification_reason`, and `classification` at `task4_consistency/controlled/s01.py:11494-11497`.
- `tests/test_s15_policy_owner.py:823-826` describes the fault as pre-C19 and asserts that `purpose` is absent. The comment and assertion contradict the static execution order, so this required regression cannot pass as written.

Acceptance mapping follows.

- Issue #31 requires stable attempted-action audit behavior without caller-controlled or raw content.
- `docs/ROUND31_FIX_BRIEF_R3.md:15` made this regression reachable by using the governed defaults and placed caller-sentinel leak assertions in pre-C19 eligibility or region cases.
- The R6 brief requires preservation of the accepted R3/R2 authority, C19 ordering, audit filtering, and regression behavior.

Required fix follows.

- Keep the `_admitted_evidence` failure after C19 and replace the absent-purpose assertion with exact governed values for `purpose`, `verification_reason`, and `classification`.
- Update the stale pre-C19 comment and docstring. Retain the exact stopped reason, one audit event, unchanged revisions, stable replay, and raw/locator/sentinel absence assertions.
- Keep caller-controlled sentinel omission assertions in the existing metadata-first pre-C19 qualification cases.

## Accepted R6 corrections

- The service-level fault calls the original `_sync_idempotency` and then raises `sqlite3.OperationalError` at `tests/test_s15_policy_owner.py:1140-1161`. This executes inside `SQLiteTargetStore.persist()` after `BEGIN IMMEDIATE`, audit synchronization, and idempotency insertion at `task4_consistency/controlled/s01_store.py:621-673`; the store rollback handler is at `:773-775`.
- The complete R6 authority baseline helper is at `tests/test_s15_policy_owner.py:115-130`. The persistence case compares it after a real reload at `:1175-1182`, then proves a same-key fresh retry at `:1183-1195`.
- Both HTTP variants reuse the in-transaction `_sync_idempotency` fault at `tests/test_s15_policy_owner.py:1582-1630`. The second variant also fails the recovery reload. Both assert 503, exact `STORAGE_UNAVAILABLE`, `no-store`, and absence of raw value, object references, credential, and internal path at `:1651-1665`.
- The unknown work-item case captures the persisted baseline, asserts equality immediately after `QueryNotFound`, reloads, and compares again at `tests/test_s15_policy_owner.py:1367-1393`.

## Preserved behavior

- Production catches `sqlite3.Error`, contains recovery reload failure, and returns the sanitized storage outcome at `task4_consistency/controlled/s01.py:11503-11531`.
- Metadata-only `evidence_eligible is True` gating remains before admitted evidence, assembly, and source reads at `task4_consistency/controlled/s01.py:11722-11765`. False/missing qualification zero-read checks remain at `tests/test_s15_policy_owner.py:499-678`.
- Selected binding integrity remains limited to the result object and requested observation object at `task4_consistency/controlled/s01.py:10176-10240`, with the sibling-read regression at `tests/test_s15_policy_owner.py:680-754`.
- Successful `app=None` v2 nullable persistence and reload remain at `tests/test_s15_policy_owner.py:1668-1736`. Governed v2 and historical v1 compatibility remain at `:1008-1092`.
- Expiry and per-link UI behavior remain at `frontend/src/components/ReviewWorkPanel.tsx:978-1060`, with rerender and mixed-link tests at `frontend/src/components/ReviewWorkPanel.test.tsx:2804-2915`.
- The generated group remains coherent in the worktree. `task4_consistency/web/static/react/index.html` references untracked `assets/index-DwR5zzmb.js`, and tracked `assets/index-ChVt9ebc.js` is deleted. Delivery must include all three changes together.
- OpenAPI generated files, legacy raw/direct-object/bulk/download/export/print/copy surfaces, `GOAL.md`, and `STATUS.md` have no worktree change.

## Residual risk and unexecuted verification

- Runtime transaction rollback, request connection counts, HTTP mapping, audit projection, frontend rendering, and all assertions remain unverified under the execution prohibition.
- The new frontend bundle remains untracked and can be omitted by tracked-only delivery selection.
- The worktree contains unrelated untracked files. Ticket delivery selection should remain limited to the S15 production/tests, grouped frontend assets, and round review evidence.

Standards has zero hard findings. Spec has one P1 finding, with the contradictory admitted-evidence audit assertion as the blocking issue.
