import type { components } from "../generated/api";

export type QueueResponse = components["schemas"]["S01QueueResponse"];
export type QueueManualItem = components["schemas"]["S01QueueManualItem"];
export type RecoveryQueueItem = components["schemas"]["S01RecoveryQueueItem"];
export type RecoveryWorkResponse = components["schemas"]["S01RecoveryWorkResponse"];
export type VerifyRecoveryResult = components["schemas"]["S01VerifyRecoveryResult"];
export type CurrentRouteResponse = components["schemas"]["S01CurrentRouteResponse"];
export type ReviewWorkResponse = components["schemas"]["S01ReviewWorkItemResponse"];
export type WorkspaceResponse = components["schemas"]["S01WorkspaceResponse"];
export type ApplicationHistoryResponse =
  components["schemas"]["S01ApplicationHistoryResponse"];
export type ClaimResult = components["schemas"]["S01ClaimResult"];
export type RenewResult = components["schemas"]["S01RenewResult"];
export type ReleaseResult = components["schemas"]["S01ReleaseResult"];
export type SubmitResult = components["schemas"]["S01SubmitResult"];
export type SupplementRequestResult =
  components["schemas"]["S01SupplementRequestResult"];
export type SupplementRequestView = components["schemas"]["S01SupplementRequestView"];
export type IntegratorSupplementRequestView =
  components["schemas"]["S01IntegratorSupplementRequestView"];
export type AttachmentSubmissionResponse =
  components["schemas"]["S01AttachmentSubmissionResponse"];
export type BusinessExceptionRequestResult =
  components["schemas"]["T05BusinessExceptionRequestResult"];
export type BusinessExceptionView =
  components["schemas"]["T05BusinessExceptionView"];
export type ExceptionClaimResult = components["schemas"]["T05ExceptionClaimResult"];
export type ExceptionDecisionResult =
  components["schemas"]["T05ExceptionDecisionResult"];
export type BusinessExceptionOperationsStatus =
  components["schemas"]["T05BusinessExceptionOperationsStatus"];
export type DemoFixturesResponse = components["schemas"]["DemoFixturesResponse"];
export type DemoFixtureOption = components["schemas"]["DemoFixtureOption"];
export type DemoCheckResponse = components["schemas"]["DemoCheckResponse"];
export type DemoCheckItem = components["schemas"]["DemoCheckItem"];
export type DemoSnapshotItem = components["schemas"]["DemoSnapshotItem"];
export type DemoDiffHighlight = components["schemas"]["DemoDiffHighlight"];
export type DemoSummary = components["schemas"]["DemoSummary"];
export type DemoConfigInfo = components["schemas"]["DemoConfigInfo"];
export type DemoEvidenceLink = components["schemas"]["DemoEvidenceLink"];
export type DemoBatchCheckResponse =
  components["schemas"]["DemoBatchCheckResponse"];
export type DemoBatchItem = components["schemas"]["DemoBatchItem"];
export type DemoEvaluationSummaryResponse =
  components["schemas"]["DemoEvaluationSummaryResponse"];
export type S08CandidateWorkspaceResponse =
  components["schemas"]["S08CandidateWorkspaceResponse"];
export type S08ImportLegacyResponse =
  components["schemas"]["S08ImportLegacyResponse"];
export type S08ReviseDraftResponse =
  components["schemas"]["S08ReviseDraftResponse"];
export type S08FreezeCandidateResponse =
  components["schemas"]["S08FreezeCandidateResponse"];
export type S08RequestValidationResponse =
  components["schemas"]["S08RequestValidationResponse"];
export type S08SubmitReviewResponse =
  components["schemas"]["S08SubmitReviewResponse"];
export type S08ApproveResponse = components["schemas"]["S08ApproveResponse"];
export type S08RejectResponse = components["schemas"]["S08RejectResponse"];
export type S08ScheduleResponse = components["schemas"]["S08ScheduleResponse"];
export type S08CancelResponse = components["schemas"]["S08CancelResponse"];
export type S08StatusResponse = components["schemas"]["S08StatusResponse"];

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
 * The statuses that prove a command was never accepted: any of them makes the
 * pending idempotency key safe to rotate.  Every other HTTP outcome (5xx,
 * other statuses) may have committed an effect and stays visibly unknown
 * with the same key retained.
 *
 * 503 is special: an intermediary/generic 503 is not proof that the S03
 * authority rejected before commit, so it is definitive only when the body
 * carries the registered S03 pre-command code.  The review panel's command
 * surface is exactly the S03 authority, so its status set includes 503 and
 * the classifier narrows it; the recovery panel keeps 503 in its unknown set
 * because its verifier may have committed.
 */
