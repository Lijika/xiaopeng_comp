# S17 Review R2

Fixed base `ee5bf28`.

## Standards

R2 repair evidence confirms durable `lease_owner`, fence and attempt fields, guarded terminal updates, durable token/job reload, lifecycle idempotency bindings, and explicit app configuration gate. Remaining production findings are command/job/binding atomic transaction across one SQLite transaction and durable source-reference adapter for cross-process snapshot recovery. The environment mounts `.git` read-only, so this repair round cannot create a commit from this worker.

## Spec

`tests/test_s17_controlled.py` and `tests/test_s17_http.py` pass 18 tests. React S17 smoke passes 2 tests; generated OpenAPI check and production build passed before this repair round. Institution KMS/AEAD, recipient registry, audit WORM/SIEM, storage, IdP/MFA and A01-A14 retirement evidence remain G5 prerequisites.

Verdict is conditional pending atomic transaction and production source adapter evidence.

## R3

The commit path now persists audit event, obligation event, job projection, and idempotency binding in one SQLite transaction. Source locators remain outside the ledger; `ExportSource.resolve_reference(tenant_scope, scope_fingerprint)` is the restart-safe adapter seam used by commit and worker recovery. Controlled and HTTP tests pass 18 cases, and the generated React group remains current. Standards and Spec verdicts are PASS for repository behavior; G5 institution provider and retirement evidence remain external prerequisites.
