import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import {
  S17_RECEIPT_KEY,
  S17_REQUEST_KEY,
  useS17Access,
  useS17Approve,
  useS17Confirm,
  useS17Expire,
  useS17Preview,
  useS17Process,
} from "./hooks";
import { createQueryClient, fetchRouter } from "../test-utils";

function wrap(client: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

describe("S17 query keys", () => {
  it("separates export state and receipt state", () => {
    expect(S17_REQUEST_KEY("req")).toEqual(["s17", "exports", "req"]);
    expect(S17_RECEIPT_KEY("req")).toEqual(["s17", "exports", "req", "receipt"]);
  });
});

describe("S17 command hooks", () => {
  it("posts the requester-defined fixed request", async () => {
    const router = fetchRouter({
      "POST /controlled/s17/api/exports/preview": () =>
        router.jsonResponse({ status: "previewed", preview_digest: "a".repeat(64) }),
    });
    const { result } = renderHook(() => useS17Preview(), {
      wrapper: wrap(createQueryClient()),
    });
    result.current.mutate({
      purpose: "audit_response",
      fields: ["application_fingerprint"],
      artifacts: ["route_metadata"],
      recipient_id: "recipient-1",
      classification: "confidential",
      expiry: 1_800_003_600,
      scope_reference: "APP-REF-42",
      idempotency_key: "preview-key",
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(router.calls[0]?.body).toEqual({
      purpose: "audit_response",
      fields: ["application_fingerprint"],
      artifacts: ["route_metadata"],
      recipient_id: "recipient-1",
      classification: "confidential",
      expiry: 1_800_003_600,
      scope_reference: "APP-REF-42",
      idempotency_key: "preview-key",
    });
  });

  it("uses a dedicated approver header and keeps the token out of the body", async () => {
    const router = fetchRouter({
      "POST /controlled/s17/api/exports/req/approve": () =>
        router.jsonResponse({ status: "approved", request_id: "req" }),
    });
    const { result } = renderHook(() => useS17Approve(), {
      wrapper: wrap(createQueryClient()),
    });
    result.current.mutate({
      requestId: "req",
      preview_digest: "b".repeat(64),
      idempotency_key: "approve-key",
      approverToken: "approver-secret",
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const init = vi.mocked(fetch).mock.calls.at(-1)?.[1];
    expect(init?.headers).toMatchObject({ "X-S17-Approver-Token": "Bearer approver-secret" });
    expect(router.calls[0]?.body).toEqual({
      preview_digest: "b".repeat(64),
      idempotency_key: "approve-key",
    });
  });

  it("keeps delivery credentials ephemeral and sends server command headers", async () => {
    const router = fetchRouter({
      "POST /controlled/s17/api/exports/req/access": () =>
        router.jsonResponse({ status: "accessed", request_id: "req" }),
      "POST /controlled/s17/api/exports/req/confirm": () =>
        router.jsonResponse({ status: "confirmed", request_id: "req" }),
      "POST /controlled/s17/api/exports/req/expire": () =>
        router.jsonResponse({ status: "expired", request_id: "req" }),
      "POST /controlled/s17/api/process": () =>
        router.jsonResponse({ status: "delivered", request_id: "req" }),
    });
    const client = createQueryClient();
    const access = renderHook(() => useS17Access(), { wrapper: wrap(client) });
    access.result.current.mutate({ requestId: "req", token: "one-time-token", recipientToken: "recipient-secret" });
    await waitFor(() => expect(access.result.current.isSuccess).toBe(true));
    expect(router.calls[0]?.body).toEqual({ token: "one-time-token" });
    expect(vi.mocked(fetch).mock.calls[0]?.[1]).toMatchObject({
      headers: { Authorization: "Bearer recipient-secret" },
    });

    const confirm = renderHook(() => useS17Confirm(), { wrapper: wrap(client) });
    confirm.result.current.mutate({ requestId: "req", idempotency_key: "confirm-key" });
    await waitFor(() => expect(confirm.result.current.isSuccess).toBe(true));
    const confirmInit = vi.mocked(fetch).mock.calls.at(-1)?.[1];
    expect(confirmInit?.headers).toEqual({ "Idempotency-Key": "confirm-key" });

    const expire = renderHook(() => useS17Expire(), { wrapper: wrap(client) });
    expire.result.current.mutate({ requestId: "req", idempotency_key: "expire-key", workerToken: "worker-secret" });
    await waitFor(() => expect(expire.result.current.isSuccess).toBe(true));
    const expireInit = vi.mocked(fetch).mock.calls.at(-1)?.[1];
    expect(expireInit?.headers).toEqual({
      Authorization: "Bearer worker-secret",
      "Idempotency-Key": "expire-key",
    });

    const process = renderHook(() => useS17Process(), { wrapper: wrap(client) });
    process.result.current.mutate({ workerToken: "worker-secret" });
    await waitFor(() => expect(process.result.current.isSuccess).toBe(true));
    const processInit = vi.mocked(fetch).mock.calls.at(-1)?.[1];
    expect(processInit?.headers).toEqual({ Authorization: "Bearer worker-secret" });
  });
});
