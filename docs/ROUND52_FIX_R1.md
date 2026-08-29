# Ticket #52 / S16 Repair R1

## Fixed point and scope

- Fixed point: `c2ab8c0` (`docs(t18): record ticket #52 delivery and review`).
- Repair commit: this commit, referencing #52 and #32.
- Changed paths: `task4_consistency/controlled/s16.py` and this repair record.
- `GOAL.md`, `STATUS.md`, all `ROUND32_*` records, frontend files, generated
  assets, and the existing regression tests remain unchanged.

## Defects addressed

1. Runtime stale-fence checks previously migrated a missing operation-fence
   row inside the command transaction. Rejected wrong-fence quarantine,
   wrong-operation quarantine, and second-identity residue therefore inserted
   a `backup_operation_fences` row despite their zero-change contracts. The
   runtime path now derives a temporary fence view; startup remains the
   durable migration boundary.
2. Legacy rows with a missing `source_fence` now retain their scope/status
   history for the delete marker and residue checks. An externally unlinked
   manifest with a pre-marker intent reaches the owner `S16_VERIFY_FAILED`
   envelope, while malformed scope/history still records a visible migration
   failure and returns stale.
3. Repair-forward completion preserves the operation's original
   `source_fence` while advancing the active takeover fence. The completed
   `binding` plus `committed` intent pair is accepted as the proof source for
   a later fence, allowing the before-return shared-source tamper recovery to
   complete once the captured bytes are restored.

## Verification

Exact regressions from the #52 delivery record:

```text
.venv/bin/pytest -q tests/test_s16_controlled.py::test_backup_resume_rejects_wrong_fence_quarantine tests/test_s16_controlled.py::test_backup_resume_rejects_wrong_operation_quarantine tests/test_s16_controlled.py::test_backup_resume_rejects_old_schema_missing_marker tests/test_s16_controlled.py::test_backup_resume_rejects_second_identity_registry_residue tests/test_s16_controlled.py::test_backup_worker_before_return_shared_tamper_invalidates_binding
5 passed
```

Affected backup/fence/recovery subset:

```text
.venv/bin/pytest -q tests/test_s16_controlled.py -k 'backup_resume or operation_fence or migration or stale_fence or repair_forward or before_return'
22 passed, 87 deselected
```

Malformed-history and lazy-migration regressions are included in that subset,
including `test_backup_resume_rejects_corrupt_identities_json` and
`test_backup_lazy_damaged_history_without_fence_fails_closed`.

Full S16 consumers:

```text
.venv/bin/pytest -q tests/test_s16_controlled.py
109 passed

.venv/bin/pytest -q tests/test_s16_http.py
11 passed
```

`python -m py_compile task4_consistency/controlled/s16.py` and `git diff
--check` also pass.

## Two-axis review

**Standards: PASS.** The repair keeps S16 as the sole owner of operation
fences, preserves short transactions, leaves rejected commands observational,
and records malformed history through the existing fail-closed migration
failure table. Repair-forward retains the original source proof and uses the
active fence for takeover CAS.

**Spec: PASS for the repaired S16 contracts.** All five #52 blockers now pass,
including zero bookkeeping change on cross-pass quarantine/residue rejection,
old-schema marker fail-closed behavior, stale damaged-history handling, and
before-return tamper recovery. The T18 frontend remains unchanged and consumes
the same typed S16 authority.

## Unverified items

Full repository pytest, full Playwright, `scripts/ci_gate.sh`, evaluate, attack
probes, build, generated API checks, deployment packaging, and live institution
identity-provider rollback remain unverified in this repair lane.
