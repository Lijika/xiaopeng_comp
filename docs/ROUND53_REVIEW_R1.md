# Ticket #53 / T19 Two-Axis Review R1

## Review scope

- Fixed point: `c2ab8c0` (the #52 delivery commit).
- Delivery diff: `git diff c2ab8c0...HEAD`.
- Specification sources: GitHub issue #53, issue #52 and its R1 review,
  issue #33's S17 contract, `docs/ROUND33_PLAN.md`, `CONTEXT.md`,
  `ARCHITECTURE.md`, ADR-0003, ADR-0004, ADR-0008, and
  `docs/ROUND53_DELIVERY_R1.md`.
- The reviewed S17 implementation is the existing
  `frontend/src/components/S17ExportPanel.tsx`, `frontend/src/api/hooks.ts`,
  generated OpenAPI group, S17 HTTP adapter, and controlled S17 service. The
  non-empty T19 diff contains only the two delivery records.

## Standards

**PASS for the current delivery diff; implementation completeness remains a
Spec issue**

The existing hooks use the thin same-origin adapter, typed generated DTOs,
`retry: false` for mutations, and query invalidation. FastAPI remains the sole
owner of export request binding, approval, package generation, delivery,
cleanup, and audit. The route is additive and the generated drift/build gates
pass. No documented standards breach or blocking Fowler smell was introduced
by the T19 delivery records.

## Spec

**BLOCKED**

The backend S17 release is available and its 21 controlled/HTTP tests pass, but
the current React surface covers only preview, approval, commit, query, and a
receipt read. Issue #53 requires an operator-defined fixed request,
independently authenticated approval, one-time or short-lived delivery, and
visible denial, expiry, reuse, generation-failure, watermark/encryption, and
partial-cleanup states. Those interfaces and their lower/browser tests are
absent, and no T19 Playwright tracer exists.

#52 is a declared blocker for #53. Its R1 review reports five failing S16
authority tests. They cover zero-change quarantine rejection, old-schema marker
fail-closed behavior, identity-residue rejection, and repaired before-return
tamper completion. These failures are reachable through the shared deployment
authority and require S16 repair before T19 can receive a PASS verdict.

## Findings

1. **Blocking, T19 frontend** — `S17ExportPanel.submitPreview` hard-codes the
   purpose, minimum fields/artifacts, recipient, classification, and expiry,
   so React cannot create the user-defined immutable request required by #53.
2. **Blocking, T19 frontend** — `useS17Approve` sends the normal same-origin
   credential and the panel has no independent approver credential seam; the
   required separation of duties is unreachable in React.
3. **Blocking, T19 frontend** — access/confirm/expire and delivery status hooks
   and UI are missing, along with denial, expiry, reuse, generation-failure,
   watermark/encryption, cleanup, redaction, and responsive/browser evidence.
4. **Blocking, dependency #52** — the five S16 backend failures recorded in
   `docs/ROUND52_REVIEW_R1.md` prevent the required affected-consumer gate.

## Verdict

**BLOCKED**

The S17 backend and build seams are healthy. T19 needs #52 authority repair,
the missing React delivery seams, and a dedicated desktop/mobile Playwright
tracer before ticket closure.
