import { randomUUID } from "node:crypto";
import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, expectTypeOf, it, vi } from "vitest";
import type { ReactNode } from "react";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { paths } from "../generated/api";
import { HttpError, isDefinitiveRejection } from "./client";
import {
  correctionConverged,
  useApplicationHistory,
  useClaimWorkItem,
  useCorrectFieldObservation,
  useCorrectionConvergence,
  useCurrentRoute,
  useQueue,
  useRecoveryWork,
  useRevealFieldObservation,
  useSubmitVerification,
  type ClaimCommand,
  type CorrectionCommand,
  type FencedCommand,
  type RevealCommand,
  type SubmitCommand,
  type VerifyRecoveryCommand,
} from "./hooks";
import {
  createQueryClient,
  fetchRouter,
  restrictedDigest,
} from "../test-utils";

function wrap(client: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

const WORK_ID = "recovery_work_t01retry1234567890abcdef";
const APP_ID = "app_t01retry9876543210fedcba";
const WORK_PATH =
  "/controlled/s01/api/queries/recovery-work-items/recovery_work_t01retry1234567890abcdef";
const QUEUE_PATH = "/controlled/s01/api/queries/queue";
const ROUTE_PATH =
  "/controlled/s01/api/queries/applications/app_t01retry9876543210fedcba/current-route";
const HISTORY_PATH =
  "/controlled/s01/api/queries/applications/app_t01retry9876543210fedcba/history";
const RESTRICTED_CORRECTION_RAW = `restricted-correction:${randomUUID()}`;
const RESTRICTED_SOURCE_TEXT = `restricted-source:${randomUUID()}`;

function workPayload(overrides: Record<string, unknown> = {}) {
  return {
    schema_version: "recovery-work-view/1",
    recovery_work_id: WORK_ID,
    status: "open",
    application_id: APP_ID,
    cycle: 1,
    phase: "Unprocessable",
    route: "unprocessable",
    lifecycle_revision: 5,
    evidence_revision: 1,
    primary_reason_code: "configuration.checker_unavailable",
    related_reason_codes: [],
    operation: "execute_check_run",
    dependency: "c-demo-target-checker",
    logical_operation_id: "job_t01retry000000000000000000",
    attempts: [
      {
        attempt: 1,
        classification: "terminal",
        status: "blocked",
        started_at: 10,
        retry_not_before: null,
      },
    ],
    responsible_party: "policy_owner",
    recovery_action: "restore_exact_release_or_activate_compatible_successor",
    recovery_target: "Evidence Ready",
    criterion: {
      id: "s07-checker-compatibility/1",
      version: "1",
      operation: "execute_check_run",
      dependency: "c-demo-target-checker",
      required_conditions: ["configuration.checker_unavailable"],
      trusted_verifier: "policy_owner",
      evidence_kind: "checker_compatibility_probe",
      conditions: [
        {
          condition_id: "s07-checker-compatibility/1",
          reason_code: "configuration.checker_unavailable",
        },
      ],
      digest: "a".repeat(64),
    },
    retry_policy: {
      id: "s07-c-demo-retry/1",
      max_attempts: 3,
      retry_offsets_seconds: [1, 2],
      jitter: false,
    },
    outcome_known: true,
    retryable: false,
    recovery_fact_count: 0,
    resolution_count: 0,
    job_status: "blocked",
    delivery_semantics: "at_least_once",
    protected_business_revision: 0,
    current_run_id: null,
    projection_watermark: 1,
    can_verify: false,
    ...overrides,
  };
}

describe("generated request body binding (S2)", () => {
  it("binds the VerifyRecovery command type to the generated OpenAPI request body", () => {
    type GeneratedBody = paths["/controlled/s01/api/commands/recovery-work-items/{recovery_work_id}/verify"]["post"]["requestBody"]["content"]["application/json"];
    expectTypeOf<VerifyRecoveryCommand>().toEqualTypeOf<GeneratedBody>();
    expectTypeOf<keyof GeneratedBody>().toEqualTypeOf<
      "expected_lifecycle_revision" | "expected_criterion_digest" | "idempotency_key"
    >();
  });

  it("binds the S01 manual-review command types to the generated OpenAPI request bodies", () => {
    type ClaimBody = paths["/controlled/s01/api/commands/review-work-items/{work_item_id}/claim"]["post"]["requestBody"]["content"]["application/json"];
    type RenewBody = paths["/controlled/s01/api/commands/review-work-items/{work_item_id}/renew"]["post"]["requestBody"]["content"]["application/json"];
    type SubmitBody = paths["/controlled/s01/api/commands/review-work-items/{work_item_id}/submit"]["post"]["requestBody"]["content"]["application/json"];
    expectTypeOf<ClaimCommand>().toEqualTypeOf<ClaimBody>();
    expectTypeOf<FencedCommand>().toEqualTypeOf<RenewBody>();
    expectTypeOf<SubmitCommand>().toEqualTypeOf<SubmitBody>();
    expectTypeOf<keyof ClaimBody>().toEqualTypeOf<"expected_context">();
    expectTypeOf<keyof RenewBody>().toEqualTypeOf<
      "expected_context" | "expected_fence" | "idempotency_key"
    >();
    expectTypeOf<keyof SubmitBody>().toEqualTypeOf<
      "expected_context" | "expected_fence" | "idempotency_key" | "verification"
    >();
  });

  it("binds the T03 reveal and correction command types to the generated OpenAPI bodies", () => {
    type RevealBody = paths["/controlled/s01/api/commands/review-work-items/{work_item_id}/reveal-field-observation"]["post"]["requestBody"]["content"]["application/json"];
    type CorrectBody = paths["/controlled/s01/api/commands/review-work-items/{work_item_id}/correct-field-observation"]["post"]["requestBody"]["content"]["application/json"];
    expectTypeOf<RevealCommand>().toEqualTypeOf<RevealBody>();
    expectTypeOf<CorrectionCommand>().toEqualTypeOf<CorrectBody>();
    expectTypeOf<keyof RevealBody>().toEqualTypeOf<
      "application_id" | "observation_id" | "expected_fence" | "expected_context" | "idempotency_key"
    >();
    expectTypeOf<keyof CorrectBody>().toEqualTypeOf<
      "application_id" | "expected_fence" | "expected_context" | "idempotency_key" | "correction"
    >();
  });

  it("closes the correction reason and schema version as generated literals", () => {
    type Reason = CorrectionCommand["correction"]["reason_code"];
    type Version = CorrectionCommand["correction"]["schema_version"];
    expectTypeOf<Reason>().toEqualTypeOf<
      "SOURCE_VALUE_MISREAD" | "SOURCE_VALUE_MISSING"
    >();
    expectTypeOf<Version>().toEqualTypeOf<"field-observation-correction/1">();
  });
});

async function settleWithTimers(check: () => boolean): Promise<void> {
  for (let index = 0; index < 60 && !check(); index += 1) {
    await vi.advanceTimersByTimeAsync(500);
    await Promise.resolve();
  }
  expect(check()).toBe(true);
}

describe("endpoint- and status-specific GET retry policy (P5)", () => {
  it("never retries an existence-hiding 404 (exactly one request)", async () => {
    let requests = 0;
    fetchRouter({
      [`GET ${WORK_PATH}`]: () => {
        requests += 1;
        return new Response(
          JSON.stringify({ detail: { error: "S07_NOT_FOUND" } }),
          { status: 404, headers: { "Content-Type": "application/json" } },
        );
      },
    });
    const { result } = renderHook(() => useRecoveryWork(WORK_ID), {
      wrapper: wrap(createQueryClient()),
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(requests).toBe(1);
  });

  it("retries only transient 503 responses and then fails (three requests)", async () => {
    vi.useFakeTimers();
    try {
      let requests = 0;
      fetchRouter({
        [`GET ${QUEUE_PATH}`]: () => {
          requests += 1;
          return new Response(
            JSON.stringify({ detail: { error: "S01_UNAVAILABLE" } }),
            { status: 503, headers: { "Content-Type": "application/json" } },
          );
        },
      });
      const client = createQueryClient();
      const { result } = renderHook(() => useQueue(), {
        wrapper: wrap(client),
      });
      await settleWithTimers(() => result.current.isError);
      await vi.advanceTimersByTimeAsync(20_000);
      expect(requests).toBe(3);
    } finally {
      vi.useRealTimers();
    }
  });

  it("retries transport-level failures but not 403 scope rejections", async () => {
    vi.useFakeTimers();
    try {
      let transportRequests = 0;
      fetchRouter({
        [`GET ${ROUTE_PATH}`]: () => {
          transportRequests += 1;
          return Promise.reject(new TypeError("fetch failed: connection reset"));
        },
      });
      const transportClient = createQueryClient();
      const { result } = renderHook(() => useCurrentRoute(APP_ID), {
        wrapper: wrap(transportClient),
      });
      await settleWithTimers(() => result.current.isError);
      await vi.advanceTimersByTimeAsync(20_000);
      expect(transportRequests).toBe(3);

      let scopeRequests = 0;
      fetchRouter({
        [`GET ${ROUTE_PATH}`]: () => {
          scopeRequests += 1;
          return new Response(
            JSON.stringify({ detail: { error: "S01_FORBIDDEN" } }),
            { status: 403, headers: { "Content-Type": "application/json" } },
          );
        },
      });
      const scopeClient = createQueryClient();
      const second = renderHook(() => useCurrentRoute(APP_ID), {
        wrapper: wrap(scopeClient),
      });
      await settleWithTimers(() => second.result.current.isError);
      await vi.advanceTimersByTimeAsync(20_000);
      expect(scopeRequests).toBe(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not retry a successful 200 response", async () => {
    let requests = 0;
    fetchRouter({
      [`GET ${WORK_PATH}`]: () => {
        requests += 1;
        return new Response(JSON.stringify(workPayload()), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      },
    });
    const { result } = renderHook(() => useRecoveryWork(WORK_ID), {
      wrapper: wrap(createQueryClient()),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(requests).toBe(1);
  });
});

describe("manual-review mutations never retry and invalidate the S01 cache (T02)", () => {
  const CLAIM_PATH =
    "/controlled/s01/api/commands/review-work-items/work_t02hooks1234567890abcdef/claim";
  const SUBMIT_PATH =
    "/controlled/s01/api/commands/review-work-items/work_t02hooks1234567890abcdef/submit";

  it("fires exactly one claim POST and invalidates the S01 queries on success", async () => {
    let claimPosts = 0;
    let queueRequests = 0;
    const router = fetchRouter({
      [`POST ${CLAIM_PATH}`]: () => {
        claimPosts += 1;
        return new Response(
          JSON.stringify({
            status: "claimed",
            application_id: "app_t02hooks",
            work_item_id: "work_t02hooks1234567890abcdef",
            claim_subject: "t02-reviewer",
            claim_fence: 1,
            claim_expires_at: 1786000000,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
      "GET /controlled/s01/api/queries/queue": () => {
        queueRequests += 1;
        return new Response(
          JSON.stringify({
            items: [],
            recovery_items: [],
            projection_watermark: 0,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
    });
    const client = createQueryClient();
    const { result } = renderHook(
      () => ({
        claim: useClaimWorkItem("work_t02hooks1234567890abcdef"),
        queue: useQueue(),
      }),
      { wrapper: wrap(client) },
    );
    result.current.claim.mutate({
      expected_context: {
        lifecycle_revision: 6,
        evidence_revision: 1,
        run_id: "run_t02hooks",
        projection_watermark: 1,
        current_context: "ctx",
      },
    });
    await waitFor(() => expect(result.current.claim.isSuccess).toBe(true));
    expect(claimPosts).toBe(1);
    expect(router.calls.filter((call) => call.method === "POST")).toHaveLength(1);
    // The accepted claim invalidates the server-owned S01 queue query.
    await waitFor(() => expect(queueRequests).toBeGreaterThan(0));
  });

  it("never retries a rejected submit POST (retry: false)", async () => {
    let submitPosts = 0;
    fetchRouter({
      [`POST ${SUBMIT_PATH}`]: () => {
        submitPosts += 1;
        return new Response(
          JSON.stringify({
            detail: { error: "S03_STALE", reason_code: "STALE_WORK_ITEM_CLAIM" },
          }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        );
      },
    });
    const { result } = renderHook(
      () => useSubmitVerification("work_t02hooks1234567890abcdef"),
      { wrapper: wrap(createQueryClient()) },
    );
    result.current.mutate({
      expected_fence: 1,
      expected_context: {
        lifecycle_revision: 6,
        evidence_revision: 1,
        run_id: "run_t02hooks",
        projection_watermark: 1,
        current_context: "ctx",
      },
      idempotency_key: "t02-hooks-key",
      verification: {
        schema_version: "human-decision/1",
        outcome: "confirmed",
        reason_code: "HUMAN_REVIEW_COMPLETED",
        finding_decisions: [],
      },
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(submitPosts).toBe(1);
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(submitPosts).toBe(1);
  });
});

describe("structured rejection classifier (FIX-3)", () => {
  const recoverySet: ReadonlySet<number> = new Set([409]);

  it("treats 413 as a definitive pre-command rejection", () => {
    const error = new HttpError(413, { error: "S03_COMMAND_TOO_LARGE" });
    expect(isDefinitiveRejection(error)).toBe(true);
  });

  it("treats a structured S03 503 as definitive for the review panel", () => {
    for (const code of ["S03_STOPPED", "S03_UNAVAILABLE"]) {
      const error = new HttpError(503, {
        error: code,
        reason_code: "AUDIT_UNAVAILABLE",
      });
      expect(isDefinitiveRejection(error)).toBe(true);
    }
  });

  it("keeps a generic or non-S03 503 transport-unknown", () => {
    const generic = new HttpError(503, { message: "upstream" });
    expect(isDefinitiveRejection(generic)).toBe(false);
    const nonJson = new HttpError(503, null);
    expect(isDefinitiveRejection(nonJson)).toBe(false);
    const intermediary = new HttpError(503, { error: "S07_UNAVAILABLE" });
    expect(isDefinitiveRejection(intermediary)).toBe(false);
  });

  it("keeps the recovery 409-only policy unchanged", () => {
    const structured503 = new HttpError(503, { error: "S03_UNAVAILABLE" });
    expect(isDefinitiveRejection(structured503, recoverySet)).toBe(false);
    const stale = new HttpError(409, { error: "S03_STALE" });
    expect(isDefinitiveRejection(stale, recoverySet)).toBe(true);
  });

  it("still classifies 404/409/422 as definitive for the review panel", () => {
    for (const [status, code] of [
      [404, "S03_NOT_FOUND"],
      [409, "S03_STALE"],
      [422, "S03_INVALID_COMMAND"],
    ] as const) {
      const error = new HttpError(status, { error: code });
      expect(isDefinitiveRejection(error)).toBe(true);
    }
  });
});

const T03_REVEAL_PATH =
  "/controlled/s01/api/commands/review-work-items/recovery_work_t01retry1234567890abcdef/reveal-field-observation";
const T03_CORRECT_PATH =
  "/controlled/s01/api/commands/review-work-items/recovery_work_t01retry1234567890abcdef/correct-field-observation";

function routePayload(overrides: Record<string, unknown> = {}) {
  return {
    schema_version: "s04-current-route/1",
    application_id: APP_ID,
    phase: "Auto Complete",
    route: "auto_complete",
    current_run_id: "run_succ",
    cycle: 2,
    lifecycle_revision: 7,
    evidence_revision: 2,
    evidence_snapshot_id: "snapshot_succ",
    evidence_snapshot_digest: "a".repeat(64),
    release_id: "auto_lease@1.9.0",
    release_digest: "b".repeat(64),
    checker_build: "s01-target-checker/7",
    currentness_reason: "CURRENT_CONTEXT_MATCH",
    completion_basis: null,
    exception_id: null,
    exception_decision_id: null,
    exception_expires_at: null,
    failure: null,
    ...overrides,
  };
}

function historyPayload(overrides: Record<string, unknown> = {}) {
  const run = {
    run_id: "run_succ",
    status: "complete",
    authority_digest: "c".repeat(64),
    current: true,
    currentness_reason: "CURRENT_CONTEXT_MATCH",
    cycle: 2,
    lifecycle_revision: 7,
    evidence_revision: 2,
    evidence_snapshot_id: "snapshot_succ",
    evidence_snapshot_digest: "a".repeat(64),
    release_id: "auto_lease@1.9.0",
    release_digest: "b".repeat(64),
    checker_build: "s01-target-checker/7",
    finding_ids: [],
    cas_mismatches: [],
    selected_observation_ids: [],
    decision_ids: [],
    exception_ids: [],
    applicable_decision_ids: [],
    applicable_exception_ids: [],
    invalidated_decision_ids: [],
    invalidated_exception_ids: [],
  };
  return {
    schema_version: "s04-application-history/1",
    application_id: APP_ID,
    current_run_id: "run_succ",
    runs: [
      { ...run, run_id: "run_old", current: false, currentness_reason: "SUPERSEDED_BY_EVIDENCE_REVISION", evidence_revision: 1 },
      run,
    ],
    corrections: [],
    business_exceptions: [],
    attachment_versions: [],
    ...overrides,
  };
}

describe("correction convergence predicate (T03)", () => {
  it("refuses convergence without an authoritative route or history", () => {
    expect(correctionConverged(undefined, undefined, 2)).toBe(false);
    expect(correctionConverged(routePayload(), undefined, 2)).toBe(false);
    expect(correctionConverged(undefined, historyPayload(), 2)).toBe(false);
  });

  it("refuses convergence before the accepted evidence revision is server-current", () => {
    expect(
      correctionConverged(
        routePayload({ evidence_revision: 1 }),
        historyPayload(),
        2,
      ),
    ).toBe(false);
  });

  it("refuses convergence while the route has no server-current run", () => {
    expect(
      correctionConverged(routePayload({ current_run_id: null }), historyPayload(), 2),
    ).toBe(false);
  });

  it("requires exactly one server-current run matching the route run", () => {
    const runs = historyPayload().runs;
    const noCurrent = historyPayload({
      runs: runs.map((run) => ({ ...run, current: false })),
    });
    expect(correctionConverged(routePayload(), noCurrent, 2)).toBe(false);
    const twoCurrent = historyPayload({
      runs: runs.map((run) => ({ ...run, current: true })),
    });
    expect(correctionConverged(routePayload(), twoCurrent, 2)).toBe(false);
    const mismatched = historyPayload({
      runs: [{ ...runs[0], run_id: "run_other", current: true }],
    });
    expect(correctionConverged(routePayload(), mismatched, 2)).toBe(false);
  });

  it("converges only on exact agreement between route and history", () => {
    expect(correctionConverged(routePayload(), historyPayload(), 2)).toBe(true);
    expect(correctionConverged(routePayload(), historyPayload(), 3)).toBe(false);
  });

  it("requires the route and history to name the same server-current run", () => {
    const disagreeing = historyPayload({ current_run_id: "run_other" });
    expect(correctionConverged(routePayload(), disagreeing, 2)).toBe(false);
  });

  it("requires the current run's evidence revision to reach the accepted revision", () => {
    // The route already reports the successor revision while history still
    // marks the old run current at the old revision: the skewed projection
    // must stay waiting, never converge.
    const runs = historyPayload().runs;
    const skewed = historyPayload({
      current_run_id: "run_old",
      runs: [
        { ...runs[0], run_id: "run_old", current: true, evidence_revision: 1 },
        { ...runs[1], run_id: "run_succ", current: false },
      ],
    });
    expect(
      correctionConverged(
        routePayload({ current_run_id: "run_old", evidence_revision: 2 }),
        skewed,
        2,
      ),
    ).toBe(false);
    const exact = historyPayload({
      runs: [
        { ...runs[0], current: false },
        { ...runs[1], current: true, evidence_revision: 2 },
      ],
    });
    expect(correctionConverged(routePayload(), exact, 2)).toBe(true);
  });
});

describe("correction convergence polling (T03)", () => {
  // refetchQueries uses cancelRefetch, so an in-flight mount route fetch may
  // be replaced by the first poll's refetch and the route request count is
  // racy.  The history fetch is never cancelled (the route refetch awaits
  // before the history refetch starts), so history request N + 1 is exactly
  // poll N and all polling assertions are pinned to the history count.
  it("polls only the authoritative reads and stops at exact convergence", async () => {
    vi.useFakeTimers();
    try {
      let routeRequests = 0;
      let historyRequests = 0;
      const { jsonResponse } = fetchRouter({
        [`GET ${ROUTE_PATH}`]: () => {
          routeRequests += 1;
          return jsonResponse(
            routePayload({
              // Poll 1 always precedes any second history request; poll 2
              // observes the poll-1 history refetch and must see the successor.
              current_run_id: historyRequests >= 2 ? "run_succ" : "run_old",
            }),
          );
        },
        [`GET ${HISTORY_PATH}`]: () => {
          historyRequests += 1;
          const runs = historyPayload().runs;
          return jsonResponse(
            historyPayload({
              runs:
                historyRequests >= 3
                  ? runs
                  : runs.map((run) => ({ ...run, current: false })),
            }),
          );
        },
      });
      const client = createQueryClient();
      const { result } = renderHook(
        () => ({
          route: useCurrentRoute(APP_ID),
          history: useApplicationHistory(APP_ID),
          converge: useCorrectionConvergence(APP_ID, 2),
        }),
        { wrapper: wrap(client) },
      );
      // The effect reports waiting immediately (before the first refetch).
      await settleWithTimers(() => result.current.converge === "waiting");
      // Poll 1 (history request 2): not converged, so the 1.5s timer runs.
      await settleWithTimers(() => historyRequests >= 2);
      // Poll 2 (history request 3) observes convergence and must stop polling.
      await settleWithTimers(
        () => historyRequests >= 3 && result.current.converge === "converged",
      );
      const routeAtConvergence = routeRequests;
      await vi.advanceTimersByTimeAsync(20_000);
      expect(historyRequests).toBe(3);
      expect(routeRequests).toBe(routeAtConvergence);
    } finally {
      vi.useRealTimers();
    }
  });

  it("stops polling on a definitive terminal rejection", async () => {
    vi.useFakeTimers();
    try {
      let routeRequests = 0;
      let historyRequests = 0;
      const { jsonResponse } = fetchRouter({
        [`GET ${ROUTE_PATH}`]: () => {
          routeRequests += 1;
          return jsonResponse({ detail: { error: "S03_NOT_FOUND" } }, 404);
        },
        [`GET ${HISTORY_PATH}`]: () => {
          historyRequests += 1;
          return jsonResponse(historyPayload());
        },
      });
      const client = createQueryClient();
      const { result } = renderHook(
        () => ({
          route: useCurrentRoute(APP_ID),
          history: useApplicationHistory(APP_ID),
          converge: useCorrectionConvergence(APP_ID, 2),
        }),
        { wrapper: wrap(client) },
      );
      // Poll 1 ends on the definitive route error: the outcome is the
      // sanitized terminal state, never a claimed elapsed timeout.
      await settleWithTimers(
        () => historyRequests >= 2 && result.current.converge === "terminal",
      );
      const routeAtRejection = routeRequests;
      await vi.advanceTimersByTimeAsync(20_000);
      expect(historyRequests).toBe(2);
      expect(routeRequests).toBe(routeAtRejection);
    } finally {
      vi.useRealTimers();
    }
  });

  it("gives a current definitive error precedence over retained cached data", async () => {
    let failRoute = false;
    const currentHistory = historyPayload();
    const waitingHistory = historyPayload({
      runs: currentHistory.runs.map((run) => ({ ...run, current: false })),
    });
    const { jsonResponse } = fetchRouter({
      [`GET ${ROUTE_PATH}`]: () =>
        failRoute
          ? jsonResponse({ detail: { error: "S03_NOT_FOUND" } }, 404)
          : jsonResponse(routePayload()),
      [`GET ${HISTORY_PATH}`]: () =>
        jsonResponse(failRoute ? currentHistory : waitingHistory),
    });
    const client = createQueryClient();
    const { result, rerender } = renderHook(
      ({ acceptedRevision }: { acceptedRevision: number | null }) => ({
        route: useCurrentRoute(APP_ID),
        history: useApplicationHistory(APP_ID),
        converge: useCorrectionConvergence(APP_ID, acceptedRevision),
      }),
      {
        wrapper: wrap(client),
        initialProps: { acceptedRevision: null as number | null },
      },
    );
    await waitFor(() => expect(result.current.route.isSuccess).toBe(true));
    await waitFor(() => expect(result.current.history.isSuccess).toBe(true));

    // The route's last successful value looks converged, but its current
    // refetch is now a definitive 404 while history catches up.  The current
    // error must win over retained cache data.
    failRoute = true;
    rerender({ acceptedRevision: 2 });
    await waitFor(() => expect(result.current.converge).toBe("terminal"));
    expect(result.current.route.isError).toBe(true);
  });

  it("waits through a transient route error instead of converging from retained data", async () => {
    vi.useFakeTimers();
    try {
      let routeUnavailable = false;
      let historyCaughtUp = false;
      const currentHistory = historyPayload();
      const waitingHistory = historyPayload({
        runs: currentHistory.runs.map((run) => ({ ...run, current: false })),
      });
      const { jsonResponse } = fetchRouter({
        [`GET ${ROUTE_PATH}`]: () =>
          routeUnavailable
            ? jsonResponse({ detail: { error: "UPSTREAM_UNAVAILABLE" } }, 503)
            : jsonResponse(routePayload()),
        [`GET ${HISTORY_PATH}`]: () =>
          jsonResponse(historyCaughtUp ? currentHistory : waitingHistory),
      });
      const client = createQueryClient();
      const { result, rerender } = renderHook(
        ({ acceptedRevision }: { acceptedRevision: number | null }) => ({
          route: useCurrentRoute(APP_ID),
          history: useApplicationHistory(APP_ID),
          converge: useCorrectionConvergence(APP_ID, acceptedRevision),
        }),
        {
          wrapper: wrap(client),
          initialProps: { acceptedRevision: null as number | null },
        },
      );
      await settleWithTimers(
        () => result.current.route.isSuccess && result.current.history.isSuccess,
      );

      routeUnavailable = true;
      historyCaughtUp = true;
      rerender({ acceptedRevision: 2 });
      await settleWithTimers(
        () =>
          result.current.route.isError &&
          result.current.history.data?.current_run_id === "run_succ",
      );
      expect(result.current.converge).toBe("waiting");

      routeUnavailable = false;
      await settleWithTimers(() => result.current.converge === "converged");
    } finally {
      vi.useRealTimers();
    }
  });

  it("cancels the poll timer on unmount", async () => {
    vi.useFakeTimers();
    try {
      let routeRequests = 0;
      let historyRequests = 0;
      const { jsonResponse } = fetchRouter({
        [`GET ${ROUTE_PATH}`]: () => {
          routeRequests += 1;
          return jsonResponse(routePayload({ current_run_id: "run_old" }));
        },
        [`GET ${HISTORY_PATH}`]: () => {
          historyRequests += 1;
          return jsonResponse(
            historyPayload({
              runs: historyPayload().runs.map((run) => ({ ...run, current: false })),
            }),
          );
        },
      });
      const client = createQueryClient();
      const { unmount } = renderHook(
        () => ({
          route: useCurrentRoute(APP_ID),
          history: useApplicationHistory(APP_ID),
          converge: useCorrectionConvergence(APP_ID, 2),
        }),
        { wrapper: wrap(client) },
      );
      // Poll 1 is not converged and schedules the next poll.
      await settleWithTimers(() => historyRequests >= 2);
      unmount();
      await vi.advanceTimersByTimeAsync(20_000);
      expect(historyRequests).toBe(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("surfaces an explicit timed_out when the bounded poll ends without convergence", async () => {
    vi.useFakeTimers();
    try {
      let routeRequests = 0;
      let historyRequests = 0;
      const { jsonResponse } = fetchRouter({
        [`GET ${ROUTE_PATH}`]: () => {
          routeRequests += 1;
          return jsonResponse(routePayload({ current_run_id: "run_old" }));
        },
        [`GET ${HISTORY_PATH}`]: () => {
          historyRequests += 1;
          return jsonResponse(
            historyPayload({
              runs: historyPayload().runs.map((run) => ({ ...run, current: false })),
            }),
          );
        },
      });
      const client = createQueryClient();
      const { result } = renderHook(
        () => ({
          route: useCurrentRoute(APP_ID),
          history: useApplicationHistory(APP_ID),
          converge: useCorrectionConvergence(APP_ID, 2),
        }),
        { wrapper: wrap(client) },
      );
      // The successor never becomes current: every 1.5s poll refetches and
      // rechecks until the 240-attempt safety ceiling turns the outcome into
      // an explicit timed_out (never a converged claim).
      await vi.advanceTimersByTimeAsync(400_000);
      await settleWithTimers(() => result.current.converge === "timed_out");
      const routesAtCeiling = routeRequests;
      const historiesAtCeiling = historyRequests;
      await vi.advanceTimersByTimeAsync(20_000);
      expect(routeRequests).toBe(routesAtCeiling);
      expect(historyRequests).toBe(historiesAtCeiling);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("reveal and correction mutations (T03)", () => {
  const revealCommand: RevealCommand = {
    application_id: APP_ID,
    observation_id: "observation_t03hook",
    expected_fence: 1,
    expected_context: {
      lifecycle_revision: 6,
      evidence_revision: 1,
      run_id: "run_t03hook",
      projection_watermark: 1,
      current_context: "ctx",
    },
    idempotency_key: "t03-reveal-key",
  };

  const correctionCommand: CorrectionCommand = {
    application_id: APP_ID,
    expected_fence: 1,
    expected_context: {
      lifecycle_revision: 6,
      evidence_revision: 1,
      run_id: "run_t03hook",
      projection_watermark: 1,
      current_context: "ctx",
    },
    idempotency_key: "t03-correct-key",
    correction: {
      schema_version: "field-observation-correction/1",
      finding_id: "finding_t03hook",
      observation_id: "observation_t03hook",
      document_id: "reg",
      document_role: "机动车登记证书",
      field: "engine_no",
      raw: RESTRICTED_CORRECTION_RAW,
      source_location: {
        source_sha256: "d".repeat(64),
        source_page: 1,
        source_region: "region:1",
      },
      reason_code: "SOURCE_VALUE_MISREAD",
    },
  };

  function revealResult() {
    return {
      status: "revealed",
      replayed: false,
      application_id: APP_ID,
      work_item_id: WORK_ID,
      observation_id: "observation_t03hook",
      source_location: {
        source_sha256: "d".repeat(64),
        source_page: 1,
        source_region: "region:1",
      },
      source_text: RESTRICTED_SOURCE_TEXT,
      revealed_at: 1786000000,
    };
  }

  it("reveals with exactly one POST and never invalidates the S01 cache", async () => {
    let revealPosts = 0;
    let queueRequests = 0;
    const { jsonResponse } = fetchRouter({
      [`POST ${T03_REVEAL_PATH}`]: () => {
        revealPosts += 1;
        return jsonResponse(revealResult());
      },
      "GET /controlled/s01/api/queries/queue": () => {
        queueRequests += 1;
        return jsonResponse({
          items: [],
          recovery_items: [],
          projection_watermark: 0,
        });
      },
    });
    const client = createQueryClient();
    const { result } = renderHook(
      () => ({
        reveal: useRevealFieldObservation(WORK_ID),
        queue: useQueue(),
      }),
      { wrapper: wrap(client) },
    );
    result.current.reveal.mutate(revealCommand);
    await waitFor(() => expect(result.current.reveal.isSuccess).toBe(true));
    expect(revealPosts).toBe(1);
    expect(restrictedDigest(result.current.reveal.data?.source_text)).toBe(
      restrictedDigest(RESTRICTED_SOURCE_TEXT),
    );
    // The restricted reveal must never invalidate the server-owned S01 cache.
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(queueRequests).toBe(1);
  });

  it("corrects with exactly one POST and invalidates the S01 cache on acceptance", async () => {
    let correctPosts = 0;
    let queueRequests = 0;
    const { jsonResponse } = fetchRouter({
      [`POST ${T03_CORRECT_PATH}`]: () => {
        correctPosts += 1;
        return jsonResponse({
          status: "accepted",
          replayed: false,
          application_id: APP_ID,
          work_item_id: WORK_ID,
          correction_id: "correction_t03hook",
          observation_id: "observation_t03hook",
          invalidated_run_id: "run_t03hook",
          job_id: "job_t03hook",
          phase: "Auto Complete",
          route: "auto_complete",
          lifecycle_revision: 7,
          evidence_revision: 2,
          invalidated_exception_ids: [],
        });
      },
      "GET /controlled/s01/api/queries/queue": () => {
        queueRequests += 1;
        return jsonResponse({
          items: [],
          recovery_items: [],
          projection_watermark: 0,
        });
      },
    });
    const client = createQueryClient();
    const { result } = renderHook(
      () => ({
        correct: useCorrectFieldObservation(WORK_ID),
        queue: useQueue(),
      }),
      { wrapper: wrap(client) },
    );
    result.current.correct.mutate(correctionCommand);
    await waitFor(() => expect(result.current.correct.isSuccess).toBe(true));
    expect(correctPosts).toBe(1);
    expect(result.current.correct.data?.evidence_revision).toBe(2);
    // Acceptance invalidates the server-owned S01 queries: the queue refetches.
    await waitFor(() => expect(queueRequests).toBeGreaterThan(1));
  });

  it("does not retain the correction raw in the MutationCache after unmount", async () => {
    const { jsonResponse } = fetchRouter({
      [`POST ${T03_CORRECT_PATH}`]: () =>
        jsonResponse({
          status: "accepted",
          replayed: false,
          application_id: APP_ID,
          work_item_id: WORK_ID,
          correction_id: "correction_t03hook",
          observation_id: "observation_t03hook",
          invalidated_run_id: "run_t03hook",
          job_id: "job_t03hook",
          phase: "Auto Complete",
          route: "auto_complete",
          lifecycle_revision: 7,
          evidence_revision: 2,
          invalidated_exception_ids: [],
        }),
    });
    const client = createQueryClient();
    const holder = renderHook(() => useCorrectFieldObservation(WORK_ID), {
      wrapper: wrap(client),
    });
    // The exact raw-bearing command travels through the mutation cache only
    // for the shortest allowed lifetime (gcTime 0): while mounted it must be
    // visible, and after unmount it must be garbage collected.
    holder.result.current.mutate(correctionCommand);
    await waitFor(() => expect(holder.result.current.isSuccess).toBe(true));
    const rawRetained = client
      .getMutationCache()
      .getAll()
      .some((mutation) =>
        JSON.stringify(mutation.state.variables ?? {}).includes(
          RESTRICTED_CORRECTION_RAW,
        ),
      );
    expect(rawRetained).toBe(true);
    holder.unmount();
    await new Promise((resolve) => setTimeout(resolve, 50));
    const afterUnmount = client
      .getMutationCache()
      .getAll()
      .some((mutation) =>
        JSON.stringify(mutation.state.variables ?? {}).includes(
          RESTRICTED_CORRECTION_RAW,
        ),
      );
    expect(afterUnmount).toBe(false);
  });

  it("discards a late reveal response after unmount without a retry", async () => {
    let revealPosts = 0;
    let resolveReveal: ((response: Response) => void) | undefined;
    fetchRouter({
      [`POST ${T03_REVEAL_PATH}`]: () => {
        revealPosts += 1;
        return new Promise<Response>((resolve) => {
          resolveReveal = resolve;
        });
      },
    });
    const { result, unmount } = renderHook(
      () => useRevealFieldObservation(WORK_ID),
      { wrapper: wrap(createQueryClient()) },
    );
    result.current.mutate(revealCommand);
    await waitFor(() => expect(result.current.isPending).toBe(true));
    unmount();
    await act(async () => {
      resolveReveal?.(
        new Response(JSON.stringify(revealResult()), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    });
    expect(revealPosts).toBe(1);
  });
});
