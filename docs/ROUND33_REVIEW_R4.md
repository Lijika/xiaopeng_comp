# S17 Review R4

Fixed base `243b99f` with the R4 working-tree repairs.

## Standards

PASS for the repository implementation.

- `TASK4_S17_SERVICE_FACTORY` is an explicit institution-owned injection seam. Identity, state, source, recipient, provider, audit, storage, and factory configuration are checked before service construction. Missing or malformed configuration records `S17_CONFIGURATION_ERROR` and keeps the HTTP plane at typed 503.
- Commit persistence writes the redacted SQLite `security_audit` fact, obligation event, job projection, and idempotency binding in one transaction. The external audit sink receives the same record after a successful transaction, so a database rollback leaves no external audit side effect.
- Source locators stay behind `ExportSource.resolve_reference(tenant_scope, scope_fingerprint)`. Resolver failure releases the claimed job through the owner/fence/attempt CAS path and returns `S17_SOURCE_DRIFT`.
- Worker claims include durable owner, fence, attempt, and lease expiry. Expired processing leases are eligible for recovery after restart. Successful reconciliation closes the timeout job before another delivery attempt can be claimed.

## Spec

PASS for repository-level behavior. The focused controlled and HTTP suite passes 21 tests. React S17 smoke passes 2 tests; generated API check and production build pass. `git diff --check` passes.

The implementation evidence uses C-DEMO and C-DEV-REG providers. Institution KMS/AEAD, recipient registry, audit WORM/SIEM, storage, IdP/MFA, service-factory integration, and A01-A14 retirement evidence remain G5 release prerequisites and require deployment verification.
