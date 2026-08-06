import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";

import { useEffect, useState } from "react";

import type { paths, components } from "../generated/api";
import {
  request,
  HttpError,
  isDefinitiveRejection,
  type ApplicationHistoryResponse,
  type ClaimResult,
  type CurrentRouteResponse,
  type QueueResponse,
  type RecoveryWorkResponse,
  type ReleaseResult,
  type RenewResult,
  type ReviewWorkResponse,
  type SubmitResult,
  type VerifyRecoveryResult,
  type WorkspaceResponse,
} from "./client";

/** The restricted reveal and evidence-correction command results, bound to
 * the generated OpenAPI schemas (mirrors the sibling result aliases in
 * client.ts without extending that file). */
export type RevealResult = components["schemas"]["S01RevealResult"];
export type CorrectionResult = components["schemas"]["S01CorrectionResult"];
export type SupplementRequestResult =
  components["schemas"]["S01SupplementRequestResult"];
export type SupplementRequestView =
  components["schemas"]["S01SupplementRequestView"];
export type IntegratorSupplementRequestView =
  components["schemas"]["S01IntegratorSupplementRequestView"];
export type AttachmentSubmissionResponse =
  components["schemas"]["S01AttachmentSubmissionResponse"];

export const QUEUE_KEY = ["s01", "queue"] as const;
export const WORK_KEY = (workId: string) =>
  ["s01", "recovery-work", workId] as const;
export const ROUTE_KEY = (applicationId: string) =>
  ["s01", "current-route", applicationId] as const;
export const MANUAL_WORK_KEY = (workId: string) =>
  ["s01", "manual-work", workId] as const;
export const WORKSPACE_KEY = (applicationId: string) =>
  ["s01", "workspace", applicationId] as const;
export const HISTORY_KEY = (applicationId: string) =>
  ["s01", "history", applicationId] as const;
export const SUPPLEMENT_REQUEST_KEY = (requestId: string) =>
  ["s01", "supplement-request", requestId] as const;
export const INTEGRATOR_REQUEST_KEY = (requestId: string) =>
  ["s02", "supplement-request", requestId] as const;

/**
 * The S01 manual-review command bodies are bound to the generated OpenAPI
 * request schemas; a backend contract change fails strict typecheck here.
 */
export type ClaimCommand = paths["/controlled/s01/api/commands/review-work-items/{work_item_id}/claim"]["post"]["requestBody"]["content"]["application/json"];
export type FencedCommand = paths["/controlled/s01/api/commands/review-work-items/{work_item_id}/renew"]["post"]["requestBody"]["content"]["application/json"];
export type SubmitCommand = paths["/controlled/s01/api/commands/review-work-items/{work_item_id}/submit"]["post"]["requestBody"]["content"]["application/json"];
export type RevealCommand = paths["/controlled/s01/api/commands/review-work-items/{work_item_id}/reveal-field-observation"]["post"]["requestBody"]["content"]["application/json"];
export type CorrectionCommand = paths["/controlled/s01/api/commands/review-work-items/{work_item_id}/correct-field-observation"]["post"]["requestBody"]["content"]["application/json"];

/**
 * The T04 commands are bound to the generated OpenAPI request schemas; a
 * backend contract change fails strict typecheck here.
 */
export type SupplementRequestCommand = NonNullable<paths["/controlled/s01/api/commands/review-work-items/{work_item_id}/supplement"]["post"]["requestBody"]>["content"]["application/json"];
export type AttachmentSubmissionCommand = NonNullable<paths["/controlled/s02/api/commands/submit-attachment-version"]["post"]["requestBody"]>["content"]["application/json"];

/**
 * The VerifyRecovery POST body is bound to the generated OpenAPI request
 * schema; a backend contract change fails strict typecheck here.
 */
export type VerifyRecoveryCommand = paths["/controlled/s01/api/commands/recovery-work-items/{recovery_work_id}/verify"]["post"]["requestBody"]["content"]["application/json"];

const TRANSIENT_HTTP_STATUSES = new Set([408, 429, 502, 503, 504]);

/**
 * Endpoint- and status-specific GET retry: existence-hiding 404 (and any
 * other client error) is never retried; only transient server statuses and
 * transport-level failures are, and at most twice.
 */
export function retryPolicy(failureCount: number, error: Error): boolean {
  if (failureCount >= 2) return false;
  if (error instanceof HttpError) {
    return TRANSIENT_HTTP_STATUSES.has(error.status);
  }
  return true;
}

