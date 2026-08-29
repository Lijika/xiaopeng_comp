# S17 Delivery R1

Ticket #33 implements the governed export seam at base `1f2d5ecddf3249b3ada717c33ef90105a20c2273`.

Implemented paths include the independent S17 ledger/orchestrator, typed FastAPI routes, default-closed app registration, typed React request/query hooks, and the S17 export panel mounted at `/controlled/s17` and `/controlled/s17/react`.

Focused verification

- `.venv/bin/pytest -q tests/test_s17_controlled.py` -> 16 passed.
- `npx tsc --noEmit -p frontend/tsconfig.json` -> passed.
- `.venv/bin/python -m py_compile task4_consistency/controlled/s17.py task4_consistency/web/s17_http.py` -> passed.
- OpenAPI inspection confirms all S17 API and shell paths are registered.
- `npm run test:unit -- src/components/S17ExportPanel.test.tsx src/api/hooks.s17.test.tsx` -> 2 passed.
- R3 repair focused tests -> 18 backend tests passed, 2 React tests passed, and generated API check passed.

The test providers are C-DEMO/C-DEV-REG seams. Institution KMS/AEAD, recipient registry, audit WORM/SIEM, storage, IdP/MFA and A01-A14 retirement evidence remain G5 prerequisites and are unverified in this repository.

Full pytest, full Playwright, evaluate, attack probes, ci gate and production build remain outside the ticket lane per ROUND33_PLAN.

Review status

- Standards axis reports worker lease/CAS, command-job-binding transaction atomicity, and cross-process source recovery as follow-up findings. S17 remains default-closed for production G5 because institution identities, KMS/AEAD, storage, audit and retirement evidence are unavailable here.
- Spec axis evidence is covered by the focused controlled, HTTP/OpenAPI and React smoke tests listed above. Full browser path and institution provider verification remain unverified.
