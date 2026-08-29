# Ticket #53 / T19 Two-Axis Review R2

## Standards

PASS for the frontend diff. The panel follows the existing typed React Query
adapter pattern, keeps mutations retry-free, invalidates the S17 query group,
uses no browser persistence, clears ephemeral credentials, and renders only
closed server DTO fields. Desktop and mobile browser checks show no horizontal
overflow and no cross-plane requests.

## Spec

PASS for the React contract exercised by the focused fixture and browser
tracer. User-defined fixed request fields, independent approver header,
approval/deny/commit/process/access/confirm/expire/revoke controls, one-time replay
handling, watermark/encryption registration, cleanup, requester revocation,
and typed error states are reachable and tested. Reveal and download controls
remain absent.

The S17 authority now exposes a dedicated approver `deny` command bound to the
same preview digest and idempotency contract. Denied requests become terminal,
cannot be approved or committed, and are covered by domain, HTTP, and React
regressions.

## Follow-up authority verification

The preview route now projects the domain result to the closed
`S17PreviewResponse` field set before FastAPI validation. The T19 fixture uses
the unadapted domain preview result, so `tests/test_t19_react_app.py` exercises
the production response boundary and confirms the configured preview succeeds.

Full repository pytest/Playwright, deployment packaging, live identity
providers, and production rollback remain outside this ticket lane.
