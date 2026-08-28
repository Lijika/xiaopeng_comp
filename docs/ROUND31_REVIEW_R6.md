# Ticket 31/S15 Read-only Review R6

Date 2026-08-28

## Fixed point and scope

- The reviewed fixed point is `2b4092195ffa643b17b3c17b62f6fe1971d144d4`, recorded by `docs/ROUND31_REVIEW_R5.md` and equal to `HEAD`.
- The review covers the current working-tree diff from that point, including tracked S15 changes and the untracked generated bundle and delivery evidence visible in `git status --short`.
- The `code-review` skill was applied with independent Standards and Spec axes.
- Spec sources are GitHub issue #31, `docs/ROUND31_REVIEW_R5.md`, `docs/ROUND31_FIX_BRIEF_R5.md`, `CONTEXT.md`, `ARCHITECTURE.md`, and ADR-0002, ADR-0003, ADR-0004, ADR-0006, ADR-0007, and ADR-0008.
- Evidence is static source, call-path, diff, and generated-file inspection. No tests, builds, evaluate, attack scripts, project scripts, or gates were executed.

## Verdict

**FAIL**

## Standards

**PASS with non-blocking judgement findings.**

No documented standards breach was found. `GOAL.md` and `STATUS.md` have no worktree change. OpenAPI generated files are untouched, and the production edit remains within the S01 authority boundary.

Non-blocking Fowler judgements follow.

- Possible Duplicated Code at `task4_consistency/controlled/s01.py:10111-10174` and `:10176-10240`. Both helpers repeat C-DEMO and registered result-object integrity checks. A shared result-authority helper would reduce drift while leaving bulk and selected-observation policy separate.
- Possible Duplicated Code at `task4_consistency/controlled/s01.py:9894-9909` and `:11614-11630`. Both selectors construct a visible-scope, Lifecycle-owned, assigned manual-review candidate. The pre-audit selector and full authority reconstruction have distinct responsibilities, so this is a maintenance judgement.
- Possible Feature Envy / long test setup at `tests/test_s15_policy_owner.py:1077-1218` and `:1478-1634`, where tests replace private store seams and embed the complete HTTP admission/claim/workspace flow. The cases are tied to the requested fault evidence and remain non-blocking.

## Spec

**FAIL with one P1 acceptance-blocking evidence gap and one P2 evidence gap.**

### P1 — staged SQLite failure does not exercise transaction rollback

Evidence follows.

- `tests/test_s15_policy_owner.py:1122-1145` patches `SQLiteTargetStore._connect` to raise `sqlite3.OperationalError` before calling the original connector.
- `SQLiteTargetStore.persist()` enters `with self._connect() as connection` at `task4_consistency/controlled/s01_store.py:621`; `BEGIN IMMEDIATE` and the transaction `try/except` containing `connection.rollback()` start only at `:622-623` and end at `:773-775`.
- The test therefore reaches the production `sqlite3.Error` catch at `task4_consistency/controlled/s01.py:11503-11522`, and it verifies reload/baseline/retry at `tests/test_s15_policy_owner.py:1159-1181`, yet no SQLite transaction exists and no audit or idempotency SQL has run when the fault is raised.
- The HTTP variants at `tests/test_s15_policy_owner.py:1563-1570` and `:1584-1599` use the same pre-connection fault. They prove sanitized 503 handling and contained recovery failure through the adapter assertions at `:1620-1634`, while leaving rollback untested.

Acceptance mapping follows.

- Issue #31 requires audit and idempotency atomicity with zero partial success on storage failure.
- `docs/ROUND31_FIX_BRIEF_R5.md:9-13` requires a genuine staged `persist()` `OperationalError` after audit and idempotency are staged, rollback/baseline/replay evidence, and HTTP persistence/recovery variants.
- ADR-0004 and ADR-0008 require protected facts and required audit to commit atomically and to fail closed on authoritative storage failure.

Required fix follows.

