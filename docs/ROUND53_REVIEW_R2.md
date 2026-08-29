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
approval/commit/process/access/confirm/expire/revoke controls, one-time replay
handling, watermark/encryption registration, cleanup, requester revocation,
and typed error states are reachable and tested. Reveal and download controls
remain absent.

The released S17 authority exposes requester `revoke` and has no approver-deny
command. The React surface therefore cannot offer an authoritative approver
denial action; the existing revoke control is requester-owned and is labelled
accordingly. Adding an approver-deny route and DTO is required to satisfy the
literal approve-or-deny acceptance criterion.

## Residual authority finding

The released `task4_consistency/web/s17_http.py` preview route currently returns
the domain preview dictionary through `S17PreviewResponse` with `extra="forbid"`.
Configured S17 service responses therefore raise FastAPI
`ResponseValidationError` for the domain's purpose, fields, artifacts,
recipient, classification, expiry, source revisions, and policy digest keys.
The T19 fixture adapts domain results to the closed command DTO to keep the
frontend tracer focused. The HTTP authority owner must align the response DTO
or return projection before claiming configured production preview evidence.

Full repository pytest/Playwright, deployment packaging, live identity
providers, and production rollback remain outside this ticket lane.
