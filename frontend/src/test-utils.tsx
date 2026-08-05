import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { vi } from "vitest";

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
}

export function renderWithQuery(
  ui: ReactElement,
  client: QueryClient = createQueryClient(),
) {
  return { ...render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>), client };
}

export type RouteHandler = (
  url: string,
  init: RequestInit | undefined,
) => Response | Promise<Response>;

export interface RecordedCall {
  method: string;
  url: string;
  body: unknown;
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function errorResponse(status: number, error: string, reasonCode?: string): Response {
  const detail: Record<string, unknown> = { error };
  if (reasonCode !== undefined) detail.reason_code = reasonCode;
  return jsonResponse({ detail }, status);
}

/**
 * Fetch boundary mock: routes "METHOD /pathname" to a handler, records every
 * call, and rejects with a network TypeError for unmocked routes (so a
 * component reaching an unexpected endpoint fails loudly instead of passing).
 */
export function fetchRouter(routes: Record<string, RouteHandler>) {
  const calls: RecordedCall[] = [];
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    const pathname = new URL(url, "http://localhost").pathname;
    calls.push({
      method,
      url,
      body: init?.body !== undefined ? JSON.parse(String(init.body)) : undefined,
    });
    const handler = routes[`${method} ${pathname}`];
    if (!handler) {
      return Promise.reject(
        new TypeError(`no mocked route for ${method} ${pathname}`),
      );
    }
    return Promise.resolve(handler(url, init));
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, jsonResponse, errorResponse };
}
