# Ticket #53 / T19 Delivery R1

## Scope

T19 evaluates the S17 controlled-export React surface at fixed point
`c2ab8c0`, which contains the #52 delivery record and the released S17 backend
and React implementation. The existing `S17ExportPanel`, S17 hooks, generated
OpenAPI contract, `/controlled/s17` shell, and static build are present. This
ticket adds delivery evidence and records the blocking gaps; it does not add a
second export authority or invent frontend state while the owning migration
dependency is blocked.

## Existing implementation evidence

- The shell is mounted at `/controlled/s17` and `/controlled/s17/react`.
- The panel can submit a preview, display the request state, submit approval,
  commit generation, and read a minimized receipt through typed S17 hooks.
- The generated contract contains preview, query, access, approval, commit,
  confirm, expire, receipt, revoke, process, and typed error paths. FastAPI
  remains the only S17 authority for identity, immutable request binding,
  delivery token, package generation, watermark/encryption status, cleanup,
  audit, and one-time confirmation.

## Focused verification

Commands were run from the repository root.

```text
.venv/bin/pytest -q tests/test_s17_controlled.py tests/test_s17_http.py
21 passed in 11.02s
```

```text
npm run test:unit -- src/components/S17ExportPanel.test.tsx src/api/hooks.s17.test.tsx
Test Files  2 passed (2)
Tests  2 passed (2)
```

```text
npm run typecheck
tsc -p frontend/tsconfig.json --noEmit

npm run check:generated
Generated API types and OpenAPI document match the FastAPI authority.

npm run build
87 modules transformed
built in 616ms
```

There is no `tests/test_t19_react.spec.js` or equivalent T19 Playwright tracer
in the repository. `tests/test_t17_react.spec.js` exercises S16 deletion and
does not provide T19 export evidence, so no Playwright result is claimed here.

## Blocking gaps

The current panel does not satisfy the complete #53 issue contract.

- Purpose, recipient, minimum fields/artifacts, classification, and expiry are
  hard-coded in `submitPreview`; the requester cannot define and review the
  complete fixed request in React.
- Approval has no independent approver credential or `X-S17-Approver-Token`
  input. The panel uses the shared same-origin request adapter, so requester
  and approver separation has no reachable React path.
- The panel has no access, confirm, expire, or delivery-token flow, and does
  not render one-time/short-lived delivery, denial, expiry, reuse,
  watermark/encryption, generation failure, or partial-cleanup states.
- The focused React tests cover only shell rendering and query-key shape. They
  do not exercise fixed-request immutability, approval separation, delivery,
  redaction, cleanup, or responsive/browser behavior.

#52 is a prerequisite and remains blocked. `docs/ROUND52_REVIEW_R1.md` records
five reproducible S16 failures involving wrong-fence and wrong-operation
quarantine bookkeeping, old-schema marker rejection, second-identity residue,
and before-return tamper repair. The T19 ticket cannot pass its required
affected-consumer and dependency gates while those authority failures remain.

## Unverified evidence

- Full pytest, the complete Playwright suite, `ci_gate.sh`, evaluate, attack
  probes, installed-artifact verification, deployment packaging, and live
  identity-provider rollback were not run in this ticket lane.
- T19 Playwright, interaction-level React tests, and production delivery
  verification remain open after #52 repair and completion of the missing UI
  seams above.
