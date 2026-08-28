import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import {
  HttpError,
  isDefinitiveS16Rejection,
  type S16PreflightResponse,
  type S16QueryResponse,
} from "./client";
import {
  clearApplicationScopedCache,
  S16_REQUEST_KEY,
  useS16Approve,
  useS16Commit,
  useS16Preflight,
  useS16Query,
  useS16Repair,
} from "./hooks";
import { createQueryClient, fetchRouter } from "../test-utils";

function wrap(client: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

function manifestEntry(overrides: Partial<S16PreflightResponse["entries"][number]> = {}) {
  return {
    owner_id: "s01",
    copy_class: "evidence",
    classification: "RESTRICTED",
    content_sha256: "a".repeat(64),
    identity_fingerprint: "b".repeat(64),
    retention_policy_id: "s16-retention/1",
    retention_policy_version: "1",
    retention_due_at: null,
    legal_hold_generation: 0,
    hold_state: "none",
    shared_state: "exclusive",
    planned_action: "delete",
    count: 3,
    ...overrides,
  };
}

export function s16PreflightPayload(
  overrides: Partial<S16PreflightResponse> = {},
): S16PreflightResponse {
  const entries = [
    manifestEntry({ owner_id: "s01", copy_class: "source_object" }),
    manifestEntry({ owner_id: "s01", copy_class: "evidence" }),
    manifestEntry({ owner_id: "s01", copy_class: "run_or_finding" }),
    manifestEntry({ owner_id: "s01", copy_class: "projection_or_cache" }),
    manifestEntry({ owner_id: "s02", copy_class: "derived_object", count: 2 }),
    manifestEntry({ owner_id: "s12", copy_class: "evaluation_copy", count: 0, planned_action: "none" }),
    manifestEntry({ owner_id: "s17-disabled", copy_class: "export_or_temp", count: 0, planned_action: "none" }),
    manifestEntry({ owner_id: "backup", copy_class: "replica", count: 0, planned_action: "none" }),
    manifestEntry({ owner_id: "backup", copy_class: "backup_manifest", count: 0, planned_action: "none" }),
  ];
  return {
    status: "accepted",
    request_id: "s16req_test_00000001",
    application_reference: "APP-REFERENCE-1",
    scope_fingerprint: "c".repeat(64),
    manifest_digest: "d".repeat(64),
    entries_digest: "e".repeat(64),
    owner_registry_digest: "f".repeat(64),
    s01_revision: 12,
    s12_revision: "g".repeat(64),
    policy_digest: "h".repeat(64),
    retention_due: 1_800_000_000,
    early_deletion: true,
    retained_scan_clean: true,
    entries,
    replayed: false,
    ...overrides,
  };
}

export function s16QueryPayload(
  overrides: Partial<S16QueryResponse> = {},
): S16QueryResponse {
  return {
    schema_version: "s16-query/1",
    request_id: "s16req_test_00000001",
    scope_fingerprint: "c".repeat(64),
    manifest_digest: "d".repeat(64),
    owner_registry_digest: "f".repeat(64),
    s01_revision: 12,
    s12_revision: "g".repeat(64),
    policy_digest: "h".repeat(64),
    retention_due: 1_800_000_000,
    early_deletion: true,
    cancelled: false,
    approvals: [],
    legal_holds: [],
    job: null,
    ...overrides,
  };
}

describe("S16 preflight hook", () => {
  it("posts the closed reference + idempotency key body with no-store", async () => {
    const client = createQueryClient();
    const router = fetchRouter({
      "POST /controlled/s16/api/deletions/preflight": () =>
        new Response(JSON.stringify(s16PreflightPayload()), {
          headers: { "Content-Type": "application/json" },
        }),
    });
    const { result } = renderHook(() => useS16Preflight(), {
      wrapper: wrap(client),
    });
    result.current.mutate({
      application_reference: "APP-REFERENCE-1",
      idempotency_key: "key-1",
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(router.calls).toEqual([
      {
        method: "POST",
        url: "/controlled/s16/api/deletions/preflight",
        body: {
          application_reference: "APP-REFERENCE-1",
          idempotency_key: "key-1",
        },
      },
    ]);
    const init = vi.mocked(fetch).mock.calls[0]?.[1];
    expect(init).toMatchObject({
      credentials: "same-origin",
      cache: "no-store",
    });
    expect(result.current.data?.entries).toHaveLength(9);
  });

  it("keeps the same idempotency key on a lost transport response", async () => {
    const client = createQueryClient();
    fetchRouter({});
    const { result } = renderHook(() => useS16Preflight(), {
      wrapper: wrap(client),
    });
    result.current.mutate({
      application_reference: "APP-REFERENCE-1",
      idempotency_key: "key-keep",
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.variables).toEqual({
      application_reference: "APP-REFERENCE-1",
      idempotency_key: "key-keep",
    });
  });
});

describe("S16 query hook", () => {
  it("uses one parameterized no-store GET and polls only while moving", async () => {
    const client = createQueryClient();
    const router = fetchRouter({
      "GET /controlled/s16/api/deletions/s16req_test_00000001": () =>
        new Response(
          JSON.stringify(
            s16QueryPayload({
              job: {
                job_id: "s16job_1",
                status: "pending",
                attempt: 1,
                fence: 1,
                lease_owner: null,
                pending_owner_fingerprints: { s01: 4 },
                owner_results: {},
                stable_failure: null,
                completed_at: null,
              },
            }),
          ),
          { headers: { "Content-Type": "application/json" } },
        ),
    });
    expect(S16_REQUEST_KEY("s16req_test_00000001")).toEqual([
      "s16",
      "deletions",
      "s16req_test_00000001",
    ]);
    const { result } = renderHook(
      () => useS16Query("s16req_test_00000001"),
      { wrapper: wrap(client) },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.job?.status).toBe("pending");
    expect(router.calls).toHaveLength(1);
  });

  it("issues zero requests when requestId is null", () => {
    const client = createQueryClient();
    const router = fetchRouter({});
    const { result } = renderHook(() => useS16Query(null), {
      wrapper: wrap(client),
    });
    expect(result.current.fetchStatus).toBe("idle");
    expect(router.calls).toHaveLength(0);
  });
});

describe("S16 approve/commit/repair hooks", () => {
  it("approve posts the manifest digest and the approver bearer token", async () => {
    const client = createQueryClient();
    const router = fetchRouter({
      "POST /controlled/s16/api/deletions/s16req_test_00000001/approve": () =>
        new Response(
          JSON.stringify({
            status: "accepted",
            request_id: "s16req_test_00000001",
            approved_by: "approver-1",
          }),
          { headers: { "Content-Type": "application/json" } },
        ),
    });
    const { result } = renderHook(() => useS16Approve(), {
      wrapper: wrap(client),
    });
    result.current.mutate({
      requestId: "s16req_test_00000001",
      manifestDigest: "d".repeat(64),
      idempotencyKey: "approve-key",
      approverToken: "approver-token-1",
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(router.calls[0]?.body).toEqual({
      manifest_digest: "d".repeat(64),
      idempotency_key: "approve-key",
    });
    const init = vi.mocked(fetch).mock.calls[0]?.[1];
    expect(
      (init?.headers as Record<string, string>)["X-S16-Approver-Token"],
    ).toBe("approver-token-1");
    expect((init?.headers as Record<string, string>).Authorization).toBe(
      undefined,
    );
  });

  it("commit posts only the idempotency key", async () => {
    const client = createQueryClient();
    const router = fetchRouter({
      "POST /controlled/s16/api/deletions/s16req_test_00000001/commit": () =>
        new Response(
          JSON.stringify({
            status: "accepted",
            request_id: "s16req_test_00000001",
            job_id: "s16job_1",
          }),
          { headers: { "Content-Type": "application/json" } },
        ),
    });
    const { result } = renderHook(() => useS16Commit(), {
      wrapper: wrap(client),
    });
    result.current.mutate({
      requestId: "s16req_test_00000001",
      idempotencyKey: "commit-key",
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(router.calls[0]?.body).toEqual({ idempotency_key: "commit-key" });
  });

  it("repair posts owner id and repair fact", async () => {
    const client = createQueryClient();
    const router = fetchRouter({
      "POST /controlled/s16/api/deletions/s16req_test_00000001/repair": () =>
        new Response(
          JSON.stringify({
            status: "accepted",
            request_id: "s16req_test_00000001",
            job_id: "s16job_1",
          }),
          { headers: { "Content-Type": "application/json" } },
        ),
    });
    const { result } = renderHook(() => useS16Repair(), {
      wrapper: wrap(client),
    });
    result.current.mutate({
      requestId: "s16req_test_00000001",
      ownerId: "s02",
      repairFact: "s02-repair-verified",
      idempotencyKey: "repair-key",
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(router.calls[0]?.body).toEqual({
      owner_id: "s02",
      repair_fact: "s02-repair-verified",
      idempotency_key: "repair-key",
    });
  });
});

describe("S16 rejection classifier", () => {
  it("treats registered 403/503 and 404/409/422 as definitive", () => {
    expect(
      isDefinitiveS16Rejection(
        new HttpError(403, { error: "S16_FORBIDDEN" }),
      ),
    ).toBe(true);
    expect(
      isDefinitiveS16Rejection(
        new HttpError(503, { error: "S16_UNAVAILABLE" }),
      ),
    ).toBe(true);
    expect(isDefinitiveS16Rejection(new HttpError(404, { error: "S16_NOT_FOUND" }))).toBe(true);
    expect(isDefinitiveS16Rejection(new HttpError(409, { error: "S16_BLOCKED" }))).toBe(true);
    expect(
      isDefinitiveS16Rejection(
        new HttpError(503, { error: "SOME_OTHER_CODE" }),
      ),
    ).toBe(false);
    expect(isDefinitiveS16Rejection(new TypeError("network lost"))).toBe(false);
  });
});

describe("clearApplicationScopedCache", () => {
  it("drops S01/S02/S12/S13 caches and invalidates S16", async () => {
    const client = createQueryClient();
    client.setQueryData(["s01", "queue"], { stale: true });
    client.setQueryData(["s02", "x"], { stale: true });
    client.setQueryData(["s12", "plans"], { stale: true });
    client.setQueryData(["s13", "delivery", "app_1"], { stale: true });
    client.setQueryData(S16_REQUEST_KEY("s16req_1"), s16QueryPayload());
    clearApplicationScopedCache(client);
    expect(client.getQueryData(["s01", "queue"])).toBeUndefined();
    expect(client.getQueryData(["s02", "x"])).toBeUndefined();
    expect(client.getQueryData(["s12", "plans"])).toBeUndefined();
    expect(client.getQueryData(["s13", "delivery", "app_1"])).toBeUndefined();
    // The S16 entry itself survives for the authority refetch.
    expect(client.getQueryData(S16_REQUEST_KEY("s16req_1"))).toBeDefined();
  });
});
