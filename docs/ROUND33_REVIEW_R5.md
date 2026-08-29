# S17 Review R5

Fixed base `c64ac69` with the explicit application factory gate.

## Standards

PASS for the changed application wiring.

- S17 remains default-closed when any required identity, state, source, recipient, provider, audit, storage, or factory input is absent.
- The factory is an explicit `module:callable` injection point and accepts only a `GovernedExportService` whose `ready()` predicate is true.
- S17 identities are checked for internal uniqueness and for aliases to configured controlled identities before dynamic import or service construction.
- Factory failures are recorded as `S17_CONFIGURATION_ERROR` and leave router requests at typed 503 without exposing exception details.

## Spec

PASS for repository-level behavior. Focused S17 controlled and HTTP tests pass 21 tests; `py_compile` and `git diff --check` pass. The default test environment has no G5 institution inputs, so `S17_SERVICE` remains unavailable as required.

Institution KMS/AEAD, recipient registry, audit WORM/SIEM, storage, IdP/MFA, and production service-factory integration remain deployment prerequisites and require environment verification.
