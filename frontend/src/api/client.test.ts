import { describe, expect, it, vi } from "vitest";

import { HttpError, request } from "./client";

describe("thin same-origin fetch client", () => {
  it("requests no-store with same-origin credentials and decodes JSON", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ items: [], recovery_items: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const data = await request<{ items: unknown[]; recovery_items: unknown[] }>(
      "/controlled/s01/api/queries/queue",
    );
    expect(data).toEqual({ items: [], recovery_items: [] });
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/controlled/s01/api/queries/queue");
    expect(init.cache).toBe("no-store");
    expect(init.credentials).toBe("same-origin");
    expect(init.headers).toBeUndefined();
  });

  it("sends JSON bodies for commands", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ status: "accepted" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const body = { a: 1 };
    await request("/controlled/s01/api/commands/x", {
      method: "POST",
      body: JSON.stringify(body),
    });
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/controlled/s01/api/commands/x");
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe(
      "application/json",
    );
  });

  it("surfaces structured HTTP errors with stable error codes", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            detail: { error: "S07_STALE", reason_code: "recovery.context_changed" },
          }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );
    const error = await request("/controlled/s01/api/commands/x").catch(
      (caught: unknown) => caught,
    );
    expect(error).toBeInstanceOf(HttpError);
    expect(error).toMatchObject({
      status: 409,
      errorCode: "S07_STALE",
      reasonCode: "recovery.context_changed",
    });
  });

  it("classifies existence-hiding 404s without leaking identifiers", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: { error: "S07_NOT_FOUND" } }), {
          status: 404,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    const error = await request("/controlled/s01/api/queries/recovery-work-items/hidden").catch(
      (caught: unknown) => caught,
    );
    expect(error).toBeInstanceOf(HttpError);
    expect((error as HttpError).status).toBe(404);
    expect((error as HttpError).errorCode).toBe("S07_NOT_FOUND");
    expect((error as HttpError).message).not.toContain("hidden");
  });
});
