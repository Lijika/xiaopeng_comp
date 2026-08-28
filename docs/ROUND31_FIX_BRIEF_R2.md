# Ticket 31/S15 Fix Brief R2

Source review `docs/ROUND31_REVIEW_R2.md`

The R2 verdict is FAIL. The same OMP session owns the following corrections. `GOAL.md` and `STATUS.md` remain Manager-only and must stay unchanged.

## Required corrections

1. Sanitize audit authorization fields in `task4_consistency/controlled/s01.py`. Persist `purpose`, `reason`, and `classification` only after the governed C19 decision confirms each value. Eligibility, region, authority, policy-unavailable, and other earlier failures must record stable codes without caller-controlled text. Preserve the metadata-only eligibility guard before `_admitted_evidence`, `_assemble_evidence`, and every registered source read.

2. Remove the synthetic application revision fallback at `task4_consistency/controlled/s01.py:11565-11569`. Use authentic work-item fixed revisions with labels that state their meaning, an explicit schema-supported unknown value, or a sanitized unavailable response when current authority cannot be proven. Audit events must never claim invented lifecycle or evidence revisions.

3. Route authority failures through the S15 fail-closed contract. Protect storage reload, work-item authority reconstruction after a visible resource is identified, finding/link metadata parsing, current-context reconstruction, admitted evidence parsing, supersession assembly, and selected binding resolution. Return stable stopped or unavailable outcomes with one safe audit whenever audit persistence remains available. Preserve cross-tenant and unauthorized existence hiding.

4. Replace the shared 206-line monkeypatch sequence in `tests/test_s15_policy_owner.py` with isolated phases. Save original descriptors before replacement or use `monkeypatch.context()` so the eligible phase uses real reload and immutable persistence. Verify the audit and idempotency binding survive a real reload.

5. Strengthen adversarial regression evidence. Add a code-shaped raw sentinel as an unknown `purpose`, `reason`, or `classification` on an early eligibility or region failure and prove it is absent from audit, replay storage, logs, and the response. Add distinct sibling observations and object references, then assert exactly one result-object read and one selected-object read with zero sibling reads. Assert `_admitted_evidence` damage and `_assemble_evidence` damage separately with the exact `stopped/SOURCE_EVIDENCE_UNAVAILABLE` result and mapped HTTP status.

6. Preserve the already-correct behavior. Keep per-link `evidence_eligible`, expiry-time rendering, mixed-link button behavior, selected binding verification, no-store responses, source-text filtering, audit/idempotency atomicity, unchanged business revisions, and the retired raw/direct-object/bulk/download/export/print/copy boundaries.

7. Keep generated delivery files coherent. Include `task4_consistency/web/static/react/index.html`, deletion of `assets/index-ChVt9ebc.js`, and addition of `assets/index-DwR5zzmb.js` together. Exclude unrelated untracked files.

## Allowed file boundary

- Expected production change is `task4_consistency/controlled/s01.py`.
- Expected regression changes are `tests/test_s15_policy_owner.py` and, only when HTTP mapping coverage requires it, the existing focused S15 HTTP test location.
- Frontend source and generated bundle changes are already sufficient for the reviewed UI requirements.
- `GOAL.md`, `STATUS.md`, unrelated slices, public raw routes, direct-object access, bulk reveal, download, export, print, and copy surfaces remain outside this fix.

## Delivery verification

The OMP delivery should execute and report the focused S15 policy-owner tests, affected S04 controlled assertions, focused S15 HTTP coverage, and the `ReviewWorkPanel` unit file. Project-wide pytest, full Playwright, evaluate, attack scripts, and full gates remain outside this repair brief.

The same Codex worker must repeat a read-only Standards and Spec review after the repair. The handoff must include exact commands and results, the three-file generated bundle group, and every remaining unverified item.
