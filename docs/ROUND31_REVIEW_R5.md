# Ticket 31/S15 Read-only Review R5

Date 2026-08-28

## Fixed point and scope

- `docs/ROUND31_REVIEW_R4.md` and `docs/ROUND31_FIX_BRIEF_R4.md` record fixed point `2b4092195ffa643b17b3c17b62f6fe1971d144d4`, equal to the current `HEAD`.
- The review covers the current working tree difference from that point, plus untracked delivery files shown by `git status --short`.
- The `code-review` skill was applied on independent Standards and Spec axes.
- Standards sources were `AGENTS.md`, `CONTEXT.md`, `ARCHITECTURE.md`, ADR-0004, ADR-0008, and the skill's Fowler smell baseline.
- Spec sources were GitHub issue #31, `docs/ROUND31_REVIEW_R4.md`, and `docs/ROUND31_FIX_BRIEF_R4.md`.
- Evidence is static source, call-path, diff, and generated-file inspection. Tests, builds, evaluate, attack scripts, project scripts, and gates were not executed.

## Verdict

**FAIL**

## Standards

**PASS with non-blocking judgement findings.**

No documented standards breach was found. `GOAL.md` and `STATUS.md` have no worktree diff, and production changes stay within the S01 authority boundary.

### Judgement findings

- Possible Duplicated Code at `task4_consistency/controlled/s01.py:9893-9908` and `:11606-11622`. Both blocks build visible scopes and select the assigned Lifecycle-owned manual-review item. A narrow shared selector would reduce drift; the duplication is non-blocking because the predicates currently match the required pre-authority and full-authority stages.
- Possible Feature Envy at `tests/test_s15_policy_owner.py:1024-1056`, `:1082-1185`, and `:1391-1402`. Tests construct audit rows and mutate service/store internals directly. This is a maintainability judgement, with no current acceptance failure.
- Possible Speculative Generality at `task4_consistency/controlled/s01.py:566`. `_REVEAL_AUDIT_SCHEMA_V1` has no production or test caller; the timeline reader handles historical events generically. Remove the unused constant or make it the source of a real discriminator.

## Spec

**FAIL with one P1 production defect and two P2 evidence gaps.**

### P1 — real SQLite persistence errors escape the reveal outcome contract

Evidence

- `task4_consistency/controlled/s01.py:11502-11523` catches `StaleStoreRevision`, `_StoreWriteFailure`, `OSError`, `RuntimeError`, and `ValueError`. `sqlite3.Error` is absent.
- `task4_consistency/controlled/s01_store.py:619-775` executes SQLite statements inside a transaction, rolls back, and re-raises the original exception. A reachable lock, I/O, or database failure can be `sqlite3.OperationalError` or another `sqlite3.DatabaseError`.
- The resulting exception bypasses the recovery branch at `s01.py:11511-11523`; the command can reach the generic HTTP 500 adapter path instead of returning `unavailable/STORAGE_UNAVAILABLE` with no value.
- `tests/test_s15_policy_owner.py:1120-1148` labels its case persistence failure but raises `_StoreWriteFailure` from `_before_write("reveal.idempotency")` at `:1128-1133`, before `staged.persist()` at `s01.py:11501`. The recovery case at `:1150-1185` fails the first `_before_write` call as well.
- `tests/test_s15_policy_owner.py:1331-1429` covers only an audit-write outage over HTTP. It does not exercise a real persistence exception or the recovery-reload variant through the adapter.

Acceptance mapping

- Issue #31 requires audit/storage failure to fail closed, preserve atomic audit/idempotency behavior, return no raw value, and prevent cache/history recovery.
- `docs/ROUND31_FIX_BRIEF_R4.md:9-15` requires real persistence and recovery fault evidence, including 503, `no-store`, raw/path absence, rollback, and zero partial audit/idempotency.

Required fix

- Convert SQLite adapter exceptions to the existing domain storage exception in `SQLiteTargetStore.persist()`, or catch `sqlite3.Error` at `_record_reveal_outcome`, while preserving transaction rollback and contained recovery reload.
- Inject `sqlite3.OperationalError` from the actual staged `persist()` call after audit and idempotency are staged. Assert that reload restores the pre-attempt persisted state with no audit row and no idempotency binding.
- Exercise actual persistence failure and recovery-reload failure through HTTP. Assert 503, exact `STORAGE_UNAVAILABLE`, `Cache-Control: no-store`, and absence of raw value, source locator, credential, and internal path.

### P2 — v2 nullable `app=None` audit shape has no successful persistence evidence