export function useQueue(): UseQueryResult<QueueResponse> {
  return useQuery({
    queryKey: QUEUE_KEY,
    queryFn: () => request<QueueResponse>("/controlled/s01/api/queries/queue"),
    retry: retryPolicy,
  });
}

export function useRecoveryWork(
  workId: string,
): UseQueryResult<RecoveryWorkResponse> {
  return useQuery({
    queryKey: WORK_KEY(workId),
    queryFn: () =>
      request<RecoveryWorkResponse>(
        `/controlled/s01/api/queries/recovery-work-items/${encodeURIComponent(workId)}`,
      ),
    retry: retryPolicy,
  });
}

export function useCurrentRoute(
  applicationId: string | null,
): UseQueryResult<CurrentRouteResponse> {
  return useQuery({
    queryKey: ROUTE_KEY(applicationId ?? ""),
    enabled: applicationId !== null,
    queryFn: () =>
      request<CurrentRouteResponse>(
        `/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId ?? "")}/current-route`,
      ),
    retry: retryPolicy,
  });
}

export function useManualWork(
  workId: string,
): UseQueryResult<ReviewWorkResponse> {
  return useQuery({
    queryKey: MANUAL_WORK_KEY(workId),
    queryFn: () =>
      request<ReviewWorkResponse>(
        `/controlled/s01/api/queries/review-work-items/${encodeURIComponent(workId)}`,
      ),
    retry: retryPolicy,
  });
}

export function useWorkspace(
  applicationId: string | null,
): UseQueryResult<WorkspaceResponse> {
  return useQuery({
    queryKey: WORKSPACE_KEY(applicationId ?? ""),
    enabled: applicationId !== null,
    queryFn: () =>
      request<WorkspaceResponse>(
        `/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId ?? "")}/workspace`,
      ),
    retry: retryPolicy,
  });
}

export function useApplicationHistory(
  applicationId: string | null,
): UseQueryResult<ApplicationHistoryResponse> {
  return useQuery({
    queryKey: HISTORY_KEY(applicationId ?? ""),
    enabled: applicationId !== null,
    queryFn: () =>
      request<ApplicationHistoryResponse>(
        `/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId ?? "")}/history`,
      ),
    retry: retryPolicy,
  });
}

/**
 * The four manual-review command hooks share one shape: a retry:false POST
 * through the thin same-origin adapter whose acceptance invalidates the
 * server-owned S01 queries.
 */
function useReviewCommandMutation<TResult, TCommand>(
  path: string,
): UseMutationResult<TResult, Error, TCommand> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (command: TCommand) =>
      request<TResult>(path, { method: "POST", body: JSON.stringify(command) }),
    retry: false,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["s01"] }),
  });
}

export function useClaimWorkItem(
  workId: string,
): UseMutationResult<ClaimResult, Error, ClaimCommand> {
  return useReviewCommandMutation<ClaimResult, ClaimCommand>(
    `/controlled/s01/api/commands/review-work-items/${encodeURIComponent(workId)}/claim`,
  );
}

export function useRenewWorkItem(
  workId: string,
): UseMutationResult<RenewResult, Error, FencedCommand> {
  return useReviewCommandMutation<RenewResult, FencedCommand>(
    `/controlled/s01/api/commands/review-work-items/${encodeURIComponent(workId)}/renew`,
  );
}

export function useReleaseWorkItem(
  workId: string,
): UseMutationResult<ReleaseResult, Error, FencedCommand> {
  return useReviewCommandMutation<ReleaseResult, FencedCommand>(
    `/controlled/s01/api/commands/review-work-items/${encodeURIComponent(workId)}/release`,
  );
}

export function useSubmitVerification(
  workId: string,
): UseMutationResult<SubmitResult, Error, SubmitCommand> {
  return useReviewCommandMutation<SubmitResult, SubmitCommand>(
    `/controlled/s01/api/commands/review-work-items/${encodeURIComponent(workId)}/submit`,
  );
}

/**
 * The restricted reveal mutation.  Its success payload carries ``source_text``
 * that may exist only in the exact live panel state authorized to display it,
 * so the mutation never invalidates the S01 cache and retains nothing:
 * ``gcTime: 0`` drops the cached entry as soon as the panel resets it.
 */
