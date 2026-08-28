# Ticket 31/S15 Fix Brief

Source review `docs/ROUND31_REVIEW.md`

Owner is the same OMP session `w1:p1F` for Ticket #31/S15. The owner must call `/skill:implement` before editing and must keep `GOAL.md` and `STATUS.md` unchanged.

## Required changes

1. In `task4_consistency/controlled/s01.py`, read only finding-link metadata to decide `evidence_eligible is True`. Keep `_admitted_evidence`, `_assemble_evidence`, and every raw-bearing observation load after that decision. Ineligible or missing metadata must produce one safe audited outcome, zero `RegisteredSourceBoundary.read_object` calls, zero raw, and unchanged business revision.
2. Make source readability validation use the selected binding. A single reveal may read only the requested observation and the one result object required by the contract. Do not iterate all evidence observations.
3. Map authority parse, missing-event, digest, and supersession exceptions to the common stopped or unavailable outcome. The HTTP path must not expose an unaudited 500. Preserve audit atomicity, idempotency, raw filtering, and revision invariants.
4. Update `tests/test_s15_policy_owner.py` to set link metadata directly, inject failures into `_admitted_evidence` and `read_object`, prove zero calls on ineligible and missing metadata, and assert the exact selected-object read set for eligible reveal.
5. Keep frontend generated artifacts as a coherent pair when required by the project. The new hash bundle must be present with the updated `index.html` and old hash removal. Leave unrelated untracked files alone.

## Allowed verification

Run only S15 tests, the associated S04 controlled and HTTP tests, and the focused ReviewWorkPanel unit tests listed in the original worker plan. Do not run project-wide pytest, full Playwright, `ci_gate.sh`, or full evaluate.

## Handoff evidence

Report modified files, `/skill:implement` use, focused test commands and results, and any unverified item. After delivery, the same Codex worker `ticket31_codex` must perform a read-only code review without running tests.