Evidence

- The v2 writer permits nullable revisions and optional vocabulary at `task4_consistency/controlled/s01.py:565`, `:11435-11447`, and `:11472-11496`.
- `tests/test_s15_policy_owner.py:991-1072` verifies a governed v2 success with integer revisions and a manually inserted historical v1 event with the original required fields.
- Every missing-application case at `tests/test_s15_policy_owner.py:1075-1185` deliberately causes audit or write failure. No case persists, reloads, and projects a v2 event with both revisions `None` and omitted `purpose`, `verification_reason`, and `classification`.

Acceptance mapping

- `docs/ROUND31_FIX_BRIEF_R4.md:16` requires v2 nullable-shape evidence alongside historical v1 readability.

Required fix

- Add a successful visible-work-item `app=None` outcome where audit persistence succeeds, reload it, and assert schema `/2`, both nullable revisions, omitted governed vocabulary, no raw fields, and a stable replay binding. Retain the current governed v2 and historical v1 assertions.

### P2 — unknown work-item existence hiding lacks a direct regression

Evidence

- The minimal reference guard at `task4_consistency/controlled/s01.py:11609-11622` raises `QueryNotFound` when no unique scope- and assignment-matching candidate exists, including an unknown identifier.
- `tests/test_s15_policy_owner.py:1282-1328` covers an unassigned subject and a cross-tenant scope. It does not call the command with an unknown `work_item_id` and compare the audit ledger before and after.

Acceptance mapping

- Issue #31 explicitly includes existence probing in the adversarial matrix.
- `docs/ROUND31_FIX_BRIEF_R4.md:17` requires an unidentifiable work-item regression with unchanged audit events.

Required fix

- Add an unknown `work_item_id` case, assert `QueryNotFound`, and assert the reveal-audit event set and persisted state remain unchanged.

## Accepted R4 and earlier behavior

- The four R3 repair targets are present statically. Metadata `evidence_eligible is True` runs at `task4_consistency/controlled/s01.py:11714-11757` before evidence assembly or registered source reads; false/missing tests install raising guards at `tests/test_s15_policy_owner.py:525-658`.
- Targeted source integrity reads the result object and selected observation object at `task4_consistency/controlled/s01.py:10171-10235`; the sibling fixture and exact ordered call list remain at `tests/test_s15_policy_owner.py:662-727`.
- Governed vocabulary is assigned only after C19 at `task4_consistency/controlled/s01.py:11808-11826`; `source_text` is filtered from replay and audit persistence at `:11429-11501`.
- Schema v2 is declared at `task4_consistency/controlled/s01.py:565`, and historical v1 events remain projected by the generic context-key reader at `:18805-18927`.
- `app=None` uses `work_item["application_id"]` and contains recovery reload failure at `task4_consistency/controlled/s01.py:11502-11523` for the caught exception classes.
- A minimally scope-checked work reference precedes full authority reconstruction at `task4_consistency/controlled/s01.py:11601-11623`; visible authority damage uses the common outcome at `:11698-11711`, while unauthorized and cross-tenant paths retain `QueryNotFound`.
- `ReviewWorkPanel` directly checks expiry at `frontend/src/components/ReviewWorkPanel.tsx:978-982` and per-link eligibility for projection and button state at `:1025-1060`; expiry rerender and mixed-link tests are at `frontend/src/components/ReviewWorkPanel.test.tsx:2804-2915`.
- The generated bundle group is coherent in the worktree: updated `task4_consistency/web/static/react/index.html`, deleted `assets/index-ChVt9ebc.js`, and untracked `assets/index-DwR5zzmb.js`. The three files require grouped delivery.
- `GOAL.md`, `STATUS.md`, and OpenAPI generated files have no diff. No raw, direct-object, bulk reveal, download, export, print, or copy route was added.

## Residual risk and unexecuted verification

- Runtime exception classes, SQLite rollback, HTTP status/header mapping, v1/v2 projection, test reachability, TypeScript rendering, bundle equivalence, and all acceptance assertions remain unverified because execution was prohibited.
- The new frontend bundle remains untracked and can be omitted by a tracked-only delivery operation.
- The worktree has unrelated untracked files. Ticket delivery selection must include only the S15 production/tests, grouped frontend assets, and review evidence.
- `/tmp/codex-ticket31-plan.md` is absent, so plan comparison relies on the retained R4 review and fix brief plus the issue and repository specifications.

Standards has zero hard findings and three judgement findings. Spec has one P1 and two P2 findings; the uncaught SQLite persistence exception is the blocking defect.
