# Ticket #53 / T19 Delivery R2

Fixed point `0bd6d29` includes the repaired #52 authority and S17 backend
release. This round completes the S17 React operator surface and its focused
browser evidence.

## Delivered behavior

- Requesters define purpose, recipient, minimum fields and artifacts,
  classification, scope reference, and expiry. The preview freezes those values
  and displays only identifiers and digests.
- Independent approval uses an ephemeral `X-S17-Approver-Token` bearer header;
  the token is excluded from command bodies and cleared after each action.
- Commit, worker generation, one-time recipient access, confirmation, revoke,
  expiry, receipt, watermark, encryption-registration, delivery, and cleanup
  states are rendered from typed server responses.
- Error states expose registered error/reason codes only. No package bytes,
  source values, result URLs, or credentials enter browser storage or rendered
  text.

The released S17 API has requester revocation and no approver-deny command.
The panel labels revocation as requester-owned; authoritative approver denial
remains a backend contract gap.

## Verification

```text
.venv/bin/pytest -q tests/test_t19_react_app.py
1 passed
```

```text
npm run test:unit -- --run src/components/S17ExportPanel.test.tsx src/api/hooks.s17.test.tsx
10 passed
```

```text
npm run typecheck
npm run check:generated
npm run build
```

```text
npx playwright test tests/test_t19_react.spec.js --workers=1
2 passed (desktop 1280x800, mobile 390x844)
```

The HTTP preview adapter projects full domain results to the closed response
DTO. The fixture therefore exercises the same response validation boundary as
configured production S17.

The browser tracer uses separate requester and approver contexts, an isolated
worker/recipient API context, one-time replay rejection, receipt cleanup proof,
storage redaction, and horizontal-overflow checks.

## Scope

Only the existing typed S17 hooks, React panel, shared shell marker, CSS,
generated static build group, focused unit tests, FastAPI fixture, and T19
Playwright tracer changed. S17 authority remains server-owned.
