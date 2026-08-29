# Ticket #51 / T17 Two-Axis Review R1

## Review scope

- Fixed point: `a1165a6` (the #50 delivery commit).
- Delivery diff: `git diff a1165a6...HEAD`.
- Specification: GitHub issue #51, canonical frontend migration issue #34,
  `CONTEXT.md`, `ARCHITECTURE.md`, ADR-0003, ADR-0004, ADR-0008, and
  `docs/ROUND51_DELIVERY_R1.md`.
- The reviewed implementation is the existing S15 React vertical slice in
  `frontend/src/components/ReviewWorkPanel.tsx`, `frontend/src/api/hooks.ts`,
  generated OpenAPI files, the S15 FastAPI adapter, and their focused tests.
  This ticket's non-empty delivery diff contains the two records named above;
  production behavior is traced to the released S15 commits.

## Standards

**PASS**

The React interface stays thin and server-state oriented. TanStack Query owns
requests, bounded retries, invalidation, and mutation cache; FastAPI remains
the sole authority for C19 eligibility, scope, duration, source reads,
idempotency, and audit. Restricted values enter state only from an authorized
response and are scrubbed on expiry, reload, access loss, context/fence change,
release, and supersession. The shell is additive, same-origin, and no-store;
generated types and the static bundle are checked by the existing drift gate.

No documented standards breach was found. No blocking Fowler smell appears in
the reviewed S15 surface. The existing `ReviewWorkPanel` remains a large
workflow module, which is an accepted migration seam with focused tests and a
single owning route; splitting it would add an unrequested second interface.

## Spec

**PASS**

Issue #51 requires a purpose-bound minimum reveal after S15 release. The panel
reads the server `reveal_eligibility` projection, sends the closed generated
command with the exact fence/context/region, and renders only the authorized
observation value. Expiry, stale authority, denied role/scope, existence
hiding, audit minimization, no-store headers, key-preserving unknown outcomes,
and cache/URL/storage/error/telemetry cleanup are covered by the S15 Python,
Testing Library, and registered Playwright evidence recorded in the delivery
document. Print, copy, export, direct-object, and bulk surfaces remain absent;
the reveal action carries no such capability.

The migration keeps the legacy S01 route as rollback fallback and introduces no
client lifecycle or authorization authority. No scope-creep behavior was found
in the T17 surface. Full repository gates and live deployment rollback remain
explicitly unverified in this ticket lane.

## Verdict

**PASS**

T17 is ready for its dedicated delivery commit. The backend S15 release and
the additive React route remain prerequisites for deployment cutover.
