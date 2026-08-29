# Ticket #32 / S16 R21 Static Code Review

## Scope and method

- Fixed baseline: `7a55192`.
- Reviewed commits: `d00db1a` (implementation) and `8e550ef` (R20 delivery).
- Reviewed range: `git diff 7a55192...HEAD` and `git log 7a55192..HEAD`.
- Sources: `docs/ROUND32_REVIEW_R20.md`, `docs/ROUND32_FIX_BRIEF_R20.md`, `docs/ROUND32_DELIVERY_R20.md`, `docs/ROUND32_PLAN.md`, ADR-0003, ADR-0008, `ARCHITECTURE.md`, and `AGENTS.md`.
- This review is static only. No tests, builds, generate, lint, typecheck, evaluate, attack probes, or other project commands were run for R21.

## Findings

No new blocking finding was identified. The four R20 findings map to the following verified changes.

### R20 P1-1, existing fence metadata validation

- Evidence: `task4_consistency/controlled/s16.py:2244-2262` adds `_validate_runtime_operation_fence()`, which reloads the operation history and delegates to `_validate_existing_operation_fence()`. `task4_consistency/controlled/s16.py:2311-2347` invokes it before stale/high-water/active decisions. `task4_consistency/controlled/s16.py:3938-3959` applies the same validation to every stored fence during `owner_healthy()`.
- The validator covers positive fence fields, source fence, scope, digest, known statuses, and identity/manifest JSON consistency through `_derive_operation_fence()` at `task4_consistency/controlled/s16.py:2113-2214`. Failures record `backup_fence_migration_failures` and prevent use of the fence.
- Targeted regression `test_backup_runtime_fence_metadata_mismatch_fails_closed` covers a live scope metadata alteration and zero effect-state change. The effect snapshot includes fences at `tests/test_s16_controlled.py:4578-4583`.

### R20 P1-2, NULL source-fence migration

- Evidence: `task4_consistency/controlled/s16.py:1986-1990` now parses source fences with the strict `_parse_fence_int()` at `:2018-2041`; NULL, booleans, malformed numeric text, zero, and negative values raise. `_derive_operation_fence()` at `:2136-2141` requires a non-null positive source fence in range.
- Invalid history is represented as an invalid sentinel and causes migration failure without inserting a derived operation fence. `test_backup_null_source_fence_fails_closed` covers the NULL case and the unchanged-fence-table assertion.

### R20 P1-3, restore replay fence contract

- Evidence: `task4_consistency/controlled/s16.py:6408-6428` uses the completed job's positive claimed fence for both replay delete and replay verification. The replay operation remains per job, owner, and scope through `_replay_operation_id()`.
- `BackupDeletionOwner.delete()` validates a positive fence before any transaction at `task4_consistency/controlled/s16.py:2857-2878`, and backup replay defaults to a schema-valid fence at `:3914-3927`. The restore regression asserts the persisted replay fence tuple `(high_water, active_fence, source_fence) == (1, 1, 1)` in `tests/test_s16_controlled.py:2154-2172`.

### R20 P2-1, regression and delivery evidence

- Evidence: `tests/test_s16_controlled.py:4568-4583` includes operation-fence rows in zero-change snapshots. New tests at `:7368-7448` cover NULL source fences and runtime metadata tampering; existing replay, restore, cross-scope, crash, migration, and repair-forward tests remain in the affected test module.
- `docs/ROUND32_DELIVERY_R20.md:19-28` records the four allowed targeted pytest selections and their results, and `:30-33` records the commands intentionally omitted.

## Standards axis

**PASS**.

The implementation follows the repository's fail-closed boundary, short-transaction, fencing, migration, and repair-forward rules. Existing fence use is guarded by a complete history check, invalid source values cannot be backfilled, and replay uses a positive persisted fence. The test changes strengthen zero-change evidence without adding a new abstraction or dependency. No additional documented-standard breach or blocking Fowler smell was found in the reviewed diff.

## Spec axis

**PASS**.

The implementation satisfies the R20 acceptance for complete operation-fence binding, strict NULL rejection, positive restore replay, owner health gating, cross-scope-safe repair-forward, and a single deletion effect. The code paths use the same operation/fence contract for delete, verify, worker health, and restore replay. Targeted tests and the delivery record cover the changed consumers named by `docs/ROUND32_PLAN.md` and ADR-0008.

## Final verdict

**PASS**

## Remaining risks and unverified items

- R21 did not execute tests or other project commands by instruction. The targeted results cited above are the R20 delivery record produced before this static review.
- Full repository pytest, full Playwright, `ci_gate.sh`, evaluate, attack probes, build, lint, typecheck, and generate remain unverified.
- Production PostgreSQL/object-storage connectors, external WORM/SIEM replication, HTTP deployment wiring, React runtime cache behavior, and institution-specific identity/retention/legal-hold integrations remain outside this static diff and were not validated here.
