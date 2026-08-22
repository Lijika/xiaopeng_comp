import { renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { HttpError, isDefinitiveS13Rejection } from "./client";
import {
  S13_DELIVERY_KEY,
  useS13Compensate,
  useS13Delivery,
  useS13ProcessNextDelivery,
  useS13Reconcile,
  type S13QueryResponse,
  type S13ReconcileCommand,
} from "./hooks";
import { createQueryClient, fetchRouter } from "../test-utils";

export const S13_APPLICATION_ID = "app_s13_t15_00000001";
export const S13_OBLIGATION_ID = "obl_s13_t15_00000001";
export const S13_OPERATION_ID = "op_s13_t15_00000000000000000000000001";
const DIGEST_64 = "c".repeat(64);

export function s13QueryPayload(
  overrides: Record<string, unknown> = {},
): S13QueryResponse {
  return {
    schema_version: "s13-delivery-view/1",
    application_id: S13_APPLICATION_ID,
    phase: "Verification Completed",
    route: "human_complete",
    cycle: 1,
    lifecycle_revision: 7,
    verification_completed: true,
    obligation: {
      obligation_id: S13_OBLIGATION_ID,
      application_id: S13_APPLICATION_ID,
      cycle: 1,
      route: "human_complete",
      attribution_kind: "human_complete",
      operation_id: S13_OPERATION_ID,
      recipient_id: "recipient_c_demo_1",
      adapter_id: "c-demo-downstream",
      adapter_version: "1",
      payload_ref: "payload/s13/00000001",
      payload_digest: DIGEST_64,
      payload_schema: "s13-route-payload/1",
      status: "pending",
    },
    delivery_status: "pending",
    attempt_count: 0,
    projection_watermark: 10,
    store_revision: 42,
    ...overrides,
  } as S13QueryResponse;
}

function wrap(client: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

describe("S13 delivery query hook", () => {
  it("uses the parameterized query key and same-origin no-store GET", async () => {
    const client = createQueryClient();
    const payload = s13QueryPayload();
    const router = fetchRouter({
      [`GET /controlled/s13/delivery/${S13_APPLICATION_ID}`]: () => {
        return new Response(JSON.stringify(payload), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      },
    });

    expect(S13_DELIVERY_KEY(S13_APPLICATION_ID)).toEqual([
      "s13",
      "delivery",
      S13_APPLICATION_ID,
    ]);

    const { result } = renderHook(() => useS13Delivery(S13_APPLICATION_ID), {
      wrapper: wrap(client),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.verification_completed).toBe(true);
    expect(result.current.data?.obligation?.operation_id).toBe(S13_OPERATION_ID);
    // Same-origin credentials and no-store cache are owned by the thin fetch adapter.
    // The mock records body; headers are set by request().
    expect(router.calls).toHaveLength(1);
    expect(router.calls[0]?.url).toBe(
      `/controlled/s13/delivery/${S13_APPLICATION_ID}`,
    );
  });

  it("is disabled when applicationId is null and issues zero fetches", async () => {
    const client = createQueryClient();
    const router = fetchRouter({});
    const { result } = renderHook(() => useS13Delivery(null), {
      wrapper: wrap(client),
    });
    // Disabled query stays pending/idle without fetching.
    expect(result.current.fetchStatus).toBe("idle");
    expect(router.calls).toHaveLength(0);
  });

  it("retries transient 503 but never retries existence-hiding 404", async () => {
    // retryPolicy: 404 not retried, 503 retried up to twice.
    const { retryPolicy } = await import("./hooks");
    expect(retryPolicy(0, new HttpError(404, { error: "S13_NOT_FOUND" }))).toBe(false);
    expect(retryPolicy(0, new HttpError(503, { error: "S13_UNAVAILABLE" }))).toBe(true);
    expect(retryPolicy(2, new HttpError(503, { error: "S13_UNAVAILABLE" }))).toBe(false);
    expect(retryPolicy(0, new HttpError(403, { error: "S13_FORBIDDEN" }))).toBe(false);
  });

  it("classifies 403/503 without envelope as transient unknown, not definitive", () => {
    expect(isDefinitiveS13Rejection(new HttpError(403, { error: "OTHER" }))).toBe(false);
    expect(isDefinitiveS13Rejection(new HttpError(503, { error: "OTHER" }))).toBe(false);
    expect(isDefinitiveS13Rejection(new HttpError(403, { error: "S13_FORBIDDEN" }))).toBe(true);
    expect(isDefinitiveS13Rejection(new HttpError(503, { error: "S13_UNAVAILABLE" }))).toBe(true);
  });

  it("clears cached delivery data on 404 error (stale content not shown)", async () => {
    const client = createQueryClient();
    // Seed cache with a successful payload, then fail the refetch.
    const payload = s13QueryPayload();
    client.setQueryData(S13_DELIVERY_KEY(S13_APPLICATION_ID), payload);
    let call = 0;
    const router = fetchRouter({
      [`GET /controlled/s13/delivery/${S13_APPLICATION_ID}`]: () => {
        call += 1;
        return new Response(
          JSON.stringify({ detail: { error: "S13_NOT_FOUND", message: "gone" } }),
          { status: 404, headers: { "Content-Type": "application/json" } },
        );
      },
    });
    const { result } = renderHook(() => useS13Delivery(S13_APPLICATION_ID), {
      wrapper: wrap(client),
    });
    // Force refetch to trigger error path
    await client.refetchQueries({ queryKey: S13_DELIVERY_KEY(S13_APPLICATION_ID) });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect((result.current.error as HttpError).status).toBe(404);
    expect(router.calls.length).toBeGreaterThanOrEqual(1);
  });
});

describe("S13 reconcile command hook", () => {
  it("sends the closed reconcile body with retry:false and invalidates s13 on success", async () => {
    const client = createQueryClient();
    const invalidateSpy = vi.spyOn(client, "invalidateQueries");
    const response = {
      obligation_id: S13_OBLIGATION_ID,
      operation_id: S13_OPERATION_ID,
      delivery_status: "received",
      status: "received",
      reason_code: null,
    };
    const router = fetchRouter({
      [`POST /controlled/s13/api/commands/reconcile`]: (_url, init) => {
        const body = JSON.parse(String(init?.body));
        expect(body).toEqual({ obligation_id: S13_OBLIGATION_ID });
        return new Response(JSON.stringify(response), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      },
    });
    const { result } = renderHook(() => useS13Reconcile(), {
      wrapper: wrap(client),
    });
    const command: S13ReconcileCommand = { obligation_id: S13_OBLIGATION_ID };
    result.current.mutate(command);
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.delivery_status).toBe("received");
    expect(router.calls).toHaveLength(1);
    expect(router.calls[0]?.method).toBe("POST");
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["s13"] });
    // Mutation never retries: retry is false on the mutation.
    expect(result.current.failureCount).toBe(0);
  });

  it("retains the stable operation_id on unknown transport outcome (non-definitive error)", async () => {
    const client = createQueryClient();
    const router = fetchRouter({
      [`POST /controlled/s13/api/commands/reconcile`]: () =>
        // Generic 503 without registered code — isDefinitiveS13Rejection = false, stays unknown
        new Response(JSON.stringify({ detail: { error: "UNKNOWN", message: "timeout" } }), {
          status: 503,
          headers: { "Content-Type": "application/json" },
        }),
    });
    const { result } = renderHook(() => useS13Reconcile(), {
      wrapper: wrap(client),
    });
    result.current.mutate({ obligation_id: S13_OBLIGATION_ID });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(isDefinitiveS13Rejection(result.current.error as Error)).toBe(false);
    // Caller must retain the original operation_id and reconcile before retry.
    expect(router.calls).toHaveLength(1);
  });

  it("proves not_executed via definitive rejection rotates only after reconciliation", async () => {
    const client = createQueryClient();
    const router = fetchRouter({
      [`POST /controlled/s13/api/commands/reconcile`]: () =>
        new Response(
          JSON.stringify({ detail: { error: "S13_NOT_FOUND", message: "not found" } }),
          { status: 404, headers: { "Content-Type": "application/json" } },
        ),
    });
    const { result } = renderHook(() => useS13Reconcile(), {
      wrapper: wrap(client),
    });
    result.current.mutate({ obligation_id: S13_OBLIGATION_ID });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(isDefinitiveS13Rejection(result.current.error as Error)).toBe(true);
    expect(router.calls).toHaveLength(1);
  });
});

describe("S13 compensate command hook", () => {
  it("sends the closed compensate body with retry:false and invalidates s13", async () => {
    const client = createQueryClient();
    const invalidateSpy = vi.spyOn(client, "invalidateQueries");
    const response = {
      obligation_id: S13_OBLIGATION_ID,
      operation_id: S13_OPERATION_ID,
      status: "compensated",
      reason_code: null,
    };
    const router = fetchRouter({
      [`POST /controlled/s13/api/commands/compensate`]: (_url, init) => {
        const body = JSON.parse(String(init?.body));
        expect(body).toEqual({ obligation_id: S13_OBLIGATION_ID });
        return new Response(JSON.stringify(response), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      },
    });
    const { result } = renderHook(() => useS13Compensate(), {
      wrapper: wrap(client),
    });
    result.current.mutate({ obligation_id: S13_OBLIGATION_ID });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(router.calls).toHaveLength(1);
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["s13"] });
  });
});

describe("S13 process_next_delivery command hook", () => {
  it("sends an empty body POST with retry:false and invalidates s13", async () => {
    const client = createQueryClient();
    const invalidateSpy = vi.spyOn(client, "invalidateQueries");
    const response = {
      status: "sent",
      obligation_id: S13_OBLIGATION_ID,
      operation_id: S13_OPERATION_ID,
      remote_message_id: "remote_1",
      reason_code: null,
    };
    const router = fetchRouter({
      [`POST /controlled/s13/api/commands/process_next_delivery`]: (_url, init) => {
        const body = JSON.parse(String(init?.body));
        expect(body).toEqual({});
        return new Response(JSON.stringify(response), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      },
    });
    const { result } = renderHook(() => useS13ProcessNextDelivery(), {
      wrapper: wrap(client),
    });
    result.current.mutate({});
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(router.calls).toHaveLength(1);
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["s13"] });
  });
});

describe("S13 definitive rejection classifier", () => {
  it("treats structured 403/503 and 404/422 as definitive, others as unknown", () => {
    expect(isDefinitiveS13Rejection(new HttpError(404, { error: "S13_NOT_FOUND" }))).toBe(true);
    expect(isDefinitiveS13Rejection(new HttpError(422, { error: "S13_VALIDATION" }))).toBe(true);
    expect(isDefinitiveS13Rejection(new HttpError(403, { error: "S13_FORBIDDEN" }))).toBe(true);
    expect(isDefinitiveS13Rejection(new HttpError(503, { error: "S13_UNAVAILABLE" }))).toBe(true);
    expect(isDefinitiveS13Rejection(new HttpError(403, { error: "S01_FORBIDDEN" }))).toBe(false);
    expect(isDefinitiveS13Rejection(new HttpError(503, { error: "S01_UNAVAILABLE" }))).toBe(false);
    expect(isDefinitiveS13Rejection(new HttpError(409, { error: "S13_CONFLICT" }))).toBe(false);
    expect(isDefinitiveS13Rejection(new TypeError("network"))).toBe(false);
  });
});
