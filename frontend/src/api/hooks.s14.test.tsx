import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import {
  useCurrentRoute,
  ROUTE_KEY,
  useS14Cancel,
  useS14GrantReopenPermission,
  useS14ProcessNotification,
  useS14Reopen,
  useS14Settle,
  useTerminationConvergence,
} from "./hooks";
import { createQueryClient, fetchRouter } from "../test-utils";
import {
  ARTIFACT_DIGEST,
  S14_APPLICATION_ID,
  S14_APPROVER_SUBJECT,
  S14_PERMISSION_ID,
  s14AcceptedCancel,
  s14AcceptedGrant,
  s14AcceptedReopen,
  s14CurrentRoute,
  s14OutstandingSettle,
  s14TerminatedSettle,
} from "../test-fixtures/s14";

const CANCEL_PATH = `/controlled/s01/api/commands/applications/${S14_APPLICATION_ID}/cancel`;
const SETTLE_PATH = `/controlled/s01/api/commands/applications/${S14_APPLICATION_ID}/settle-termination`;
const GRANT_PATH = `/controlled/s01/api/commands/applications/${S14_APPLICATION_ID}/grant-reopen-permission`;
const REOPEN_PATH = `/controlled/s01/api/commands/applications/${S14_APPLICATION_ID}/reopen`;
const NOTIFY_PATH = "/controlled/s01/api/commands/process-termination-notification";
const ROUTE_PATH = `/controlled/s01/api/queries/applications/${S14_APPLICATION_ID}/current-route`;

