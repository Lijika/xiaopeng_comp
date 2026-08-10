import { describe, expect, it, vi } from "vitest";

import {
  HttpError,
  isDefinitiveIntegratorRejection,
  isDefinitiveS05Rejection,
  isDefinitiveS08Rejection,
  request,
} from "./client";

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

describe("Integrator definitive rejection classifier (T04)", () => {
  function httpError(status: number, errorCode: string | undefined): HttpError {
    return new HttpError(status, errorCode === undefined ? {} : { error: errorCode });
  }

  it("treats the structured S02_FORBIDDEN 403 as definitive", () => {
    expect(
      isDefinitiveIntegratorRejection(httpError(403, "S02_FORBIDDEN")),
    ).toBe(true);
  });

  it("treats the structured S02_UNAVAILABLE 503 as definitive", () => {
    expect(
      isDefinitiveIntegratorRejection(httpError(503, "S02_UNAVAILABLE")),
    ).toBe(true);
  });

  it("keeps a generic 403 unknown (not definitive)", () => {
    expect(isDefinitiveIntegratorRejection(httpError(403, undefined))).toBe(
      false,
    );
    expect(isDefinitiveIntegratorRejection(httpError(403, "S02_OTHER"))).toBe(
      false,
    );
  });

  it("keeps a generic or unrelated 503 unknown (not definitive)", () => {
    expect(isDefinitiveIntegratorRejection(httpError(503, undefined))).toBe(
      false,
    );
    expect(isDefinitiveIntegratorRejection(httpError(503, "S03_UNAVAILABLE"))).toBe(
      false,
    );
  });

  it("keeps non-HTTP transport failures unknown", () => {
    expect(
      isDefinitiveIntegratorRejection(new TypeError("fetch failed")),
    ).toBe(false);
  });

  it("keeps the registered 404/409/413/422 statuses definitive", () => {
    for (const status of [404, 409, 413, 422]) {
      expect(isDefinitiveIntegratorRejection(httpError(status, "S02_X"))).toBe(
        true,
      );
    }
  });
});

describe("S05 definitive rejection classifier (T05)", () => {
  function httpError(status: number, errorCode: string | undefined): HttpError {
    return new HttpError(status, errorCode === undefined ? {} : { error: errorCode });
  }

  it("treats the structured S05_STOPPED and S05_UNAVAILABLE 503s as definitive", () => {
    expect(isDefinitiveS05Rejection(httpError(503, "S05_STOPPED"))).toBe(true);
    expect(isDefinitiveS05Rejection(httpError(503, "S05_UNAVAILABLE"))).toBe(
      true,
    );
  });

  it("keeps a generic or unrelated 503 unknown", () => {
    expect(isDefinitiveS05Rejection(httpError(503, undefined))).toBe(false);
    expect(isDefinitiveS05Rejection(httpError(503, "S03_UNAVAILABLE"))).toBe(
      false,
    );
    expect(isDefinitiveS05Rejection(httpError(503, "S05_OTHER"))).toBe(false);
  });

  it("treats the registered S05 404/409/413/422 statuses as definitive", () => {
    for (const status of [404, 409, 413, 422]) {
      expect(isDefinitiveS05Rejection(httpError(status, "S05_X"))).toBe(true);
    }
  });

  it("keeps non-HTTP transport failures unknown", () => {
    expect(isDefinitiveS05Rejection(new TypeError("fetch failed"))).toBe(false);
  });
});

describe("S08 definitive rejection classifier (T08)", () => {
  function httpError(status: number, errorCode: string | undefined): HttpError {
    return new HttpError(status, errorCode === undefined ? {} : { error: errorCode });
  }

  it("treats the registered S08_FORBIDDEN 403 as definitive", () => {
    expect(isDefinitiveS08Rejection(httpError(403, "S08_FORBIDDEN"))).toBe(true);
  });

  it("keeps a generic 403 unknown (may be a proxy or unrelated gate)", () => {
    expect(isDefinitiveS08Rejection(httpError(403, undefined))).toBe(false);
  });

  it("treats the registered S08 404/409/422 statuses as definitive", () => {
    for (const status of [404, 409, 422]) {
      expect(isDefinitiveS08Rejection(httpError(status, "S08_X"))).toBe(true);
    }
  });

  it("treats the structured S08_UNAVAILABLE 503 as definitive", () => {
    expect(isDefinitiveS08Rejection(httpError(503, "S08_UNAVAILABLE"))).toBe(true);
  });

  it("keeps a generic or unrelated 503 unknown so the idempotency key is retained", () => {
    expect(isDefinitiveS08Rejection(httpError(503, undefined))).toBe(false);
    expect(isDefinitiveS08Rejection(httpError(503, "S08_REACT_UNAVAILABLE"))).toBe(
      false,
    );
    expect(isDefinitiveS08Rejection(httpError(503, "S03_UNAVAILABLE"))).toBe(false);
  });

  it("keeps non-HTTP transport failures unknown", () => {
    expect(isDefinitiveS08Rejection(new TypeError("fetch failed"))).toBe(false);
  });
});
