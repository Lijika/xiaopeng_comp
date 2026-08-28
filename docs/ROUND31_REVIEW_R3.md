# Ticket 31/S15 Read-only Review R3

Date 2026-08-28

## Fixed point and scope

- The fixed point recorded by `docs/ROUND31_REVIEW_R2.md` is `2b4092195ffa643b17b3c17b62f6fe1971d144d4`, which is the current `HEAD`.
- The reviewed delivery is the working tree difference from that fixed point, including untracked delivery files reported by `git status --short`.
- The `code-review` skill supplied independent Standards and Spec reviews.
- Review sources were GitHub issue #31, the retained Ticket 31 plan, `docs/ROUND31_REVIEW_R2.md`, `docs/ROUND31_FIX_BRIEF_R2.md`, `CONTEXT.md`, `ARCHITECTURE.md`, the relevant ADRs, and `AGENTS.md`.
- Verification used static source and diff inspection. Tests, builds, evaluate, attack scripts, project scripts, and gates remained unexecuted as required.

## Verdict

**FAIL**

## Blocking findings

### P1 Standards and Spec — `s15-reveal-audit/1` changed shape and value domains

Evidence

- `task4_consistency/controlled/s01.py:562` still declares `_REVEAL_AUDIT_SCHEMA = "s15-reveal-audit/1"`.
- `task4_consistency/controlled/s01.py:11431-11443` now permits `lifecycle_revision` and `evidence_revision` to be `None`.
- `task4_consistency/controlled/s01.py:11468-11492` now emits nullable revisions and conditionally omits `purpose`, `verification_reason`, and `classification` before C19 validation.
- At the fixed point, the same `/1` event always emitted the three vocabulary fields and integer revisions. The R2 change therefore alters both the field cardinality and value domains under an existing version.
- `CONTEXT.md:150-154` defines schema and semantic contract versions as immutable and requires a new version when existing meaning or default behavior changes. ADR-0004 and ADR-0008 assign revisions and security audit facts to explicit authorities and versioned references.

Acceptance mapping

- Issue #31 C14/F requires a complete, safe, attributable reveal audit.
- R2 fix brief requirements 1 and 2 require sanitized pre-C19 audit data and authentic revision facts. Those requirements also preserve the repository's versioned contract rules.

Required fix

- Publish an explicit `s15-reveal-audit/2` schema for nullable revisions and optional governed vocabulary, with historical `/1` events remaining readable, or preserve the established `/1` field shape and value domains using schema-valid authority-owned values.
- Add compatibility assertions for both historical `/1` and the chosen current schema.

### P1 Spec — missing application plus audit or storage failure escapes the fail-closed outcome

Evidence

- `task4_consistency/controlled/s01.py:11387` accepts `app: dict[str, Any] | None`.
- `task4_consistency/controlled/s01.py:11678-11682` intentionally routes a visible work item with missing application authority through the common outcome writer with `app=None`.
- When `_before_write` or `staged.persist()` fails, `task4_consistency/controlled/s01.py:11498-11508` enters the recovery branch, calls `_reload_store()`, and evaluates `app["application_id"]`.
- That expression raises `TypeError` for the admitted `None` value. A recovery reload failure can also escape this branch. The endpoint then loses the promised stable no-value result and reaches the generic HTTP 500 path.

Acceptance mapping

- Issue #31 C14/F and R2 fix brief requirement 3 require every identifiable reveal attempt to converge on a sanitized fail-closed outcome, with audit and idempotency behavior remaining atomic.

Required fix

- Build the unavailable response from the stable `work_item["application_id"]` reference.
- Protect recovery reload so its own failure still returns the same sanitized unavailable contract.
- Add focused cases for `app=None` combined separately with audit-write failure, persistence failure, and recovery-reload failure. Assert the mapped HTTP status, `no-store`, zero raw fields, zero partial audit, and zero idempotency binding when persistence fails.

### P1 Spec — work-item authority failures bypass the common attempted-action outcome

Evidence

- `task4_consistency/controlled/s01.py:11591-11606` catches non-`QueryNotFound` failures from `_review_work_item_authority` and returns a plain dictionary.
- The outcome closure and `_record_reveal_outcome` call are created later at `task4_consistency/controlled/s01.py:11633-11665`.
- A damaged digest, missing authority event, or reconstruction failure for a resource that can be safely identified therefore produces no `evidence_source_revealed` attempted-action audit and receives no protected idempotency handling even when the audit store remains writable.
- `tests/test_s15_policy_owner.py` contains no bootstrap reload or work-authority damage case.

Acceptance mapping

- Issue #31 C14/F requires one safe audit for an identifiable attempt and stable fail-closed behavior.
- R2 fix brief requirement 3 explicitly covers storage reload and work-item authority reconstruction while preserving unauthorized and cross-tenant existence hiding.

Required fix

- Establish a minimally trusted work-item reference before full authority reconstruction, limited to the identifiers and visibility data required for a safe audit.
- Route reconstruction damage through the common outcome writer after tenant, scope, and resource visibility are proven. Keep `QueryNotFound` for unauthorized, cross-tenant, and unidentifiable resources. A genuine storage outage may return the stable unavailable response without persisted audit.
- Add distinct regression cases for bootstrap storage outage, visible work-item authority damage, unauthorized access, and cross-tenant access. For the visible damaged case, assert exactly one safe audit, stable replay behavior, and zero raw content.

