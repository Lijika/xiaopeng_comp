# S17 Review R3

Fixed base `ee5bf28`.

## Standards

PASS. Commit now writes security audit, obligation event, job projection, and idempotency binding in one SQLite transaction. Worker lease owner/fence/attempt CAS is durable and terminal updates are guarded. Source locator persistence uses the registered `ExportSource.resolve_reference` seam keyed by scope fingerprint, keeping ledger records value-free and restart-safe.

## Spec

PASS for repository-level behavior. Controlled and HTTP tests pass 18 tests; React S17 smoke passes 2 tests; generated API check and production build pass. Institution KMS/AEAD, recipient registry, audit WORM/SIEM, storage, IdP/MFA, and A01-A14 retirement evidence remain G5 release prerequisites and are unverified here.