export const REVIEW_DEFINITIVE_STATUSES: ReadonlySet<number> = new Set([
  404,
  409,
  413,
  422,
  503,
]);
export const RECOVERY_DEFINITIVE_STATUSES: ReadonlySet<number> = new Set([409]);
/** The structured S03 codes that prove a 503 was rejected before any commit. */
const REVIEW_503_DEFINITIVE_ERROR_CODES: ReadonlySet<string> = new Set([
  "S03_STOPPED",
  "S03_UNAVAILABLE",
]);
/** The structured S05 codes that prove a 503 was rejected before any commit. */
const S05_503_DEFINITIVE_ERROR_CODES: ReadonlySet<string> = new Set([
  "S05_STOPPED",
  "S05_UNAVAILABLE",
]);

export function isDefinitiveRejection(
  error: unknown,
  statuses: ReadonlySet<number> = REVIEW_DEFINITIVE_STATUSES,
): error is HttpError {
  if (!(error instanceof HttpError)) return false;
  if (error.status === 503) {
    return (
      statuses.has(503) &&
      error.errorCode !== undefined &&
      REVIEW_503_DEFINITIVE_ERROR_CODES.has(error.errorCode)
    );
  }
  return statuses.has(error.status);
}

/**
 * The S05 command surface's definitive-rejection classifier: the registered
 * S05 pre-command responses.  The structured ``S05_STOPPED``/``S05_UNAVAILABLE``
 * 503 codes are definitive, as are the registered 404/409/413/422 statuses.
 * A generic or unstructured 503 stays unknown and retains the command.
 */
export function isDefinitiveS05Rejection(error: unknown): error is HttpError {
  if (!(error instanceof HttpError)) return false;
  if (error.status === 503) {
    return (
      error.errorCode !== undefined &&
      S05_503_DEFINITIVE_ERROR_CODES.has(error.errorCode)
    );
  }
  return REVIEW_DEFINITIVE_STATUSES.has(error.status);
}

/**
 * The Integrator panel's definitive-rejection classifier.  The registered
 * S02 pre-command responses are the evidence: a structured ``S02_FORBIDDEN``
 * 403 or ``S02_UNAVAILABLE`` 503 is definitive, as are the registered
 * 404/409/413/422 statuses.  Every other 403/503, an unreadable payload, or
 * a network/lost response stays unknown and retains the byte-identical
 * command and key for exact replay.
 */
const INTEGRATOR_DEFINITIVE_STATUSES: ReadonlySet<number> = new Set([
  404,
  409,
  413,
  422,
]);
const INTEGRATOR_403_DEFINITIVE_ERROR_CODES: ReadonlySet<string> = new Set([
  "S02_FORBIDDEN",
]);
const INTEGRATOR_503_DEFINITIVE_ERROR_CODES: ReadonlySet<string> = new Set([
  "S02_UNAVAILABLE",
]);

export function isDefinitiveIntegratorRejection(
  error: unknown,
): error is HttpError {
  if (!(error instanceof HttpError)) return false;
  if (error.status === 403) {
    return (
      error.errorCode !== undefined &&
      INTEGRATOR_403_DEFINITIVE_ERROR_CODES.has(error.errorCode)
    );
  }
  if (error.status === 503) {
    return (
      error.errorCode !== undefined &&
      INTEGRATOR_503_DEFINITIVE_ERROR_CODES.has(error.errorCode)
    );
  }
  return INTEGRATOR_DEFINITIVE_STATUSES.has(error.status);
}

/**
 * The S08 command surface's definitive-rejection classifier.  The registered
 * S08 pre-command responses are the evidence: a structured ``S08_FORBIDDEN``
 * 403 or ``S08_UNAVAILABLE`` 503 is definitive (the governance authority
 * failed closed before commit), as are the registered 404/409/422 statuses.
 * A generic 403/503, an unreadable payload, or a transport/lost response
 * stays unknown and retains the byte-identical command and key for exact
 * replay.
 */
const S08_DEFINITIVE_STATUSES: ReadonlySet<number> = new Set([404, 409, 422]);
const S08_403_DEFINITIVE_ERROR_CODES: ReadonlySet<string> = new Set([
  "S08_FORBIDDEN",
]);
const S08_503_DEFINITIVE_ERROR_CODES: ReadonlySet<string> = new Set([
  "S08_UNAVAILABLE",
]);

export function isDefinitiveS08Rejection(
  error: unknown,
): error is HttpError {
  if (!(error instanceof HttpError)) return false;
  if (error.status === 403) {
    return (
      error.errorCode !== undefined &&
      S08_403_DEFINITIVE_ERROR_CODES.has(error.errorCode)
    );
  }
  if (error.status === 503) {
    return (
      error.errorCode !== undefined &&
      S08_503_DEFINITIVE_ERROR_CODES.has(error.errorCode)
    );
  }
  return S08_DEFINITIVE_STATUSES.has(error.status);
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
