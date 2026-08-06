import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";

import type { paths } from "../generated/api";
import {
  request,
  HttpError,
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

/**
 * The S01 manual-review command bodies are bound to the generated OpenAPI
 * request schemas; a backend contract change fails strict typecheck here.
 */
export type ClaimCommand = paths["/controlled/s01/api/commands/review-work-items/{work_item_id}/claim"]["post"]["requestBody"]["content"]["application/json"];
export type FencedCommand = paths["/controlled/s01/api/commands/review-work-items/{work_item_id}/renew"]["post"]["requestBody"]["content"]["application/json"];
export type SubmitCommand = paths["/controlled/s01/api/commands/review-work-items/{work_item_id}/submit"]["post"]["requestBody"]["content"]["application/json"];

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