- Inject `sqlite3.OperationalError` inside the live `persist()` transaction after the audit and idempotency synchronization has executed, for example by wrapping the staged store's `_sync_idempotency` or a post-write transaction seam while calling the original first.
- Assert that `connection.rollback()` is reached, reload returns the complete pre-attempt persisted baseline, no reveal audit row or idempotency binding remains, and a same-key retry is a fresh attempt with stable replay semantics.
- Reuse the in-transaction fault for both HTTP variants, with the second variant failing the recovery reload after the persistence exception; retain the current 503, exact `STORAGE_UNAVAILABLE`, `no-store`, and raw/locator/credential/path absence assertions.

### P2 — unknown work-item test does not compare complete persisted state

Evidence follows.

- Production filters by work-item id, manual-review kind, Lifecycle owner, visible scope, and assigned subject at `task4_consistency/controlled/s01.py:11614-11630`, then raises `QueryNotFound` before command fingerprint, audit, or idempotency outcome handling.
- `tests/test_s15_policy_owner.py:1353-1374` now exercises an unknown identifier and checks `QueryNotFound`, an empty reveal-audit projection, and absence of the unknown idempotency key.
- The regression does not snapshot and compare the full persisted state or store revision. It does not assert unchanged projections, lifecycle/evidence facts, work items, or other idempotency rows, despite `docs/ROUND31_FIX_BRIEF_R5.md:5-6` requiring persisted state to remain unchanged.

Acceptance mapping follows.

- Issue #31 includes existence probing in the adversarial matrix and requires no observable side effect.
- The R5 fix brief requires the unknown `work_item_id` path to prove unchanged audit and persisted state.

Required fix follows.

- Capture a deep, persisted-state baseline before the unknown-id request, including store revision, projections, lifecycle/evidence/review facts, audit events, and idempotency map.
- Assert `QueryNotFound` and exact equality with that baseline after the request, then reload and compare again to prove the database state is unchanged.

## Accepted behavior

- Production catches `sqlite3.Error` in the protected outcome writer at `task4_consistency/controlled/s01.py:11503-11522`, uses the authentic work-item application reference, contains recovery reload failure, and emits no raw value.
- Metadata-only `evidence_eligible is True` gating precedes `_admitted_evidence`, `_assemble_evidence`, registered `read_object`, and source reads at `task4_consistency/controlled/s01.py:11722-11765`; false/missing qualification guards and zero-read instrumentation remain in `tests/test_s15_policy_owner.py:478-658`.
- Selected binding integrity reads the registered result object and only the requested observation object at `task4_consistency/controlled/s01.py:10176-10240`; the sibling-read assertion remains at `tests/test_s15_policy_owner.py:662-754`.
- Revision, tenant/resource, assignment, claim, context, watermark, C19, S09, S14, idempotency, raw filtering, and no-store behavior remain statically present in `task4_consistency/controlled/s01.py:11784-12093` and `task4_consistency/web/app.py:5273-5333`.
- Successful `app=None` v2 nullable audit persistence and reload are asserted at `tests/test_s15_policy_owner.py:1637-1705`; governed v2 and historical v1 projection remain at `:990-1074`.
- Expiry and per-link UI state are present at `frontend/src/components/ReviewWorkPanel.tsx:978-1060`, with rerender and mixed-link cases at `frontend/src/components/ReviewWorkPanel.test.tsx:2804-2915`.
- The generated bundle group is coherent in the worktree: `task4_consistency/web/static/react/index.html` references `assets/index-DwR5zzmb.js`, while `assets/index-ChVt9ebc.js` is deleted. Delivery must include the group together. No raw/direct-object/bulk/download/export/print/copy route was added.

## Residual risk and unexecuted verification

- The real transaction rollback path, HTTP runtime mapping, v1/v2 projection, browser rendering, and all acceptance assertions remain unverified because execution was prohibited.
- The new bundle is untracked and can be omitted by tracked-only delivery selection.
- The worktree contains unrelated untracked files. Ticket delivery should select only the S15 production/tests, the grouped frontend assets, and the R6 review evidence.
- `/tmp/codex-ticket31-plan.md` is unavailable, so comparison uses the issue, retained R5 review/fix brief, and repository specifications.