export function useRevealFieldObservation(
  workId: string,
): UseMutationResult<RevealResult, Error, RevealCommand> {
  return useMutation({
    mutationFn: (command: RevealCommand) =>
      request<RevealResult>(
        `/controlled/s01/api/commands/review-work-items/${encodeURIComponent(workId)}/reveal-field-observation`,
        { method: "POST", body: JSON.stringify(command) },
      ),
    retry: false,
    gcTime: 0,
  });
}

/** The evidence correction command.  Acceptance invalidates the server-owned
 * S01 queries; the successor run converges through current-route/history.
 * The mutation keeps ``gcTime: 0`` so a correction's restricted ``raw``
 * cannot survive in the MutationCache after the panel resets it. */
export function useCorrectFieldObservation(
  workId: string,
): UseMutationResult<CorrectionResult, Error, CorrectionCommand> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (command: CorrectionCommand) =>
      request<CorrectionResult>(
        `/controlled/s01/api/commands/review-work-items/${encodeURIComponent(workId)}/correct-field-observation`,
        { method: "POST", body: JSON.stringify(command) },
      ),
    retry: false,
    gcTime: 0,
    // The invalidation must not be awaited inside the mutation settlement:
    // the accepted correction invalidates the old work item, and the panel's
    // own acceptance callback has to run against the still-live issued token
    // before that refetch lands, or the context guard would scrub the
    // settling command and lose the acceptance.
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["s01"] });
    },
  });
}

/**
 * The authoritative convergence predicate for an accepted evidence revision:
 * current-route and history must agree on exactly one server-current run at
 * or beyond that revision.  Currentness is server data, never a browser
 * timestamp or the newest array entry.  It is shared by the evidence
 * correction and the fulfilled supplement flows.
 */
export function evidenceRevisionConverged(
  route: CurrentRouteResponse | undefined,
  history: ApplicationHistoryResponse | undefined,
  acceptedEvidenceRevision: number,
): boolean {
  if (route === undefined || history === undefined) return false;
  if (route.evidence_revision < acceptedEvidenceRevision) return false;
  if (route.current_run_id === null || route.current_run_id === undefined) {
    return false;
  }
  if (history.current_run_id !== route.current_run_id) return false;
  const currentRuns = history.runs.filter((run) => run.current === true);
  if (currentRuns.length !== 1) return false;
  const currentRun = currentRuns[0];
  if (currentRun.run_id !== route.current_run_id) return false;
  if (currentRun.evidence_revision < acceptedEvidenceRevision) return false;
  return true;
}

/** The correction flow's named convergence predicate (generalized). */
export const correctionConverged = evidenceRevisionConverged;

/** The smallest explicit convergence outcome of an accepted evidence
 * correction: nothing accepted, still reconciling against the authoritative
 * reads, converged to the server-current successor/route, or the bounded
 * poll ended without convergence.  ``terminal`` means an authoritative read
 * definitively rejected (sanitized, never an elapsed-timeout claim);
 * ``timed_out`` is reserved for the attempt ceiling.  Currentness is never
 * derived locally; every state follows server data. */
export type CorrectionConvergence =
  | "idle"
  | "waiting"
  | "converged"
  | "timed_out"
  | "terminal";

/**
 * Convergence polling for an accepted evidence revision: while the accepted
 * revision has no server-current successor run, refetch only the
 * authoritative current-route and history queries and stop on the exact
 * convergence predicate, on unmount/context change, or on a definitive
 * terminal error.  Completion is never inferred from elapsed attempts; the
 * bounded ceiling only turns the outcome into an explicit ``timed_out``.
 * Shared by the evidence correction and fulfilled supplement flows.
 */
