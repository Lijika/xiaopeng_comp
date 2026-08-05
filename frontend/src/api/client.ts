import type { components } from "../generated/api";

export type QueueResponse = components["schemas"]["S01QueueResponse"];
export type QueueManualItem = components["schemas"]["S01QueueManualItem"];
export type RecoveryQueueItem = components["schemas"]["S01RecoveryQueueItem"];
export type RecoveryWorkResponse = components["schemas"]["S01RecoveryWorkResponse"];
export type VerifyRecoveryResult = components["schemas"]["S01VerifyRecoveryResult"];
export type CurrentRouteResponse = components["schemas"]["S01CurrentRouteResponse"];

/** A structured server rejection; never invents identifiers or evidence. */
export class HttpError extends Error {
  readonly status: number;
  readonly errorCode: string | undefined;
  readonly reasonCode: string | undefined;

  constructor(status: number, detail: unknown) {
    const record =
      typeof detail === "object" && detail !== null
        ? (detail as Record<string, unknown>)
        : {};
    super(
      typeof record.message === "string" ? record.message : `HTTP ${status}`,
    );
    this.name = "HttpError";
    this.status = status;
    this.errorCode =
      typeof record.error === "string" ? record.error : undefined;
    this.reasonCode =
      typeof record.reason_code === "string" ? record.reason_code : undefined;
  }
}

/**
 * The one thin same-origin JSON fetch adapter.  It owns credentials, no-store
 * requests, response decoding, and structured HTTP errors; it owns no business
 * transition and adds no generated runtime SDK.
 */
export async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: "same-origin",
    cache: "no-store",
    headers:
      init.body !== undefined
        ? { "Content-Type": "application/json", ...(init.headers ?? {}) }
        : init.headers,
  });
  const text = await response.text();
  let payload: unknown = null;
  if (text.length > 0) {
    try {
      payload = JSON.parse(text) as unknown;
    } catch {
      payload = null;
    }
  }
  if (!response.ok) {
    const detail = (payload as { detail?: unknown } | null)?.detail;
    throw new HttpError(response.status, detail);
  }
  return payload as T;
}
