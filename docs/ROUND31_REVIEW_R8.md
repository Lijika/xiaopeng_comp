# Ticket 31/S15 Read-only Review R8

Date 2026-08-28

## Fixed point and scope

- The review compares the current worktree with `docs/ROUND31_REVIEW_R7.md` and `docs/ROUND31_FIX_BRIEF_R7.md`.
- The `code-review` skill was applied on independent Standards and Spec axes.
- Spec sources are GitHub issue #31, the R7 review and fix brief, `CONTEXT.md`, `ARCHITECTURE.md`, and ADR-0002, ADR-0003, ADR-0004, ADR-0006, ADR-0007, and ADR-0008.
- Evidence is static source, call-path, diff, generated-file, and worktree inspection. No tests, builds, evaluate, attack scripts, project scripts, or gates were executed.

## Verdict

**FAIL**

## Standards

**PASS with non-blocking judgement findings.**

No documented standards breach was found. The current change is confined to the S15 test additions and the previously reviewed S01/frontend/generated bundle group. `GOAL.md` and `STATUS.md` have no worktree change, and OpenAPI generated files are untouched.

Non-blocking Fowler judgements follow.

- Possible Duplicated Code at `tests/test_s15_policy_owner.py:1155-1161`, `:1606-1610`, and `:1641-1646`, where each test wrapper calls `_sync_idempotency` and then raises the same SQLite error.
- Possible Feature Envy at `tests/test_s15_policy_owner.py:115-130`, where the required complete-state baseline helper reads private store collections and revision state.
- Possible Feature Envy / long setup at `tests/test_s15_policy_owner.py:1500-1665`, where the HTTP case drives admission, claim, workspace, and fault injection directly through private seams.
- Existing duplicated source validation and visible-work-item selection judgements remain at `task4_consistency/controlled/s01.py:10111-10240` and `:9894-9909` versus `:11614-11630`.

## Spec

**FAIL with one P1 test-regression defect.**

### P1 — admitted-evidence fault is never triggered by the two-call assertion

Evidence follows.

- The test wrapper is defined at `tests/test_s15_policy_owner.py:791-802`. It returns the original evidence for call one and raises only when `admitted_calls["n"] >= 2`.
- The same test requires `admitted_calls["n"] == 2` and a stopped result at `tests/test_s15_policy_owner.py:815-817`.
- The reveal production path calls `_admitted_evidence` once at `task4_consistency/controlled/s01.py:11925-11927`.
- `_review_current_context` at `task4_consistency/controlled/s01.py:10017-10103` validates application, lifecycle, run, projection, and context authority without calling `_admitted_evidence`. Its authority helper later calls `_admitted_evidence` only in the separate validation path at `:24936`, outside the reveal call path used here.
- Consequently the wrapper returns normal evidence on its sole reveal call, no exception reaches the stopped-outcome handler, `admitted_calls["n"]` cannot equal two, and the test's required stopped result cannot be produced.
- The governed-vocabulary assertion added at `tests/test_s15_policy_owner.py:837-844` matches the intended post-C19 ordering. The call-count gate prevents that assertion from being reached correctly.

Acceptance mapping follows.

- Issue #31 requires authority/evidence failures to fail closed with a sanitized attempted-action audit and no raw value.
- `docs/ROUND31_FIX_BRIEF_R7.md:9-13` requires this admitted-evidence regression to be reachable after C19, with exact governed vocabulary, stopped reason, stable replay, unchanged revisions, and raw-data absence.
- The current test does not reach the injected authority failure, so the required regression evidence is absent and the focused test itself is inconsistent with the production call graph.

Required fix follows.

- Make the wrapper raise on its sole invocation, for example by changing the threshold to `admitted_calls["n"] >= 1` and asserting `admitted_calls["n"] == 1`.
- Update the comments at `tests/test_s15_policy_owner.py:795-802` to describe the single reveal evidence-load call after C19. Remove the claim that a first call belongs to current-context reconstruction.
- Retain the exact post-C19 audit assertions for `purpose == "MANUAL_REVIEW"`, `verification_reason == "EVIDENCE_VERIFICATION"`, and `classification == "RESTRICTED"`, along with stopped reason, one audit event, unchanged revisions, stable replay, and raw/locator/sentinel absence.

## Accepted R6 and R7 behavior

- The staged SQLite fault now calls the original `_sync_idempotency` before raising `sqlite3.OperationalError` at `tests/test_s15_policy_owner.py:1156-1177`; this is inside `SQLiteTargetStore.persist()` after transaction start and synchronization at `task4_consistency/controlled/s01_store.py:621-673`, with rollback at `:773-775`. The complete baseline and same-key retry assertions remain at `tests/test_s15_policy_owner.py:1191-1210`.
- HTTP persistence and recovery variants reuse the in-transaction sync fault at `tests/test_s15_policy_owner.py:1598-1646` and retain the 503, exact `STORAGE_UNAVAILABLE`, `no-store`, and sensitive-field absence checks at `:1651-1665`.
- Unknown `work_item_id` existence hiding compares the complete persisted baseline before and after the request and after reload at `tests/test_s15_policy_owner.py:1383-1399`; production raises `QueryNotFound` before command outcome handling at `task4_consistency/controlled/s01.py:11617-11630`.
- The v2 nullable `app=None` audit persists and reloads at `tests/test_s15_policy_owner.py:1668-1736`; governed v2 and historical v1 compatibility remains at `:1008-1104`.
- Metadata-only eligibility remains before admitted evidence and any source read at `task4_consistency/controlled/s01.py:11722-11765`. Selected registered integrity reads remain limited to the result object and requested observation object at `:10176-10240`.
- Production catches `sqlite3.Error` and contains recovery reload failure at `task4_consistency/controlled/s01.py:11503-11531`. Expiry and per-link UI behavior remain at `frontend/src/components/ReviewWorkPanel.tsx:978-1060`, with tests at `frontend/src/components/ReviewWorkPanel.test.tsx:2804-2915`.
- The generated bundle group remains coherent in the worktree. `index.html` references untracked `assets/index-DwR5zzmb.js`, and `assets/index-ChVt9ebc.js` is deleted. OpenAPI files, legacy raw/direct-object/bulk/download/export/print/copy surfaces, `GOAL.md`, and `STATUS.md` have no worktree change.

## Residual risk and unexecuted verification

- The corrected admitted-evidence test, runtime SQLite rollback, HTTP mapping, v1/v2 projection, browser rendering, and all acceptance assertions remain unverified because execution was prohibited.
- The generated frontend bundle is untracked and can be omitted by tracked-only delivery selection.
- The worktree contains unrelated untracked documents, fixtures, data, and output files. Delivery selection must remain limited to S15 changes, the grouped frontend assets, and review evidence.

Standards has zero hard findings. Spec has one P1 finding, caused by the unreachable second-call fault injection in the admitted-evidence regression.