export function useEvidenceConvergence(
  applicationId: string | null,
  acceptedEvidenceRevision: number | null,
): CorrectionConvergence {
  const queryClient = useQueryClient();
  const [outcome, setOutcome] = useState<CorrectionConvergence>("idle");
  useEffect(() => {
    if (applicationId === null || acceptedEvidenceRevision === null) {
      setOutcome("idle");
      return;
    }
    setOutcome("waiting");
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let attempts = 0;
    const poll = async () => {
      if (cancelled) return;
      attempts += 1;
      await queryClient.refetchQueries({ queryKey: ROUTE_KEY(applicationId) });
      if (cancelled) return;
      await queryClient.refetchQueries({ queryKey: HISTORY_KEY(applicationId) });
      if (cancelled) return;
      const routeState = queryClient.getQueryState(ROUTE_KEY(applicationId));
      const historyState = queryClient.getQueryState(HISTORY_KEY(applicationId));
      const routeError = routeState?.error;
      const historyError = historyState?.error;
      const hasRouteError = routeError !== undefined && routeError !== null;
      const hasHistoryError = historyError !== undefined && historyError !== null;
      if (
        (hasRouteError && isDefinitiveRejection(routeError)) ||
        (hasHistoryError && isDefinitiveRejection(historyError))
      ) {
        setOutcome("terminal");
        return;
      }
      // Retained data is never current evidence while either authoritative
      // refetch is unavailable.  Transient errors remain waiting and consume
      // the same bounded retry budget below.
      if (!hasRouteError && !hasHistoryError) {
        const route = queryClient.getQueryData<CurrentRouteResponse>(
          ROUTE_KEY(applicationId),
        );
        const history = queryClient.getQueryData<ApplicationHistoryResponse>(
          HISTORY_KEY(applicationId),
        );
        if (evidenceRevisionConverged(route, history, acceptedEvidenceRevision)) {
          setOutcome("converged");
          return;
        }
      }
      // Safety ceiling only: the bounded end is surfaced, never converged.
      if (attempts >= 240) {
        setOutcome("timed_out");
        return;
      }
      timer = setTimeout(poll, 1_500);
    };
    void poll();
    return () => {
      cancelled = true;
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [applicationId, acceptedEvidenceRevision, queryClient]);
  return outcome;
}

/** The correction flow's named convergence polling (generalized). */
export const useCorrectionConvergence = useEvidenceConvergence;

export function useVerifyRecovery(
  workId: string,
): UseMutationResult<VerifyRecoveryResult, Error, VerifyRecoveryCommand> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (command: VerifyRecoveryCommand) =>
      request<VerifyRecoveryResult>(
        `/controlled/s01/api/commands/recovery-work-items/${encodeURIComponent(workId)}/verify`,
        { method: "POST", body: JSON.stringify(command) },
      ),
    retry: false,
    onSuccess: () =>
      // Returning the promise keeps the mutation pending until the
      // server-owned refetch converges, so the accepted latch cannot race.
      queryClient.invalidateQueries({ queryKey: ["s01"] }),
  });
}

/** The Reviewer's authoritative supplement request view for one request id. */
export function useSupplementRequest(
  requestId: string | null,
): UseQueryResult<SupplementRequestView> {
  return useQuery({
    queryKey: SUPPLEMENT_REQUEST_KEY(requestId ?? ""),
    enabled: requestId !== null,
    queryFn: () =>
      request<SupplementRequestView>(
        `/controlled/s01/api/queries/supplement-requests/${encodeURIComponent(requestId ?? "")}`,
      ),
    retry: retryPolicy,
  });
}

/** The Reviewer's supplement request command; acceptance invalidates the
 * server-owned S01 queries (the old work item then existence-hides). */
export function useRequestSupplement(
  workId: string,
): UseMutationResult<SupplementRequestResult, Error, SupplementRequestCommand> {
  return useReviewCommandMutation<SupplementRequestResult, SupplementRequestCommand>(
    `/controlled/s01/api/commands/review-work-items/${encodeURIComponent(workId)}/supplement`,
  );
}

/** The Integrator's minimized current request-binding projection. */
export function useIntegratorSupplementRequest(
  requestId: string | null,
): UseQueryResult<IntegratorSupplementRequestView> {
  return useQuery({
    queryKey: INTEGRATOR_REQUEST_KEY(requestId ?? ""),
    enabled: requestId !== null,
    queryFn: () =>
      request<IntegratorSupplementRequestView>(
        `/controlled/s02/api/queries/supplement-requests/${encodeURIComponent(requestId ?? "")}`,
      ),
    retry: retryPolicy,
  });
}

/** The attachment-version command for the Integrator; acceptance refetches
 * the S02 projection so the panel never infers request progress. */
export function useSubmitAttachmentVersion(): UseMutationResult<
  AttachmentSubmissionResponse,
  Error,
  AttachmentSubmissionCommand
> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (command: AttachmentSubmissionCommand) =>
      request<AttachmentSubmissionResponse>(
        "/controlled/s02/api/commands/submit-attachment-version",
        { method: "POST", body: JSON.stringify(command) },
      ),
    retry: false,
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["s02"] }),
  });
}
