# Ticket 31/S15 Read-only Review R4

Date 2026-08-28

## Fixed point and scope

- The fixed point recorded by `docs/ROUND31_REVIEW_R3.md` is `2b4092195ffa643b17b3c17b62f6fe1971d144d4`, equal to the current `HEAD`.
- The reviewed delivery is the current working tree difference from that fixed point, including untracked delivery files reported by `git status --short`.
- The `code-review` skill was applied on separate Standards and Spec axes.
- Standards sources were `AGENTS.md`, `CONTEXT.md`, `ARCHITECTURE.md`, ADR-0004, ADR-0008, and the skill's Fowler smell baseline.
- Spec sources were GitHub issue #31, `docs/ROUND31_REVIEW_R3.md`, and `docs/ROUND31_FIX_BRIEF_R3.md`.
- Review evidence came from static source, call-path, diff, and generated-file inspection. Runtime tests, builds, evaluate, attack scripts, project scripts, and gates remained unexecuted as required.

## Verdict

**FAIL**

## Standards

**PASS with three non-blocking judgement findings.**

No documented-standard breach was found. `GOAL.md` and `STATUS.md` have no worktree diff, and the R3 production edits remain within `task4_consistency/controlled/s01.py`.

### Judgement findings

- Possible Duplicated Code appears in the scope and assignment filter at `task4_consistency/controlled/s01.py:9893-9908` and `task4_consistency/controlled/s01.py:11606-11622`. Both blocks define visible scopes and select the same manual-review Lifecycle work item. The duplication can drift because the first block is full authority and the second is an audit precondition. A single narrow helper returning the scope-checked candidate would keep the two uses aligned; this remains non-blocking because R3 required a pre-authority reference and the current predicates match.
- Possible Feature Envy appears at `tests/test_s15_policy_owner.py:1024-1056`, `tests/test_s15_policy_owner.py:1082-1185`, and `tests/test_s15_policy_owner.py:1391-1402`. The tests directly construct audit rows and replace store/service internals. Focused fixture helpers would reduce coupling; this remains a judgement call.
- Possible Speculative Generality appears at `task4_consistency/controlled/s01.py:566`. `_REVEAL_AUDIT_SCHEMA_V1` has no production or test caller; compatibility behavior comes from the generic timeline reader and a hard-coded test string. Remove the unused constant or use it in a real version discriminator when one exists.

## Spec

**FAIL with one P1 incomplete R3 correction and two P2 evidence gaps.**

### P1 — real SQLite persistence failures escape the protected outcome

Evidence

- `docs/ROUND31_FIX_BRIEF_R3.md:11` requires separate `app=None` cases for audit-write failure, persistence failure, and recovery-reload failure, with HTTP status, `no-store`, raw absence, and atomicity evidence.
- The protected writer at `task4_consistency/controlled/s01.py:11502` catches `StaleStoreRevision`, `_StoreWriteFailure`, `OSError`, `RuntimeError`, and `ValueError`. It does not catch `sqlite3.Error`.
- `SQLiteTargetStore.persist()` at `task4_consistency/controlled/s01_store.py:619-775` executes SQLite statements and re-raises transaction exceptions after rollback. Reachable database lock, I/O, and database failures therefore surface as `sqlite3.OperationalError` or another `sqlite3.DatabaseError`.
- Such a real persistence exception bypasses `task4_consistency/controlled/s01.py:11511-11523`, so the command loses its stable `unavailable/STORAGE_UNAVAILABLE` result and reaches the generic HTTP 500 path.
- The case labelled persistence failure at `tests/test_s15_policy_owner.py:1120-1148` replaces `_before_write` and raises `_StoreWriteFailure` at `reveal.idempotency` on lines 1128-1133.
- Production calls `_before_write("reveal.idempotency")` at `task4_consistency/controlled/s01.py:11498-11500`, before `staged.persist()` at line 11501. The test therefore exits before `SQLiteTargetStore.persist()` and cannot prove its transaction rollback or the recovery behavior following an actual persistence exception.
- The HTTP case at `tests/test_s15_policy_owner.py:1331-1429` covers only an audit-write outage. The persistence and recovery-reload cases remain service-level and do not assert the required 503 plus `Cache-Control: no-store` adapter contract.

Acceptance mapping

- Issue #31 requires an atomic audit fact, zero partial success on failure, audit-outage handling, and cache-safe failure responses.
- R3 review finding 2 and R3 fix brief requirement 2 explicitly made all three `app=None` fault classes and their HTTP mapping completion evidence.

Required fix

- Convert SQLite adapter exceptions to one domain storage exception in `SQLiteTargetStore.persist()`, or catch `sqlite3.Error` at the reveal outcome boundary. Preserve the rollback and contained recovery reload, then return `unavailable/STORAGE_UNAVAILABLE`.
- Patch `SQLiteTargetStore.persist` on the staged store class, or an equivalent transaction boundary reached by `staged.persist()`, so one case raises `sqlite3.OperationalError` from the actual persistence call after audit and idempotency are staged.
- Preserve an unmodified persisted baseline, then assert recovery reload restores that baseline with zero new audit row and zero idempotency binding.
- Exercise the persistence-failure and recovery-reload variants through the existing HTTP adapter and assert 503, the exact stable reason, `Cache-Control: no-store`, and absence of raw value or locator.

