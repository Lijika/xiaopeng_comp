import { renderHook, waitFor } from "@testing-library/react";
import { describe, expect, expectTypeOf, it, vi } from "vitest";
import type { ReactNode } from "react";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { paths } from "../generated/api";
import { HttpError, isDefinitiveRejection } from "./client";
import {
  useClaimWorkItem,
  useCurrentRoute,
  useQueue,
  useRecoveryWork,
  useSubmitVerification,
  type ClaimCommand,
  type FencedCommand,
  type SubmitCommand,
  type VerifyRecoveryCommand,
} from "./hooks";
import { createQueryClient, fetchRouter } from "../test-utils";

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
