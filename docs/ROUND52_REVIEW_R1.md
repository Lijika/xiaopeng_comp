# Ticket #52 / T18 Two-Axis Review R1

## Review scope

- Fixed point: `7115e1a` (the #51 delivery commit).
- Delivery diff: `git diff 7115e1a...HEAD`.
- Implementation evidence: the S16 React panel and typed hooks introduced by
  `17c49ef`, the S16 HTTP and ledger fixes through the S16 R20 code state, and
  the production browser tracer in `tests/test_t17_react.spec.js`.
- Specification sources: GitHub issue #52, issue #32's S16 contract,
  `CONTEXT.md`, `ARCHITECTURE.md`, ADR-0003, ADR-0004, ADR-0008,
  `docs/ROUND52_DELIVERY_R1.md`, and the S16 delivery/review records.
- This review is static. Focused command results and the exact known failures
  are recorded in the delivery document.

## Standards

**PASS for the T18 frontend surface; backend regression remains open**

The panel is a thin server-state client. S16 owns identity, scope, retention,
legal hold, approval separation, owner fencing, worker attempts, repair
semantics, and value-free receipts. The hooks use typed closed DTOs, bounded
polling, `retry: false`, stable idempotency variables for unknown transport
outcomes, and query invalidation. The shell keeps the same-origin no-store
boundary and the browser tracer allowlists only S16 routes and static assets.

The focused React and Playwright evidence supports these frontend standards.
The five Python failures are backend contract regressions in S16 quarantine,
marker, residue, and fence-repair handling. They are outside the T18 frontend
change boundary and remain a verification blocker.

## Spec

**BLOCKED**

T18's required operator path is present and its focused frontend evidence
passes: 35 React unit tests and 2 registered Playwright scenarios pass across
desktop and mobile viewports. The affected-consumer Python command also
reports 118 passes and 5 failures. Those failures violate required S16
cross-replica invariants, including zero-change fail-closed rejection,
old-schema marker rejection, and completion after before-return tamper repair.

The panel cannot be declared ticket-complete while the authority it renders
has these reachable failures. Fixing them requires S16 backend paths and
regression tests, which are outside the T18 frontend scope. The correct
status is blocked until those fixes land and the affected command passes.

## Findings

1. **Blocking, S16 backend** — `test_backup_resume_rejects_wrong_fence_quarantine`,
   `test_backup_resume_rejects_wrong_operation_quarantine`, and
   `test_backup_resume_rejects_second_identity_registry_residue` observe a new
   `backup_operation_fences` row after a rejection that must leave bookkeeping
   unchanged.
2. **Blocking, S16 backend** —
   `test_backup_resume_rejects_old_schema_missing_marker` receives no
   `S16OwnerFailure` for a missing marker plus external unlink, so the
   fail-closed old-schema contract is unproven.
3. **Blocking, S16 backend** —
   `test_backup_worker_before_return_shared_tamper_invalidates_binding` cannot
   complete the repaired takeover and returns
   `repair_required / S16_OWNER_STALE_FENCE`.

These findings are recorded as known S16 defects rather than frontend review
findings. No additional T18 frontend standards breach was identified.

## Verdict

**BLOCKED**

The T18 frontend surface is reviewable and focused browser evidence passes.
Ticket readiness requires S16 backend repair, the affected-consumer pytest
rerun, and an updated review before a PASS verdict can be issued.
