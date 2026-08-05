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
  type CurrentRouteResponse,
  type QueueResponse,
  type RecoveryWorkResponse,
  type VerifyRecoveryResult,
} from "./client";

export const QUEUE_KEY = ["s01", "queue"] as const;
export const WORK_KEY = (workId: string) =>
  ["s01", "recovery-work", workId] as const;
export const ROUTE_KEY = (applicationId: string) =>
  ["s01", "current-route", applicationId] as const;

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
  applicationId: string,
): UseQueryResult<CurrentRouteResponse> {
  return useQuery({
    queryKey: ROUTE_KEY(applicationId),
    queryFn: () =>
      request<CurrentRouteResponse>(
        `/controlled/s01/api/queries/applications/${encodeURIComponent(applicationId)}/current-route`,
      ),
    retry: retryPolicy,
  });
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
