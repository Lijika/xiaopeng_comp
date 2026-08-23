import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { HttpError } from "./client";
import { retryPolicy, S13_DELIVERY_KEY, useS13Delivery } from "./hooks";
import { createQueryClient, fetchRouter } from "../test-utils";
import {
  S13_APPLICATION_ID,
  S13_OPERATION_ID,
  s13QueryPayload,
} from "../test-fixtures/s13";

function wrap(client: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

describe("S13 delivery query hook", () => {
  it("uses one parameterized same-origin no-store GET", async () => {
    const client = createQueryClient();
    const router = fetchRouter({
      [`GET /controlled/s13/delivery/${S13_APPLICATION_ID}`]: () =>
        new Response(JSON.stringify(s13QueryPayload()), {
          headers: { "Content-Type": "application/json" },
        }),
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

    expect(result.current.data?.obligation?.operation_id).toBe(S13_OPERATION_ID);
    expect(result.current.data?.routing_history).toHaveLength(1);
    expect(router.calls).toEqual([
      {
        method: "GET",
        url: `/controlled/s13/delivery/${S13_APPLICATION_ID}`,
        body: undefined,
      },
    ]);
    const init = vi.mocked(fetch).mock.calls[0]?.[1];
    expect(init).toMatchObject({
      credentials: "same-origin",
      cache: "no-store",
    });
  });

  it.each([null, ""] as const)(
    "issues zero requests when applicationId is %s",
    (applicationId) => {
      const client = createQueryClient();
      const router = fetchRouter({});
      const { result } = renderHook(() => useS13Delivery(applicationId), {
        wrapper: wrap(client),
      });

      expect(result.current.fetchStatus).toBe("idle");
      expect(router.calls).toHaveLength(0);
    },
  );

  it("retries transient GET failures within the shared bounded policy", () => {
    expect(retryPolicy(0, new HttpError(404, { error: "S13_NOT_FOUND" }))).toBe(
      false,
    );
    expect(
      retryPolicy(0, new HttpError(503, { error: "S13_UNAVAILABLE" })),
    ).toBe(true);
    expect(
      retryPolicy(2, new HttpError(503, { error: "S13_UNAVAILABLE" })),
    ).toBe(false);
    expect(
      retryPolicy(0, new HttpError(403, { error: "S13_FORBIDDEN" })),
    ).toBe(false);
  });

  it("shows the latest error state without presenting cached facts", async () => {
    const client = createQueryClient();
    client.setQueryData(
      S13_DELIVERY_KEY(S13_APPLICATION_ID),
      s13QueryPayload(),
    );
    fetchRouter({
      [`GET /controlled/s13/delivery/${S13_APPLICATION_ID}`]: () =>
        new Response(
          JSON.stringify({
            detail: { error: "S13_NOT_FOUND", message: "unavailable" },
          }),
          { status: 404, headers: { "Content-Type": "application/json" } },
        ),
    });
    const { result } = renderHook(() => useS13Delivery(S13_APPLICATION_ID), {
      wrapper: wrap(client),
    });

    await client.refetchQueries({
      queryKey: S13_DELIVERY_KEY(S13_APPLICATION_ID),
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect((result.current.error as HttpError).status).toBe(404);
  });
});