### P1 Spec test evidence — admitted-evidence damage test cannot reach its injected fault

Evidence

- `tests/test_s15_policy_owner.py:767-785` supplies `CALLER_R2_DAMAGE_ADMITTED` as `purpose`, `reason`, and `classification` while monkeypatching `_admitted_evidence`.
- `task4_consistency/controlled/s01.py:11772-11788` resolves the governed C19 policy and rejects unknown vocabulary with `REVEAL_VOCABULARY_UNKNOWN`.
- `_admitted_evidence` is reached later in the reveal flow. The test assertions at `tests/test_s15_policy_owner.py:788-789` expect `stopped/SOURCE_EVIDENCE_UNAVAILABLE`, so the stated branch and expected result are statically incompatible.
- The replay call at `tests/test_s15_policy_owner.py:816-832` repeats the same incompatible vocabulary.

Acceptance mapping

- R2 fix brief requirement 5 requires separate, exact regressions for `_admitted_evidence` and `_assemble_evidence` damage. This test currently supplies no evidence for the admitted-evidence branch.

Required fix

- Use the governed values already defined by `_reveal_args` at `tests/test_s15_policy_owner.py:92-102` for the admitted-evidence damage and replay calls.
- Keep the caller sentinel leak proof in the pre-C19 eligibility or region failure cases, where that input is intentionally reachable.
- Preserve exact assertions for `stopped/SOURCE_EVIDENCE_UNAVAILABLE`, one safe audit, idempotent replay, unchanged revisions, and absent raw data.

## Passing static evidence

- Metadata-only `evidence_eligible is True` enforcement occurs at `task4_consistency/controlled/s01.py:11683-11726`, before `_admitted_evidence`, `_assemble_evidence`, and every registered source read.
- False and missing eligibility cases guard `_admitted_evidence`, `_assemble_evidence`, and `read_object` at `tests/test_s15_policy_owner.py:524-657`. Both cases statically require zero source reads.
- Selected-binding integrity reads the result object and selected observation object at `task4_consistency/controlled/s01.py:10206-10232`. The distinct sibling fixture and exact ordered call assertion are at `tests/test_s15_policy_owner.py:662-726`.
- Caller vocabulary enters audit only through `governed_vocabulary` after C19 validation at `task4_consistency/controlled/s01.py:11777-11795`; pre-C19 outcomes omit caller-controlled vocabulary.
- Safe result filtering and staged audit plus idempotency persistence remain at `task4_consistency/controlled/s01.py:11425-11497`. The missing-application failure branch described above remains blocking.
- The focused HTTP authority-damage test asserts 503, `no-store`, and absent raw value at `tests/test_s15_policy_owner.py:982-999`.
- `ReviewWorkPanel` requires the current time to precede `expiresAt` at `frontend/src/components/ReviewWorkPanel.tsx:978-982`. Expiry rerender coverage is at `frontend/src/components/ReviewWorkPanel.test.tsx:2804-2860`.
- Each evidence link independently controls its reveal button through `link.evidence_eligible === true` at `frontend/src/components/ReviewWorkPanel.tsx:1042-1060`. Mixed-link coverage is at `frontend/src/components/ReviewWorkPanel.test.tsx:2862-2915`.
- The generated frontend group consists of modified `task4_consistency/web/static/react/index.html`, deleted `assets/index-ChVt9ebc.js`, and untracked `assets/index-DwR5zzmb.js`. The new bundle must be included with the tracked pair.
- `GOAL.md` and `STATUS.md` have no worktree diff. OpenAPI generated files have no diff.
- The diff adds no raw, direct-object, bulk reveal, download, export, print, or copy endpoint. Existing denial coverage remains at `tests/test_s15_policy_owner.py:383-472`.
- The adjacent additions at `tests/test_s04_controlled.py:605` and `tests/test_s04_controlled.py:640` align complete-object assertions with existing fields and do not widen S15 behavior.

## Standards judgement findings

- Duplicated Code appears around `task4_consistency/controlled/s01.py:10171-10235`, `task4_consistency/controlled/s01.py:10237-10280`, and the reveal gates around `task4_consistency/controlled/s01.py:11856`. Registered context validation, result-object integrity, and several audit/storage gates repeat existing logic. This is non-blocking for R3 because a shared refactor would enlarge a security-sensitive repair.
- Feature Envy appears in the store-internal setup beginning at `tests/test_s15_policy_owner.py:475`. The test reaches into findings, reload, immutable-row synchronization, and persistence internals. This is non-blocking because the isolated `monkeypatch.context()` phases now restore the real persistence path for the eligible case.

## Residual risk and unexecuted verification

- Every conclusion is based on static inspection. Runtime behavior, test collection, branch reachability under fixtures, TypeScript rendering, generated-bundle equivalence, and HTTP adapter execution remain unverified.
- `task4_consistency/web/static/react/assets/index-DwR5zzmb.js` remains untracked. A tracked-only commit would omit the asset referenced by `index.html`.
- The working tree contains many unrelated untracked files. Delivery selection must include only the Ticket 31 files and the required review documents.
- The audit schema migration choice requires an explicit compatibility decision and consumer review before acceptance.