### P2 — v2 compatibility evidence omits the nullable revision shape

Evidence

- Production now emits `s15-reveal-audit/2` at `task4_consistency/controlled/s01.py:565` and supports nullable revisions plus optional governed vocabulary at `task4_consistency/controlled/s01.py:11435-11447` and `task4_consistency/controlled/s01.py:11472-11496`.
- `tests/test_s15_policy_owner.py:991-1072` proves a governed v2 event with integer revisions and a historical v1 event with the original required fields.
- Every `app=None` test at `tests/test_s15_policy_owner.py:1075-1185` forces outcome persistence to fail. No persisted v2 event is asserted with `lifecycle_revision=None`, `evidence_revision=None`, and omitted pre-C19 vocabulary.

Acceptance mapping

- `docs/ROUND31_FIX_BRIEF_R3.md:9` requires focused compatibility assertions for the chosen v2 contract and historical v1 readability.

Required fix

- Add a successful safe attempted-action audit with `app=None`, then reload and assert the exact v2 nullable and optional-field shape.
- Keep the existing historical v1 reload assertion and the governed v2 success assertion.

### P2 — unidentifiable work-item existence hiding lacks direct regression evidence

Evidence

- The minimal work reference rejects unmatched candidates at `task4_consistency/controlled/s01.py:11609-11622`, which statically hides unknown work-item identifiers.
- `tests/test_s15_policy_owner.py:1282-1328` covers an unassigned subject and a cross-tenant subject. It does not submit an unknown `work_item_id` and prove `QueryNotFound` with zero attempted-action audit.

Acceptance mapping

- Issue #31 names cross-tenant and existence probing as required adversarial evidence.
- `docs/ROUND31_FIX_BRIEF_R3.md:13` explicitly preserves hiding for unauthorized, cross-tenant, and unidentifiable resources.

Required fix

- Add an unknown work-item identifier to the existing existence-hiding test and assert `QueryNotFound` plus an unchanged reveal-audit event set.

## Accepted R3 corrections

- Schema versioning is corrected in production at `task4_consistency/controlled/s01.py:565-566`. Generic timeline projection retains historical v1 fields at `task4_consistency/controlled/s01.py:18805-18927`, and the v1 reload test is at `tests/test_s15_policy_owner.py:1021-1072`.
- The `app=None` exception response uses the stable work-item application reference and contains recovery reload failure at `task4_consistency/controlled/s01.py:11502-11523` for the currently caught exception classes. The uncaught SQLite class remains blocking.
- A minimally scope-checked work reference is established at `task4_consistency/controlled/s01.py:11601-11623`; full authority damage routes to one common stopped outcome at `task4_consistency/controlled/s01.py:11698-11711`. Visible damage, replay, unauthorized, and cross-tenant cases appear at `tests/test_s15_policy_owner.py:1216-1328`.
- The admitted-evidence damage test now uses governed defaults at `tests/test_s15_policy_owner.py:758-831`. `_require_application_state_authority` calls `_admitted_evidence` during current-context reconstruction, so the injected failure is reachable before C19 and maps to the asserted stopped result.

## Preserved R2 behavior

- Metadata-only link eligibility remains before `_admitted_evidence`, `_assemble_evidence`, and registered source reads at `task4_consistency/controlled/s01.py:11714-11757`. False and missing eligibility guards remain at `tests/test_s15_policy_owner.py:525-658`.
- Targeted integrity reads only the result object and selected observation object at `task4_consistency/controlled/s01.py:10210-10236`; the distinct sibling and exact call list remain at `tests/test_s15_policy_owner.py:662-727`.
- Governed vocabulary enters audit only after C19 at `task4_consistency/controlled/s01.py:11808-11826`. Audit and replay filtering continue to omit `source_text` at `task4_consistency/controlled/s01.py:11429-11501`.
- Revision, context, claim, expiry, C19, storage, S14, S09, and selected-binding checks remain before raw return at `task4_consistency/controlled/s01.py:11784-12039`.
- UI expiry checks and per-link eligibility remain at `frontend/src/components/ReviewWorkPanel.tsx:978-1060`, with static test coverage at `frontend/src/components/ReviewWorkPanel.test.tsx:2804-2915`.
- The frontend generated group remains coherent in the worktree. `index.html` references untracked `assets/index-DwR5zzmb.js`, and tracked `assets/index-ChVt9ebc.js` is deleted. The three files must be delivered together.
- `GOAL.md`, `STATUS.md`, and OpenAPI generated files have no diff. No raw, direct-object, bulk reveal, download, export, print, or copy API was added.
- The adjacent `tests/test_s04_controlled.py` changes only align complete-object assertions with existing `evidence_ready` and correction `cycle` fields.

## Residual risk and unexecuted verification

- Runtime collection, SQLite transaction behavior, HTTP mapping, TypeScript rendering, bundle equivalence, and all test assertions remain unverified under the review prohibition.
- `task4_consistency/web/static/react/assets/index-DwR5zzmb.js` remains untracked and can be omitted by a tracked-only delivery operation.
- The worktree contains unrelated untracked files. Ticket delivery selection must remain limited to the listed S15 files and review evidence.

Standards has zero hard findings and three judgement findings; Spec has one P1 and two P2 findings, with uncaught SQLite persistence failures as the worst Spec issue.
