# Ticket 31/S15 Final Read-only Review R2

Date 2026-08-28

## Fixed point and scope

- Fixed point `2b4092195ffa643b17b3c17b62f6fe1971d144d4` from the current `HEAD`.
- Worktree comparison `git diff HEAD --`, plus `git status --short` for untracked delivery files.
- Commit range `HEAD..HEAD` is empty because this review covers the uncommitted OMP worktree.
- Review method was the `code-review` skill with independent Standards and Spec axes.
- Sources were GitHub issue #31, the session-retained plan content, `docs/ROUND31_REVIEW.md`, `docs/ROUND31_FIX_BRIEF.md`, `CONTEXT.md`, `ARCHITECTURE.md`, ADR-0002, ADR-0003, ADR-0004, ADR-0006, ADR-0008, and `AGENTS.md`.
- `/tmp/codex-ticket31-plan.md` is absent from the current filesystem.
- This review ran no tests, builds, evaluate commands, scripts, or gates.

## Verdict

**FAIL**

## Standards

**FAIL with one hard violation and two judgement findings.**

### P1 fabricated lifecycle and evidence revisions

`task4_consistency/controlled/s01.py:11565-11569` constructs a fallback application with `lifecycle_revision` and `evidence_revision` set to `0` when application authority is missing. `_record_reveal_outcome` then records those values as audit context at `task4_consistency/controlled/s01.py:11453-11454`.

This creates a revision fact that did not come from Lifecycle or Evidence authority. It violates ADR-0004 line 7, which makes Lifecycle the unique owner of application state and requires versioned references, and ADR-0008 lines 47-58, which assign revision and audit facts to explicit authorities. A damaged authority path must use an authentic fixed work-item reference, an explicit unknown representation allowed by the schema, or return a sanitized unavailable result without persisting invented revisions.

### Judgement findings

- Possible Duplicated Code at `task4_consistency/controlled/s01.py:10171-10235` and `10237-10280`. The new target source helper repeats registered context, result-object validation, source hashing, and exception folding from the existing source gate. The reveal path also repeats audit, storage, S14, and S09 checks. A selected-binding parameter on the existing gate would keep one authority implementation.
- Possible Feature Envy at `tests/test_s15_policy_owner.py:475-678`. The test mutates store findings and replaces reload and immutable persistence internals to exercise one reveal branch. The coupling already causes an invalid restoration described below.

`GOAL.md` and `STATUS.md` have no worktree diff. No dependency or OpenAPI generated-file change is present.

## Spec

**FAIL with two P1 findings and two P2 test-evidence findings.**

### P1 unvalidated caller codes can enter the security audit

The new metadata failures at `task4_consistency/controlled/s01.py:11644-11707` call the common outcome writer before governed C19 vocabulary validation at `11717-11733`. The writer records caller-provided `purpose`, `reason`, and `classification` verbatim at `11447-11449`.

The HTTP model at `task4_consistency/web/app.py:2420-2428` only constrains these values to uppercase code-shaped strings. A raw value such as an uppercase identifier can therefore be supplied as an unknown code and persisted during an ineligible, ambiguous, region-mismatch, projection-damage, or context-damage outcome. Direct service callers receive even weaker non-empty-string validation at `task4_consistency/controlled/s01.py:11531-11543`.

This violates issue #31 C14/F and the acceptance rule that audit metadata contain no raw value or free text. The outcome writer must persist these three fields only after an affirmative governed vocabulary decision. Earlier outcomes need stable sanitized metadata that excludes caller text.

### P1 authority failures still bypass the common outcome

`task4_consistency/controlled/s01.py:11556-11561` reloads storage and resolves work-item authority before the common outcome closure exists. Authority discontinuity can raise `RuntimeError`. The link traversal at `11635-11666` is also outside an exception boundary and assumes every finding, `evidence_links` value, and link has the expected shape. Damaged immutable authority can therefore reach the central middleware's sanitized 500 at `task4_consistency/web/app.py:818-829` without an S15 attempt outcome or audit.