function wrap(client: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

describe("S14 command hooks", () => {
  it("sends one exact generated cancel body and resolves the typed envelope", async () => {
    const client = createQueryClient();
    const router = fetchRouter({
      [`${"POST"} ${CANCEL_PATH}`]: () =>
        new Response(JSON.stringify(s14AcceptedCancel()), {
          headers: { "Content-Type": "application/json" },
        }),
    });
    const { result } = renderHook(() => useS14Cancel(S14_APPLICATION_ID), {
      wrapper: wrap(client),
    });
    result.current.mutate({
      expected_lifecycle_revision: 5,
      idempotency_key: "t16-hook-cancel-1",
      reason_code: "UPSTREAM_WITHDRAWN",
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(result.current.data?.status).toBe("accepted");
    expect(result.current.data?.phase).toBe("Terminating");
    expect(router.calls).toEqual([
      {
        method: "POST",
        url: CANCEL_PATH,
        body: {
          expected_lifecycle_revision: 5,
          idempotency_key: "t16-hook-cancel-1",
          reason_code: "UPSTREAM_WITHDRAWN",
        },
      },
    ]);
    // Same-origin no-store POST contract.
    const init = vi.mocked(fetch).mock.calls[0]?.[1];
    expect(init).toMatchObject({
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
    });
  });

  it("resolves a definitive stale domain body without throwing", async () => {
    const client = createQueryClient();
    fetchRouter({
      [`POST ${SETTLE_PATH}`]: () =>
        new Response(
          JSON.stringify(
            s14CommandResultStale(),
          ),
          { status: 409, headers: { "Content-Type": "application/json" } },
        ),
    });
    const { result } = renderHook(() => useS14Settle(S14_APPLICATION_ID), {
      wrapper: wrap(client),
    });
    result.current.mutate({
      expected_lifecycle_revision: 5,
      idempotency_key: "t16-hook-settle-stale",
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.status).toBe("stale");
    expect(result.current.data?.reason_code).toBe(
      "lifecycle.settle_stale_revision",
    );
  });

  it("keeps retry disabled so an unknown transport never auto-replays", async () => {
    const client = createQueryClient();
    const router = fetchRouter({});
    router; // no mocked route: every call rejects with a network TypeError
    const { result } = renderHook(() => useS14Settle(S14_APPLICATION_ID), {
      wrapper: wrap(client),
    });
    result.current.mutate({
      expected_lifecycle_revision: 5,
      idempotency_key: "t16-hook-settle-unknown",
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(1);
    expect(result.current.error).not.toBeUndefined();
  });

  it("sends the grant body and surfaces the server-owned reopen bindings", async () => {
    const client = createQueryClient();
    const router = fetchRouter({
      [`POST ${GRANT_PATH}`]: () =>
        new Response(JSON.stringify(s14AcceptedGrant()), {
          headers: { "Content-Type": "application/json" },
        }),
    });
    const { result } = renderHook(
      () => useS14GrantReopenPermission(S14_APPLICATION_ID),
      { wrapper: wrap(client) },
    );
    result.current.mutate({
      expected_lifecycle_revision: 7,
      approver_subject: S14_APPROVER_SUBJECT,
      permission_id: S14_PERMISSION_ID,
      idempotency_key: "t16-hook-grant-1",
      ttl_seconds: 3600,
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.artifact_release_digest).toBe(ARTIFACT_DIGEST);
    expect(result.current.data?.permission_id).toBe(S14_PERMISSION_ID);
    expect(router.calls[0]?.body).toEqual({
      expected_lifecycle_revision: 7,
      approver_subject: S14_APPROVER_SUBJECT,
      permission_id: S14_PERMISSION_ID,
      idempotency_key: "t16-hook-grant-1",
      ttl_seconds: 3600,
    });
  });

  it("sends the reopen policy exactly as granted by the server", async () => {
    const client = createQueryClient();
    const router = fetchRouter({
      [`POST ${REOPEN_PATH}`]: () =>
        new Response(JSON.stringify(s14AcceptedReopen()), {
          headers: { "Content-Type": "application/json" },
        }),
    });
    const { result } = renderHook(() => useS14Reopen(S14_APPLICATION_ID), {
      wrapper: wrap(client),
    });
    result.current.mutate({
      expected_lifecycle_revision: 7,
      idempotency_key: "t16-hook-reopen-1",
      target_phase: "Intake",
      reopen_policy: {
        permission_id: S14_PERMISSION_ID,
        release_digest: ARTIFACT_DIGEST,
      },
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.cycle).toBe(2);
    expect(result.current.data?.predecessor_cycle).toBe(1);
    expect(router.calls[0]?.body).toEqual({
      expected_lifecycle_revision: 7,
      idempotency_key: "t16-hook-reopen-1",
      target_phase: "Intake",
      reopen_policy: {
        permission_id: S14_PERMISSION_ID,
        release_digest: ARTIFACT_DIGEST,
      },
    });
  });

  it("posts the operator notification without a body", async () => {
    const client = createQueryClient();
    const router = fetchRouter({
      [`POST ${NOTIFY_PATH}`]: () =>
        new Response(
          JSON.stringify({ status: "delivered", replayed: false }),
          { headers: { "Content-Type": "application/json" } },
        ),
    });
    const { result } = renderHook(() => useS14ProcessNotification(), {
      wrapper: wrap(client),
    });
    result.current.mutate(undefined);
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.status).toBe("delivered");
    expect(router.calls).toEqual([
      { method: "POST", url: NOTIFY_PATH, body: undefined },
    ]);
  });

  it("invalidates the authoritative S01 reads after a typed outcome", async () => {
    const client = createQueryClient();
    client.setQueryData(ROUTE_KEY(S14_APPLICATION_ID), s14CurrentRoute());
    let settleCalls = 0;
    fetchRouter({
      [`POST ${SETTLE_PATH}`]: () => {
        settleCalls += 1;
        return new Response(
          JSON.stringify(settleCalls === 1 ? s14OutstandingSettle() : s14TerminatedSettle()),
          {
            status: settleCalls === 1 ? 202 : 200,
            headers: { "Content-Type": "application/json" },
          },
        );
      },
      [`GET ${ROUTE_PATH}`]: () =>
        new Response(
          JSON.stringify(
            s14CurrentRoute({
              phase: "Terminated",
              lifecycle_revision: 7,
              current_run_id: null,
            }),
          ),
          { headers: { "Content-Type": "application/json" } },
        ),
    });
    const { result } = renderHook(() => useS14Settle(S14_APPLICATION_ID), {
      wrapper: wrap(client),
    });
    // A mounted authoritative read is what invalidation converges.
    const { result: route } = renderHook(
      () => useCurrentRoute(S14_APPLICATION_ID),
      { wrapper: wrap(client) },
    );
    await waitFor(() => expect(route.current.isSuccess).toBe(true));
    result.current.mutate({
      expected_lifecycle_revision: 6,
      idempotency_key: "t16-hook-settle-invalidate",
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    await waitFor(() =>
      expect(
        client.getQueryData<{ phase: string }>(ROUTE_KEY(S14_APPLICATION_ID))
          ?.phase,
      ).toBe("Terminated"),
    );
  });
});

function s14CommandResultStale() {
  return {
    status: "stale",
    replayed: false,
    application_id: S14_APPLICATION_ID,
    reason_code: "lifecycle.settle_stale_revision",
  };
}

describe("useTerminationConvergence", () => {
  it("stays waiting while the route stays Terminating then reports terminated only from server data", async () => {
    const client = createQueryClient();
    client.setQueryData(
      ROUTE_KEY(S14_APPLICATION_ID),
      s14CurrentRoute({ phase: "Terminating", lifecycle_revision: 6 }),
    );
    let routeCalls = 0;
    fetchRouter({
      [`GET ${ROUTE_PATH}`]: () => {
        routeCalls += 1;
        return new Response(
          JSON.stringify(
            routeCalls < 2
              ? s14CurrentRoute({ phase: "Terminating", lifecycle_revision: 6 })
              : s14CurrentRoute({
                  phase: "Terminated",
                  lifecycle_revision: 7,
                  current_run_id: null,
                }),
          ),
          { headers: { "Content-Type": "application/json" } },
        );
      },
    });
    const { result } = renderHook(
      () =>
        useTerminationConvergence(S14_APPLICATION_ID, true, {
          intervalMs: 5,
          maxAttempts: 10,
        }),
      { wrapper: wrap(client) },
    );
    const { result: route } = renderHook(
      () => useCurrentRoute(S14_APPLICATION_ID),
      { wrapper: wrap(client) },
    );
    await waitFor(() => expect(route.current.isSuccess).toBe(true));
    await waitFor(() => expect(result.current).toBe("terminated"));
    expect(routeCalls).toBeGreaterThanOrEqual(2);
  });

  it("ends with the explicit bounded unknown instead of inferring termination", async () => {
    const client = createQueryClient();
    client.setQueryData(
      ROUTE_KEY(S14_APPLICATION_ID),
      s14CurrentRoute({ phase: "Terminating", lifecycle_revision: 6 }),
    );
    fetchRouter({
      [`GET ${ROUTE_PATH}`]: () =>
        new Response(
          JSON.stringify(
            s14CurrentRoute({ phase: "Terminating", lifecycle_revision: 6 }),
          ),
          { headers: { "Content-Type": "application/json" } },
        ),
    });
    const { result } = renderHook(
      () =>
        useTerminationConvergence(S14_APPLICATION_ID, true, {
          intervalMs: 2,
          maxAttempts: 3,
        }),
      { wrapper: wrap(client) },
    );
    await waitFor(() => expect(result.current).toBe("timed_out"));
  });

  it("is idle when the poll is not active", () => {
    const client = createQueryClient();
    const router = fetchRouter({});
    const { result } = renderHook(
      () => useTerminationConvergence(S14_APPLICATION_ID, false),
      { wrapper: wrap(client) },
    );
    expect(result.current).toBe("idle");
    expect(router.calls).toHaveLength(0);
  });
});
