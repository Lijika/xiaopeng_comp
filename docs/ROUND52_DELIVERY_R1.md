# Ticket #52 / T18 Delivery R1

## Scope

T18 records the React migration of the S16 cross-replica governed-deletion
workflow. The production surface is the existing
`S16GovernedDeletionPanel`, its typed S16 HTTP hooks, and the registered
Playwright tracer in `tests/test_t17_react.spec.js`. The implementation paths
were released in the S16 (#32) commit chain from `17c49ef` through the final
S16 fence/replay fixes; this ticket adds the T18 delivery evidence only.

The fixed point for this record is `7115e1a` (the #51 delivery commit). The
T18 delivery diff contains this record and its review record; no S16 business
code, generated asset, GOAL file, STATUS file, or ROUND32 record changes here.

## Acceptance evidence

- The `/controlled/s16` shell and `/controlled/s16/react` alias require the
  registered governance identity, use the controlled no-store policy, and fail
  closed when the production React build is missing.
- The panel submits one application reference and renders the server-owned
  nine-class dry-run manifest with counts and digest prefixes. Each command
  carries a closed request DTO and its own idempotency key; unknown transport
  outcomes retain the key for exact replay.
- Early deletion requires two distinct registered approvers before the
  explicit commit. The browser supplies the approver token for that action and
  keeps authorization, scope, retention, legal hold, and owner readiness in
  the S16 authority.
- The durable job surface shows bounded worker attempts, typed stable owner
  failures, repair-required state, repair-forward of the same job, and a
  value-free receipt. Completed deletion clears application-scoped query
  state and hides the deleted application from the controlled read planes.
- The browser path exercises desktop `1280x800` and mobile `390x844` layouts,
  keyboard operation, hard refresh, post-delete existence hiding, and an
  allowlisted S16 request plane. Raw object values, locators, and credentials
  stay outside the rendered receipt and request log.

## Focused verification

Commands were run from the repository root at `7115e1a`.

```text
.venv/bin/pytest -q tests/test_s16_controlled.py tests/test_s16_http.py tests/test_t17_react_app.py
118 passed, 5 failed
```

The five failures are reproducible S16 backend regressions in the same command
and remain part of the ticket evidence.

| Test | Observed failure | Contract affected |
| --- | --- | --- |
| `test_backup_resume_rejects_wrong_fence_quarantine` | `backup_operation_fences` gains `op-fence`, fence 1 during a rejected wrong-fence quarantine. | Fail-closed rejection must preserve zero bookkeeping/effect changes. |
| `test_backup_resume_rejects_wrong_operation_quarantine` | `backup_operation_fences` gains `op-self`, fence 1 during a rejected cross-operation quarantine. | Fail-closed rejection must preserve zero bookkeeping/effect changes. |
| `test_backup_resume_rejects_old_schema_missing_marker` | `backup.delete(..., operation_id="op-old", fence=1)` returns without raising `S16OwnerFailure`. | A missing unlink marker with an externally unlinked manifest must fail closed. |
| `test_backup_resume_rejects_second_identity_registry_residue` | `backup_operation_fences` gains `op-multi`, fence 1 while second-identity residue is rejected. | Residue rejection must preserve zero bookkeeping/effect changes. |
| `test_backup_worker_before_return_shared_tamper_invalidates_binding` | After source restoration, the takeover attempt returns `repair_required / S16_OWNER_STALE_FENCE`; the test expects `complete`. | The before-return tamper recovery path must complete one effect after repair. |

```text
npm run test:unit -- src/components/S16GovernedDeletionPanel.test.tsx src/api/hooks.s16.test.tsx
35 passed
```

The React tests cover the nine-class manifest, distinct approvals, explicit
commit, repair and receipt rendering, unknown idempotency outcomes, cache
clearing, legal holds, and typed error states.

```text
npx playwright test tests/test_t17_react.spec.js --workers=1
2 passed (desktop 1280x800 and mobile 390x844)
```

The browser tracer covers the registered S16 workflow through the real
FastAPI shell and records no request-plane violation or restricted-value
leakage in either viewport.

## Blocking regression and scope boundary

The five S16 failures block ticket-level Spec and verification readiness.
They exercise cross-replica quarantine, identity residue, old-schema marker
handling, and before-return repair semantics that the T18 panel presents to an
operator. The failures belong to `task4_consistency/controlled/s16.py` and its
S16 regression tests; T18 changes only the frontend delivery evidence and has
no authorized frontend fix for these backend contracts. The ticket therefore
remains blocked pending the S16 repair and a passing rerun of the affected
consumer tests.

## Unverified evidence

- Full pytest, the complete Playwright suite, `scripts/ci_gate.sh`, evaluate,
  attack probes, and installed-release verification were not run in this
  ticket lane.
- `npm run build`, `npm run generate:api`, generated-drift checks, typecheck,
  deployment packaging, and live institution identity-provider rollback were
  not run for this delivery record.
- The S16 backend failure list above is the authoritative known defect record;
  the 35 React and 2 Playwright passes do not supersede it.
