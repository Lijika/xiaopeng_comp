# Ticket 31/S15 Review

Date 2026-08-28

Reviewer is the dedicated Codex worker `ticket31_codex` using `gpt-5.6-sol` at `xhigh`. Review used the `code-review` skill against the current uncommitted worktree, issue #31, the worker plan, `CONTEXT.md`, ADRs, and `ARCHITECTURE.md`. The worker performed read-only inspection and ran no tests, builds, evaluate commands, scripts, or full gates.

## Verdict

FAIL

## Standards

PASS. `GOAL.md` and `STATUS.md` remained outside the diff. Production changes stay in the S15 authority and review panel. No dependency, API, or second-authority changes were found.

## Blocking findings

### P1 metadata-only ordering

`task4_consistency/controlled/s01.py:11686-11687` invokes `_admitted_evidence` and `_assemble_evidence` before the `evidence_eligible` guard at about line 11733. These helpers load or copy complete evidence containing raw-bearing fields such as `source_text`. The link metadata must be checked first, with no raw-bearing evidence load before an affirmative `evidence_eligible is True` result.

### P1 single-object source read

`task4_consistency/controlled/s01.py:10133-10165` makes `_review_source_evidence_readable` read the result object and iterate every evidence observation. Its call at about line 11767 permits multiple source-object reads for one reveal. The check must use the selected binding and read only the requested observation, plus any single result object required by the contract.

### P1 authority exception audit

Authority failures from `_admitted_evidence` or `_assemble_evidence`, including missing events, digest mismatch, and supersession errors, can escape as `RuntimeError`. The HTTP adapter at `task4_consistency/web/app.py:5327` maps only `QueryNotFound` and `ValueError`, so this path can return an unaudited 500. Every failure reveal must map to a stable stopped or unavailable outcome through the common outcome writer, with one safe audit, no raw, and unchanged business revision.

### P2 test evidence

`tests/test_s15_policy_owner.py:475-579` proves zero `read_object` calls for false or missing eligibility, but it monkeypatches `_admitted_evidence` to inject the eligibility state. That setup does not prove the guard precedes `_admitted_evidence`. Tests must set eligibility on finding-link metadata, inject failures into both `_admitted_evidence` and `read_object`, and assert that ineligible paths call neither. Eligible paths must assert the exact selected-object read set.

## Passing checks from this review

- `ReviewWorkPanel.tsx` directly checks `Date.now() / 1000 < expiresAt` in `revealedHere`.
- `ReviewWorkPanel.tsx` requires the current link's `evidence_eligible === true` before enabling reveal; mixed-link UI tests cover this.
- Existing audit raw filtering, idempotency, revision preservation, and legacy raw/direct-object/bulk/download/export/print/copy boundaries showed no new regression in the diff.
- The generated frontend bundle is required as a pair with `index.html`, but the new hash bundle is currently untracked and must be included with the old hash removal.

## Residual evidence

`/tmp/codex-ticket31-plan.md` is absent in the current filesystem. The worker retained the plan content in its session and used it for this review. Existing S09-S14 baseline failures and the isolated T03 Playwright flake are outside this S15 verdict.
