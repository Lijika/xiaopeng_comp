import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";

import {
  request,
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

export interface VerifyRecoveryCommand {
  expected_lifecycle_revision: number;
  expected_criterion_digest: string;
  idempotency_key: string;
}

export function useQueue(): UseQueryResult<QueueResponse> {
  return useQuery({
    queryKey: QUEUE_KEY,
    queryFn: () => request<QueueResponse>("/controlled/s01/api/queries/queue"),
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
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["s01"] });
    },
  });
}