This violates issue #31 C14/F and `docs/ROUND31_FIX_BRIEF.md` requirement 3. Once identity and a visible work-item reference are established, authority parse, missing-event, digest, and supersession failures must use the common stopped or unavailable contract. Storage or audit failure may prevent persistence, but the HTTP result still needs the stable fail-closed S15 status.

### P2 the ineligible/eligible test does not restore immutable persistence

`tests/test_s15_policy_owner.py:505-509` replaces `_sync_immutable_rows`. Line 581 reads the already patched attribute and assigns that same no-op back. The eligible path at `616-633` therefore bypasses real immutable audit and idempotency persistence. Save the original descriptor before patching or use a separate `monkeypatch.context()` for the ineligible phase.

### P2 targeted-read and authority assertions are too permissive

The eligible read assertion at `tests/test_s15_policy_owner.py:626-633` uses a fixture derived from `tests/test_s02_controlled.py:80-96`, which has one observation and one observation object. The prior bulk-reading implementation would read the same allowed set, so this fixture cannot prove that sibling observations remain unread. Use at least two eligible observations backed by distinct object references and assert the exact ordered call list or exact per-reference counts.

The deterministic `_admitted_evidence` failure at `tests/test_s15_policy_owner.py:641-653` permits two statuses and five reason codes. It should assert `stopped` with `SOURCE_EVIDENCE_UNAVAILABLE`. Add a separate `_assemble_evidence` damage case and an HTTP adapter assertion for the mapped status.

## Passing evidence

- Metadata-only link resolution begins at `task4_consistency/controlled/s01.py:11635`; `evidence_eligible is True` is required at `11670` before `_admitted_evidence` and `_assemble_evidence` at `11825-11826` and before registered source reads at `10171-10235` through the call at `11887`.
- False and missing eligibility cases install raising guards for `_admitted_evidence`, `_assemble_evidence`, and `read_object` at `tests/test_s15_policy_owner.py:511-566`.
- `_review_target_source_readable` reads the registered result object at `task4_consistency/controlled/s01.py:10206-10216` and only the selected observation object at `10218-10232`; it does not iterate sibling observations.
- Evidence parse and supersession failures after eligibility are mapped through the common outcome at `task4_consistency/controlled/s01.py:11824-11832` and `11834-11870`.
- `_record_reveal_outcome` removes `source_text` from replay persistence at `task4_consistency/controlled/s01.py:11420-11424`; audit and idempotency are staged and persisted together at `11419-11476`. The unvalidated-code finding above remains blocking.
- Reveal does not mutate lifecycle or evidence revisions. Existing S15 assertions cover this at `tests/test_s15_policy_owner.py:295-310`, `475-566`, and `636-678`.
- `ReviewWorkPanel` checks `Date.now() / 1000 < expiresAt` at `frontend/src/components/ReviewWorkPanel.tsx:978-982`. The expiry rerender case is at `frontend/src/components/ReviewWorkPanel.test.tsx:2804-2860`.
- Each link controls its own reveal button through `link.evidence_eligible === true` at `frontend/src/components/ReviewWorkPanel.tsx:1042-1059`. Mixed eligible and ineligible UI coverage is at `frontend/src/components/ReviewWorkPanel.test.tsx:2862-2915`.
- `task4_consistency/web/static/react/index.html` references `index-DwR5zzmb.js`, the old `index-ChVt9ebc.js` is deleted, and the new bundle exists without a sourcemap reference. The new bundle remains untracked and must be included with the other two paths.
- No new direct-object, bulk reveal, download, export, print, or copy endpoint appears in the diff. Legacy raw and batch denial coverage remains at `tests/test_s15_policy_owner.py:383-472`.
- `tests/test_s04_controlled.py:605` and `640` align complete-object assertions with existing `evidence_ready` and correction `cycle` fields; they do not widen S15 behavior.

## Residual risk

- Every conclusion is from static inspection because the review contract prohibited execution-based verification.
- The missing `/tmp/codex-ticket31-plan.md` limits filesystem-level reproduction of the original plan comparison; the session-retained plan and both Round 31 documents supplied the acceptance mapping.
- `task4_consistency/web/static/react/assets/index-DwR5zzmb.js` is untracked, so a tracked-only commit operation would omit the bundle referenced by `index.html`.
