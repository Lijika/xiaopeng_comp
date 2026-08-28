# Ticket 31/S15 R9 Adjudication Review

Date 2026-08-28

## Fixed point and scope

- The reviewed fixed point is the R7 delivery recorded by `docs/ROUND31_REVIEW_R7.md` and `docs/ROUND31_FIX_BRIEF_R7.md`; the current `HEAD` remains `2b4092195ffa643b17b3c17b62f6fe1971d144d4`.
- This adjudication also evaluates the R8 finding and fix brief against the current worktree, with issue #31 and repository architecture documents as the normative specification.
- The `code-review` skill was applied on independent Standards and Spec axes.
- Standards sources are `AGENTS.md`, `CONTEXT.md`, `ARCHITECTURE.md`, ADR-0002, ADR-0003, ADR-0004, ADR-0006, ADR-0007, ADR-0008, and the skill's Fowler smell baseline.
- Spec sources are GitHub issue #31, `docs/ROUND31_REVIEW_R8.md`, `docs/ROUND31_FIX_BRIEF_R8.md`, `CONTEXT.md`, `ARCHITECTURE.md`, and the applicable ADRs.
- Evidence is static source, call-path, diff, generated-file, and worktree inspection. No tests, builds, evaluate, attack scripts, project scripts, or gates were executed.

## Verdict

**PASS**

## Standards

**PASS with non-blocking judgement findings.**

No documented standards breach was found. `GOAL.md` and `STATUS.md` have no worktree change, OpenAPI generated files are untouched, and the delivery remains within the reviewed S01/S15, test, and frontend bundle boundaries.

Non-blocking Fowler judgements follow.

- Possible Duplicated Code at `tests/test_s15_policy_owner.py:1171-1178`, `:1606-1612`, and `:1641-1648`, where local wrappers call `_sync_idempotency` and then raise the same SQLite error.
- Possible Feature Envy at `tests/test_s15_policy_owner.py:115-130`, where the required persisted-state helper reads private store collections and revision state.
- Possible Feature Envy / long setup at `tests/test_s15_policy_owner.py:1500-1683`, where the HTTP case drives admission, claim, workspace, and private storage seams directly.
- Existing duplicated source-validation and visible-candidate selector judgements remain at `task4_consistency/controlled/s01.py:10111-10240` and `:9894-9909` versus `:11614-11630`.

## Spec

**PASS. R8 P1 is withdrawn.**

### R8 P1 adjudication — two `_admitted_evidence` calls are real and ordered correctly

Evidence follows.

- `_review_current_context` calls `_require_application_state_authority` at `task4_consistency/controlled/s01.py:10017-10022`.
- `_require_application_state_authority` performs its authority validation and calls `_admitted_evidence(app)` at `task4_consistency/controlled/s01.py:24894-24936`. This is the first call on the reveal path and occurs before the C19 decision.
- The reveal command then resolves C19 at `task4_consistency/controlled/s01.py:11811-11827`, assigns `governed_vocabulary` at `:11828-11834`, and performs its own evidence load at `:11925-11927`. This is the second call and occurs after C19 has confirmed the request vocabulary.
- The R7 test wrapper at `tests/test_s15_policy_owner.py:791-804` returns the original evidence on call one and raises on call two. Its `admitted_calls["n"] == 2` assertion at `:815` proves the intended call count statically, and its exact governed audit assertions at `:837-844` match the post-C19 failure position.
- The R8 claim that the reveal path has one call, and the resulting FAIL verdict, came from omitting the `_require_application_state_authority` call chain. The R8 P1 is therefore withdrawn.

Acceptance mapping follows.

- Issue #31 requires evidence and authority failures to fail closed while recording a bounded, sanitized attempted-action audit.
- `docs/ROUND31_FIX_BRIEF_R7.md:9-13` requires the admitted-evidence fault to be reachable after C19 and the audit to carry exact governed vocabulary.
- The current call-count injection reaches the intended post-C19 failure and the test checks stopped outcome, one audit event, unchanged revisions, stable replay, exact vocabulary, and raw absence.

### R6 transaction, HTTP, and baseline corrections

- The service fault calls the original `_sync_idempotency` and raises `sqlite3.OperationalError` inside the live transaction at `tests/test_s15_policy_owner.py:1156-1178`. `SQLiteTargetStore.persist()` starts the transaction and synchronizes audit/idempotency before its rollback handler at `task4_consistency/controlled/s01_store.py:621-673` and `:773-775`.
- `_persisted_baseline` captures store revision, applications, projections, lifecycle/evidence/review facts, work items, audit events, and idempotency at `tests/test_s15_policy_owner.py:115-130`; the failure case compares the baseline after reload and exercises a same-key fresh retry at `:1193-1213`.
- Both HTTP variants use the in-transaction sync fault at `tests/test_s15_policy_owner.py:1598-1648`. The second variant fails recovery reload. Assertions at `:1669-1683` require 503, `S03_UNAVAILABLE`, `STORAGE_UNAVAILABLE`, `no-store`, and absence of raw value, object references, credential, and internal path.

### Remaining S15 acceptance evidence

- Unknown `work_item_id` is filtered by visible scope, Lifecycle ownership, manual-review kind, assignment, and application binding before `QueryNotFound` at `task4_consistency/controlled/s01.py:11614-11630`. The test compares the complete captured baseline before and after the request and after reload at `tests/test_s15_policy_owner.py:1385-1411`.
- The successful `app=None` v2 nullable audit shape persists and reloads with omitted vocabulary at `tests/test_s15_policy_owner.py:1686-1754`; governed v2 and historical v1 projections remain at `:1008-1104`.
- Metadata-only `evidence_eligible is True` gating remains before admitted evidence and source reads at `task4_consistency/controlled/s01.py:11722-11765`, with false/missing zero-read instrumentation at `tests/test_s15_policy_owner.py:499-678`. Selected registered integrity reads remain limited to the result object and requested observation object at `s01.py:10176-10240`.
- Production catches `sqlite3.Error` and contains recovery reload failure at `task4_consistency/controlled/s01.py:11503-11531`. Audit and replay filter raw fields, and no direct-object, bulk, download, export, print, or copy surface was added.
- Expiry is checked directly in `revealedHere` and per-link eligibility controls reveal buttons at `frontend/src/components/ReviewWorkPanel.tsx:978-1060`; rerender and mixed-link cases are present at `frontend/src/components/ReviewWorkPanel.test.tsx:2804-2915`.
- The generated bundle group is present as an updated `task4_consistency/web/static/react/index.html`, deleted `assets/index-ChVt9ebc.js`, and untracked `assets/index-DwR5zzmb.js`. OpenAPI files, `GOAL.md`, and `STATUS.md` are unchanged.

## Residual risk and unexecuted verification

- Runtime execution remains unverified under the prohibition, including actual test outcomes, SQLite rollback behavior, HTTP adapter mapping, v1/v2 projection, and browser rendering.
- The new frontend bundle is untracked and can be omitted by tracked-only delivery selection; all three generated files require grouped delivery.
- The worktree contains unrelated untracked documents, fixtures, data, and output files. Delivery selection should include only S15 changes, grouped frontend assets, and review evidence.
- No R9 fix brief is required because the R8 P1 is withdrawn and no blocking Spec finding remains.

Standards and Spec both PASS. The R8 P1 is explicitly withdrawn after complete static call-chain tracing.
