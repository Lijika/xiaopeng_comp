# S17 Review R1

Fixed base `1f2d5ecddf3249b3ada717c33ef90105a20c2273`.

## Standards

Findings from the independent review

- P1 worker lease/fence remains process-local. `_jobs` and `RLock` select work; terminal updates lack lease-owner, fence and attempt CAS.
- P1 commit event, job row and idempotency binding use separate transactions; concurrent same-key calls can split or overwrite facts.
- P1 restart recovery relies on process-global `_RUNTIME_SCOPE_REFS`; a new process cannot resume source snapshot safely.
- P1 configured app deployment has no S17 factory/readiness construction and therefore remains permanently unavailable.

## Spec

Focused controlled tests cover minimum scope, SoD, provider ordering, recipient/token binding, reconciliation, cleanup, audit and restart projections. HTTP/OpenAPI smoke covers route registration and generated schemas. React smoke covers panel mount and query-key separation. Full browser, institution provider, and A01-A14 retirement evidence remain unverified.

Verdict is Standards conditional and Spec focused-pass with the documented G5 prerequisites outstanding.
