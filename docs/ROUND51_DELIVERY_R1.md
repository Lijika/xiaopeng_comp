# Ticket #51 / T17 Delivery R1

## Scope

T17 delivers the S15 controlled minimum-reveal workflow in the existing React
Reviewer workbench. The owning backend contract and audit authority were
released by S15; this ticket records the React delivery and its production
artifact gates. The implementation is intentionally limited to the existing
`ReviewWorkPanel`, S15 hooks, generated OpenAPI contract, and their focused
tests. No second reveal authority or client-side business state was added.

## Acceptance evidence

- The reveal action is available only from a claimed Reviewer work item whose
  server projection marks the observation eligible. The command copies the
  server-owned application, fence, command context, purpose, reason,
  classification, observation, and public source region. The browser cannot
  choose a purpose or broaden the source region.
- The response renders the minimum `source_text` for the requested observation
  only. The UI keeps all other observations masked and never adds direct-object,
  bulk, download, export, print, or copy controls.
- Effective expiry comes from the authorized response. Expiry, release,
  authoritative reload, access loss, fence/context change, and a superseding
  command scrub the reveal, correction draft, pending mutation, and query
  mutation cache. A late response after expiry or unmount is discarded before
  storage.
- S15 FastAPI owns C19 authorization, scope/existence hiding, source reads,
  idempotency, and value-free audit facts. HTTP success and sanitized errors
  use the controlled no-store policy; React retains an unknown command key for
  exact replay and rotates it only after authoritative acceptance.
- The production React shell remains additive at
  `/controlled/s01/react`; `/controlled/s01` stays available as the legacy
  fallback. URL state is limited to non-sensitive work-item navigation and no
  browser storage persists restricted values or credentials.

## Focused verification

Commands were run from the repository root after #50 generated assets reached
`a1165a6`.

```text
.venv/bin/pytest -q tests/test_s15_policy_owner.py
23 passed in 108.71s (0:01:48)
```

```text
npm run test:unit -- src/components/ReviewWorkPanel.test.tsx src/api/hooks.test.tsx -t 'reveal|Reveal|restricted|source masked|expiry|ineligible'
Test Files  2 passed (2)
Tests  29 passed | 109 skipped (138)
```

```text
npx playwright test tests/test_t03_react.spec.js --workers=1 --grep 'S15 registered reveal lifetime|S15 registered late response'
3 passed (1.4m)
```

```text
npm run typecheck
tsc -p frontend/tsconfig.json --noEmit

npm run check:generated
Generated API types and OpenAPI document match the FastAPI authority.

npm run build
vite v8.2.0 ...
87 modules transformed
built in 565ms
```

The Python suite covers C19 vocabulary and scope, denied and hidden resources,
expiry, audit minimization, persistence failure, replay, and legacy-route
separation. Testing Library covers masked-by-default rendering, exact command
shape, key retention, expiry races, cleanup, ineligible links, and late
responses. The Playwright tracer runs the registered controlled path at
1280x800 and 390x844 plus a late-response expiry case, with sentinel leakage
checks and no screenshot or trace artifacts.

## Scope controls and unverified evidence

- This ticket changes only this delivery record and its review record. S15
  production code, generated contract, static bundle, and tests remain in their
  previously reviewed commits.
- Full pytest, the complete Playwright suite, `ci_gate.sh`, evaluate, attack
  probes, deployment packaging, and rollback against a live institution
  identity provider were not run in this ticket lane.
- Legacy `/controlled/s01` retirement remains outside T17 and requires a later
  parity, zero-caller, packaging, and rollback decision.
