# Ticket #50 / T16 Delivery R1

## Scope

T16 adds the React lifecycle cancellation workbench and operator settlement
console on the released S14 authority. The implementation was delivered in
the existing T16 commit chain from `2c11f46` through `94ac4eb`, including the
cycle-history contract assertion in `453b230` and the scoped history route in
`31d62fb`. This delivery record is the ticket-level evidence added after the
S17 prerequisite reached `37d01b5`.

## Acceptance evidence

- Authorized integrator cancellation posts the current lifecycle revision and
  displays the typed server result. S14 fences active work and reports
  `Terminating`; duplicate, stale, unauthorized, ineligible, and unavailable
  outcomes remain server-owned envelopes.
- The integrator panel refetches current-route and history with a bounded
  attempt budget. `Terminating` remains visible until FastAPI reports
  `Terminated`; a budget exhaustion is rendered as an explicit unknown.
- Reopen is a separate operator command. The panel requires the authoritative
  permission and artifact digest returned by S14, then displays the new cycle
  created by FastAPI. Reload and navigation only read state.
- Historical cycle views render cycle-bound runs, route facts, lifecycle
  events, late-input receipts, evidence corrections, and work destinations.
  They expose no lifecycle command controls and preserve application/cycle
  identity in navigation.
- The settlement console reads the S13 delivery projection plus the S14
  settlement view, requires matching application/cycle/phase/revision facts,
  and gates settle, notification, and reopen controls on those reads.
- Shell routes use the existing controlled identities, authenticate before
  availability disclosure, and apply `Cache-Control: no-store` and
  `Pragma: no-cache` to success and error responses. Missing builds fail closed.

## Focused verification

Commands were run from the repository root.

```text
.venv/bin/pytest -q tests/test_t16_react_app.py
15 passed in 12.84s

npm run test:unit -- src/components/T16LifecyclePanel.test.tsx src/api/hooks.s14.test.tsx
41 passed (2 test files)

npx playwright test tests/test_t16_react.spec.js --workers=1
2 passed (desktop 1280x800 and mobile 390x844)
```

The Python tests cover the real FastAPI fixture, shell identity/error
contracts, S14 command fencing and idempotency, settlement-view bindings,
late-input cycle ownership, and historical projections. Testing Library tests
cover command transitions, bounded reconciliation, error/reload behavior,
explicit reopen, cycle selection, and transport-unknown handling. The
Playwright tracer exercises two browser contexts through cancellation,
Terminating-to-Terminated reconciliation, settlement, notification, explicit
reopen, reload, back/forward navigation, old-cycle destinations, and mobile
overflow/accessibility checks.

## Scope controls and unverified evidence

- Changed paths in this delivery are this document and the review record. The
  T16 implementation paths remain the previously reviewed commits listed
  above. `GOAL.md`, `STATUS.md`, `docs/ROUND32_*`, S17 files, and generated
  frontend assets remain unchanged by this ticket delivery.
- `npm run build`, `npm run generate:api`, installed-release verification,
  production FastAPI connectors, deployment rollback, and rollback behavior
  against a live institution identity provider remain unverified here.
- Full pytest, full Playwright, `ci_gate.sh`, evaluate, and attack probes were
  not run, per the ticket lane constraints.
