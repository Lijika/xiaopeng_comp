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
  requestS14Command,
  HttpError,
  isDefinitiveRejection,
  isDefinitiveS08Rejection,
  isDefinitiveS12Rejection,
  type ApplicationHistoryResponse,
  type ClaimResult,
  type CurrentRouteResponse,
  type QueueResponse,
  type RecoveryWorkResponse,
  type ReleaseResult,
  type RenewResult,
  type ReviewWorkResponse,
  type S12BundleResponse,
  type S12JobResponse,
  type S12PlanCatalogResponse,
  type S12ProcessResponse,
  type S12StartJobBody,
  type S08ApproveResponse,
  type S08CancelResponse,
  type S08CandidateWorkspaceResponse,
  type S08FreezeCandidateResponse,
  type S08ImportLegacyResponse,
  type S08RejectResponse,
  type S08RequestValidationResponse,
  type S08ReviseDraftResponse,
  type S08ScheduleResponse,
  type S08StatusResponse,
  type S08SubmitReviewResponse,
  type S09GovernanceWorkspaceResponse,
  type S09ImposeHoldResponse,
  type S09ImpactDispositionsResponse,
  type S09PreviewResponse,
  type S09ProposeRollbackResponse,
  type S09RecoverHoldResponse,
  type SubmitResult,
  type VerifyRecoveryResult,
  type WorkspaceResponse,
} from "./client";

/** The restricted reveal and evidence-correction command results, bound to
 * the generated OpenAPI schemas (mirrors the sibling result aliases in
 * client.ts without extending that file). */
export type RevealResult = components["schemas"]["S01RevealResult"];
export type CorrectionResult = components["schemas"]["S01CorrectionResult"];
export type MembershipResult = components["schemas"]["S01MembershipCorrectionResult"];
export type EntityLinkResult =
  components["schemas"]["S01EntityLinkCorrectionResult"];
export type SupplementRequestResult =
  components["schemas"]["S01SupplementRequestResult"];
export type SupplementRequestView =
  components["schemas"]["S01SupplementRequestView"];
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
export type DemoFixturesResponse = components["schemas"]["DemoFixturesResponse"];
export type DemoCheckResponse = components["schemas"]["DemoCheckResponse"];
export type DemoBatchCheckResponse =
  components["schemas"]["DemoBatchCheckResponse"];
export type DemoBatchItem = components["schemas"]["DemoBatchItem"];
export type DemoEvaluationSummaryResponse =
  components["schemas"]["DemoEvaluationSummaryResponse"];

/**
 * The demo batch POST body is bound to the generated OpenAPI request schema;
 * a backend contract change fails strict typecheck here.
 */
export type DemoBatchCommand = paths["/api/demo/check/batch"]["post"]["requestBody"]["content"]["application/json"];

export const DEMO_FIXTURES_KEY = ["demo", "fixtures"] as const;
export const DEMO_EVAL_SUMMARY_KEY = ["demo", "evaluate-summary"] as const;

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
export const EXCEPTION_VIEW_KEY = (requestId: string) =>
  ["s05", "exception-request", requestId] as const;
export const S08_CANDIDATE_KEY = (candidateId: string) =>
  ["s08", "candidate-workspace", candidateId] as const;
export const S08_STATUS_KEY = ["s08", "status"] as const;
export const S09_WORKSPACE_KEY = ["s09", "workspace"] as const;
export const S09_RECONCILIATION_KEY = (digest: string) =>
  ["s09", "reconciliation", digest] as const;

/** The fixed governed scope of the C-DEMO workspace (server-owned fact, not
 * an input; the draft metadata editor repeats it for the published record). */
export const S08_SCOPE = "C-DEMO/demo";

/**
 * The S01 manual-review command bodies are bound to the generated OpenAPI
 * request schemas; a backend contract change fails strict typecheck here.
 */
export type ClaimCommand = paths["/controlled/s01/api/commands/review-work-items/{work_item_id}/claim"]["post"]["requestBody"]["content"]["application/json"];
export type FencedCommand = paths["/controlled/s01/api/commands/review-work-items/{work_item_id}/renew"]["post"]["requestBody"]["content"]["application/json"];
export type SubmitCommand = paths["/controlled/s01/api/commands/review-work-items/{work_item_id}/submit"]["post"]["requestBody"]["content"]["application/json"];
export type RevealCommand = paths["/controlled/s01/api/commands/review-work-items/{work_item_id}/reveal-field-observation"]["post"]["requestBody"]["content"]["application/json"];
export type CorrectionCommand = paths["/controlled/s01/api/commands/review-work-items/{work_item_id}/correct-field-observation"]["post"]["requestBody"]["content"]["application/json"];
export type MembershipCommand = paths["/controlled/s01/api/commands/review-work-items/{work_item_id}/correct-page-membership"]["post"]["requestBody"]["content"]["application/json"];
export type EntityLinkCommand = paths["/controlled/s01/api/commands/review-work-items/{work_item_id}/correct-entity-link"]["post"]["requestBody"]["content"]["application/json"];

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

/**
 * The T05 business-exception commands are bound to the generated OpenAPI
 * request schemas; a backend contract change fails strict typecheck here.
 */
export type ExceptionRequestCommand = NonNullable<paths["/controlled/s01/api/commands/review-work-items/{work_item_id}/business-exceptions"]["post"]["requestBody"]>["content"]["application/json"];
export type ExceptionClaimCommand = NonNullable<paths["/controlled/s01/api/commands/exception-work-items/{work_item_id}/claim"]["post"]["requestBody"]>["content"]["application/json"];
export type ExceptionDecisionCommand = NonNullable<paths["/controlled/s01/api/commands/business-exceptions/{request_id}/decide"]["post"]["requestBody"]>["content"]["application/json"];

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
 * The manual-review and S05 command hooks share one shape: a retry:false
 * POST through the thin same-origin adapter whose acceptance invalidates the
 * server-owned queries named by ``invalidateKeys``.  No optimistic
 * transition is ever applied.
 */
function useReviewCommandMutation<TResult, TCommand>(
  path: string,
  invalidateKeys: readonly string[] = ["s01"],
): UseMutationResult<TResult, Error, TCommand> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (command: TCommand) =>
      request<TResult>(path, { method: "POST", body: JSON.stringify(command) }),
    retry: false,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: invalidateKeys }),
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
 * The S10 page-membership correction command.  Acceptance invalidates the
 * server-owned S01 queries; the successor run converges through
 * current-route/history.  ``gcTime: 0`` so a correction cannot survive in the
 * MutationCache after the panel resets it.
 */
export function useCorrectPageMembership(
  workId: string,
): UseMutationResult<MembershipResult, Error, MembershipCommand> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (command: MembershipCommand) =>
      request<MembershipResult>(
        `/controlled/s01/api/commands/review-work-items/${encodeURIComponent(workId)}/correct-page-membership`,
        { method: "POST", body: JSON.stringify(command) },
      ),
    retry: false,
    gcTime: 0,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["s01"] });
    },
  });
}

/**
 * The S11 entity-link correction command.  Acceptance invalidates the
 * server-owned S01 queries; the successor run converges through
 * current-route/history.  The command carries only server-provided candidate
 * and authority values, so no restricted raw is retained: like the
 * membership correction it keeps the default MutationCache lifetime.
 */
export function useCorrectEntityLink(
  workId: string,
): UseMutationResult<EntityLinkResult, Error, EntityLinkCommand> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (command: EntityLinkCommand) =>
      request<EntityLinkResult>(
        `/controlled/s01/api/commands/review-work-items/${encodeURIComponent(workId)}/correct-entity-link`,
        { method: "POST", body: JSON.stringify(command) },
      ),
    retry: false,
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

/** The Exception Approver's minimized view of one business-exception request.
 * Existence-hiding 404 is never retried; only transient statuses are. */
export function useBusinessExceptionView(
  requestId: string | null,
): UseQueryResult<BusinessExceptionView> {
  return useQuery({
    queryKey: EXCEPTION_VIEW_KEY(requestId ?? ""),
    enabled: requestId !== null,
    queryFn: () =>
      request<BusinessExceptionView>(
        `/controlled/s01/api/queries/business-exceptions/${encodeURIComponent(requestId ?? "")}`,
      ),
    retry: retryPolicy,
  });
}

/** The Reviewer's business-exception request; acceptance refetches the
 * server-owned S01 reads (workspace, current-route, history). */
export function useRequestBusinessException(
  workId: string,
): UseMutationResult<BusinessExceptionRequestResult, Error, ExceptionRequestCommand> {
  return useReviewCommandMutation<
    BusinessExceptionRequestResult,
    ExceptionRequestCommand
  >(
    `/controlled/s01/api/commands/review-work-items/${encodeURIComponent(workId)}/business-exceptions`,
  );
}

/** The Exception Approver's claim command for one exception work item. */
export function useClaimExceptionWorkItem(
  workId: string,
): UseMutationResult<ExceptionClaimResult, Error, ExceptionClaimCommand> {
  return useReviewCommandMutation<ExceptionClaimResult, ExceptionClaimCommand>(
    `/controlled/s01/api/commands/exception-work-items/${encodeURIComponent(workId)}/claim`,
    ["s05"],
  );
}

/** The Exception Approver's approve/reject decision command. */
export function useDecideBusinessException(
  requestId: string,
): UseMutationResult<ExceptionDecisionResult, Error, ExceptionDecisionCommand> {
  return useReviewCommandMutation<ExceptionDecisionResult, ExceptionDecisionCommand>(
    `/controlled/s01/api/commands/business-exceptions/${encodeURIComponent(requestId)}/decide`,
    ["s05"],
  );
}

/** The closed demo option list; never retried beyond the shared policy. */
export function useDemoFixtures(): UseQueryResult<DemoFixturesResponse> {
  return useQuery({
    queryKey: DEMO_FIXTURES_KEY,
    queryFn: () => request<DemoFixturesResponse>("/api/demo/fixtures"),
    retry: retryPolicy,
  });
}

/** The single explicit-click demo check mutation.  retry:false — a demo
 * check never replays from a mount or StrictMode effect. */
export function useDemoCheck(): UseMutationResult<
  DemoCheckResponse,
  Error,
  string
> {
  return useMutation({
    mutationFn: (fixtureId: string) =>
      request<DemoCheckResponse>("/api/demo/check", {
        method: "POST",
        body: JSON.stringify({ fixture_id: fixtureId }),
      }),
    retry: false,
  });
}

/**
 * The bounded synchronous batch mutation.  It sends fixture ids only and is
 * never retried: a batch runs once per explicit user action and cannot
 * replay from a mount or StrictMode effect.
 */
export function useDemoBatchCheck(): UseMutationResult<
  DemoBatchCheckResponse,
  Error,
  DemoBatchCommand
> {
  return useMutation({
    mutationFn: (command: DemoBatchCommand) =>
      request<DemoBatchCheckResponse>("/api/demo/check/batch", {
        method: "POST",
        body: JSON.stringify(command),
      }),
    retry: false,
  });
}

/**
 * The read-only fixed-main evaluation summary.  ``enabled: false`` makes the
 * explicit load click the only trigger, and ``retry: false`` means a failed
 * read never replays on its own.
 */
export function useDemoEvaluationSummary(): UseQueryResult<DemoEvaluationSummaryResponse> {
  return useQuery({
    queryKey: DEMO_EVAL_SUMMARY_KEY,
    queryFn: () =>
      request<DemoEvaluationSummaryResponse>("/api/demo/evaluate/summary"),
    enabled: false,
    retry: false,
  });
}

/**
 * The S08 command bodies are bound to the generated OpenAPI request schemas;
 * a backend contract change fails strict typecheck here.
 */
export type S08ImportCommand = paths["/controlled/s08/api/commands/import_legacy"]["post"]["requestBody"]["content"]["application/json"];
export type S08ReviseCommand = paths["/controlled/s08/api/commands/revise_draft"]["post"]["requestBody"]["content"]["application/json"];
export type S08FreezeCommand = paths["/controlled/s08/api/commands/freeze_candidate"]["post"]["requestBody"]["content"]["application/json"];
export type S08ValidationCommand = paths["/controlled/s08/api/commands/request_validation"]["post"]["requestBody"]["content"]["application/json"];
export type S08SubmitReviewCommand = paths["/controlled/s08/api/commands/submit_review"]["post"]["requestBody"]["content"]["application/json"];
export type S08ApproveCommand = paths["/controlled/s08/api/commands/approve"]["post"]["requestBody"]["content"]["application/json"];
export type S09PreviewCommand = paths["/controlled/s09/api/commands/preview_impact"]["post"]["requestBody"]["content"]["application/json"];
export type S08RejectCommand = paths["/controlled/s08/api/commands/reject"]["post"]["requestBody"]["content"]["application/json"];
export type S08ScheduleCommand = paths["/controlled/s08/api/commands/schedule"]["post"]["requestBody"]["content"]["application/json"];
export type S08CancelCommand = paths["/controlled/s08/api/commands/cancel"]["post"]["requestBody"]["content"]["application/json"];
export type S09ImposeHoldCommand = paths["/controlled/s09/api/commands/impose_hold"]["post"]["requestBody"]["content"]["application/json"];
export type S09ProposeRollbackCommand = paths["/controlled/s09/api/commands/propose_rollback"]["post"]["requestBody"]["content"]["application/json"];
export type S09RecoverHoldCommand = paths["/controlled/s09/api/commands/recover_hold"]["post"]["requestBody"]["content"]["application/json"];

/**
 * The authoritative S08 governance revision for the draft workflow.  The
 * Admin-only status query is the only place the revision is minted before a
 * candidate exists; every draft command fences on it.  Existence-hiding 404
 * and other client errors are never retried; only transient statuses are.
 */
export function useS08Status(): UseQueryResult<S08StatusResponse> {
  return useQuery({
    queryKey: S08_STATUS_KEY,
    queryFn: () =>
      request<S08StatusResponse>(
        `/controlled/s08/api/queries/status?scope=${encodeURIComponent(S08_SCOPE)}`,
      ),
    retry: retryPolicy,
  });
}

/**
 * The one authoritative S08 candidate workspace read.  The server owns the
 * candidate status, digests, actions and the governance revision; the panel
 * never derives transition rights locally.  Existence-hiding 404 and other
 * client errors are never retried; only transient server statuses are.
 */
export function useCandidateWorkspace(
  candidateId: string | null,
): UseQueryResult<S08CandidateWorkspaceResponse> {
  return useQuery({
    queryKey: S08_CANDIDATE_KEY(candidateId ?? ""),
    enabled: candidateId !== null,
    queryFn: () =>
      request<S08CandidateWorkspaceResponse>(
        `/controlled/s08/api/queries/candidate/${encodeURIComponent(candidateId ?? "")}`,
      ),
    retry: retryPolicy,
  });
}

/**
 * The S08 command hooks share the review-command shape: a retry:false POST
 * through the thin same-origin adapter whose acceptance invalidates the
 * server-owned S08 queries (the candidate workspace first of all).  No
 * optimistic transition is ever applied, and the panel supplies the latest
 * authoritative ``expected_governance_revision`` from the workspace.
 */
function useS08CommandMutation<TResult, TCommand>(
  path: string,
): UseMutationResult<TResult, Error, TCommand> {
  return useReviewCommandMutation<TResult, TCommand>(path, ["s08"]);
}

export function useImportLegacy(): UseMutationResult<
  S08ImportLegacyResponse,
  Error,
  S08ImportCommand
> {
  return useS08CommandMutation<S08ImportLegacyResponse, S08ImportCommand>(
    "/controlled/s08/api/commands/import_legacy",
  );
}

export function useReviseDraft(): UseMutationResult<
  S08ReviseDraftResponse,
  Error,
  S08ReviseCommand
> {
  return useS08CommandMutation<S08ReviseDraftResponse, S08ReviseCommand>(
    "/controlled/s08/api/commands/revise_draft",
  );
}

export function useFreezeCandidate(): UseMutationResult<
  S08FreezeCandidateResponse,
  Error,
  S08FreezeCommand
> {
  return useS08CommandMutation<S08FreezeCandidateResponse, S08FreezeCommand>(
    "/controlled/s08/api/commands/freeze_candidate",
  );
}

export function useRequestValidation(): UseMutationResult<
  S08RequestValidationResponse,
  Error,
  S08ValidationCommand
> {
  return useS08CommandMutation<
    S08RequestValidationResponse,
    S08ValidationCommand
  >("/controlled/s08/api/commands/request_validation");
}

export function useSubmitReview(): UseMutationResult<
  S08SubmitReviewResponse,
  Error,
  S08SubmitReviewCommand
> {
  return useS08CommandMutation<S08SubmitReviewResponse, S08SubmitReviewCommand>(
    "/controlled/s08/api/commands/submit_review",
  );
}

export function useApproveCandidate(): UseMutationResult<
  S08ApproveResponse,
  Error,
  S08ApproveCommand
> {
  return useS08CommandMutation<S08ApproveResponse, S08ApproveCommand>(
    "/controlled/s08/api/commands/approve",
  );
}

export function usePreviewImpact(): UseMutationResult<
  S09PreviewResponse,
  Error,
  S09PreviewCommand
> {
  // The impact preview appends one immutable governance fact
  // (impact_previewed) and returns the fresh governance revision the
  // approval must fence on.  It must not invalidate the S08 workspace
  // query: an invalidating mutation would block its onSuccess on the
  // refetch, and the workspace revision is stale for the approval anyway.
  return useMutation({
    mutationFn: (command: S09PreviewCommand) =>
      request<S09PreviewResponse>("/controlled/s09/api/commands/preview_impact", {
        method: "POST",
        body: JSON.stringify(command),
      }),
    retry: false,
  });
}

export function useRejectCandidate(): UseMutationResult<
  S08RejectResponse,
  Error,
  S08RejectCommand
> {
  return useS08CommandMutation<S08RejectResponse, S08RejectCommand>(
    "/controlled/s08/api/commands/reject",
  );
}

export function useScheduleActivation(): UseMutationResult<
  S08ScheduleResponse,
  Error,
  S08ScheduleCommand
> {
  return useS08CommandMutation<S08ScheduleResponse, S08ScheduleCommand>(
    "/controlled/s08/api/commands/schedule",
  );
}

export function useCancelCandidate(): UseMutationResult<
  S08CancelResponse,
  Error,
  S08CancelCommand
> {
  return useS08CommandMutation<S08CancelResponse, S08CancelCommand>(
    "/controlled/s08/api/commands/cancel",
  );
}

export type S08StatusPollOutcome =
  | "idle"
  | "waiting"
  | "converged"
  | "timed_out"
  | "terminal";

/**
 * Bounded status polling for an asynchronous S08 owner transition
 * (validation and activation are background jobs).  While ``active``, only
 * the authoritative candidate workspace is refetched, at most
 * ``maxAttempts`` times; the poll converges on the exact server status set,
 * turns terminal on a definitive rejection or on the caller's
 * ``alsoTerminal`` predicate (e.g. an activation outcome that ended
 * diagnostic), and never claims convergence from an elapsed-timeout
 * (``timed_out`` is the bounded end, never ``converged``).
 */
export function useS08StatusPoll(
  candidateId: string | null,
  active: boolean,
  terminalStatuses: readonly string[],
  options: {
    intervalMs?: number;
    maxAttempts?: number;
    alsoTerminal?: (workspace: S08CandidateWorkspaceResponse) => boolean;
  } = {},
): S08StatusPollOutcome {
  const { intervalMs = 1_500, maxAttempts = 80, alsoTerminal } = options;
  const queryClient = useQueryClient();
  const [outcome, setOutcome] = useState<S08StatusPollOutcome>("idle");
  useEffect(() => {
    if (candidateId === null || !active) {
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
      await queryClient.refetchQueries({
        queryKey: S08_CANDIDATE_KEY(candidateId),
      });
      if (cancelled) return;
      const state = queryClient.getQueryState(S08_CANDIDATE_KEY(candidateId));
      const error = state?.error;
      const hasError = error !== undefined && error !== null;
      if (hasError && isDefinitiveS08Rejection(error)) {
        setOutcome("terminal");
        return;
      }
      if (!hasError) {
        const data = queryClient.getQueryData<S08CandidateWorkspaceResponse>(
          S08_CANDIDATE_KEY(candidateId),
        );
        if (data !== undefined) {
          if (terminalStatuses.includes(data.status)) {
            setOutcome("converged");
            return;
          }
          if (alsoTerminal !== undefined && alsoTerminal(data)) {
            setOutcome("terminal");
            return;
          }
        }
      }
      if (attempts >= maxAttempts) {
        setOutcome("timed_out");
        return;
      }
      timer = setTimeout(poll, intervalMs);
    };
    void poll();
    return () => {
      cancelled = true;
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [
    candidateId,
    active,
    terminalStatuses.join(","),
    intervalMs,
    maxAttempts,
    alsoTerminal,
    queryClient,
  ]);
  return outcome;
}

/**
 * The authoritative T09 governance workspace read.  The server owns the
 * actor role, the action list, the active release, the recovery anchor, the
 * active hold union and the governance revision; the panel never derives a
 * transition or a hold.  Existence-hiding 404 and other client errors are
 * never retried; only transient server statuses are.
 */
export function useS09Workspace(): UseQueryResult<S09GovernanceWorkspaceResponse> {
  return useQuery({
    queryKey: S09_WORKSPACE_KEY,
    queryFn: () =>
      request<S09GovernanceWorkspaceResponse>(
        "/controlled/s09/api/queries/workspace",
      ),
    retry: retryPolicy,
  });
}

/**
 * The T09 command hooks share the review-command shape: a retry:false POST
 * through the thin same-origin adapter whose acceptance invalidates the
 * server-owned S09 queries (the workspace first of all).  No optimistic
 * transition is ever applied, and the panel supplies the latest
 * authoritative ``expected_governance_revision`` from the workspace.
 */
function useS09CommandMutation<TResult, TCommand>(
  path: string,
): UseMutationResult<TResult, Error, TCommand> {
  return useReviewCommandMutation<TResult, TCommand>(path, ["s09"]);
}

export function useImposeHold(): UseMutationResult<
  S09ImposeHoldResponse,
  Error,
  S09ImposeHoldCommand
> {
  return useS09CommandMutation<S09ImposeHoldResponse, S09ImposeHoldCommand>(
    "/controlled/s09/api/commands/impose_hold",
  );
}

export function useProposeRollback(): UseMutationResult<
  S09ProposeRollbackResponse,
  Error,
  S09ProposeRollbackCommand
> {
  return useS09CommandMutation<
    S09ProposeRollbackResponse,
    S09ProposeRollbackCommand
  >("/controlled/s09/api/commands/propose_rollback");
}

export function useRecoverHold(): UseMutationResult<
  S09RecoverHoldResponse,
  Error,
  S09RecoverHoldCommand
> {
  return useS09CommandMutation<S09RecoverHoldResponse, S09RecoverHoldCommand>(
    "/controlled/s09/api/commands/recover_hold",
  );
}

/**
 * The Auditor's per-member impact-disposition receipts for one final impact
 * digest.  Only the registered auditor identity can read this route; the
 * panel enables it exactly for the auditor role and an active final impact.
 * The route is owned by the S01 Lifecycle reconciliation contract — S09 only
 * gates it to the auditor read surface; the S01 authority stays untouched.
 * Existence-hiding 404 and other client errors are never retried; only
 * transient server statuses are.
 */
export function useImpactReconciliation(
  digest: string | null,
): UseQueryResult<S09ImpactDispositionsResponse> {
  return useQuery({
    queryKey: S09_RECONCILIATION_KEY(digest ?? ""),
    enabled: digest !== null,
    queryFn: () =>
      request<S09ImpactDispositionsResponse>(
        `/controlled/s01/api/queries/impact-dispositions/reconciliation?final_impact_digest=${encodeURIComponent(digest ?? "")}`,
      ),
    retry: retryPolicy,
  });
}

// ---------------------------------------------------------------------------
// T14 — S12 Evaluation Operator workflow
// ---------------------------------------------------------------------------

export const S12_PLANS_KEY = ["s12", "plans"] as const;
export const S12_JOB_KEY = (jobId: string) => ["s12", "job", jobId] as const;
export const S12_BUNDLE_KEY = (bundleId: string) =>
  ["s12", "bundle", bundleId] as const;

/** The durable job lifecycle terminal statuses.  These are server-owned
 * values rendered exactly; the result class (INVALID / INSUFFICIENT / FAIL /
 * scoped PASS / SMOKE_ONLY) is never derived from them. */
export const S12_TERMINAL_JOB_STATUSES: readonly string[] = [
  "complete",
  "failed",
  "cancelled",
  "diagnostic",
];

/**
 * The read-only frozen-plan catalog.  Only selection metadata is exposed;
 * start resolves the frozen row again server-side.  No hidden retry: a
 * failed closed read stays failed so the operator surface never guesses at
 * plan availability.
 */
export function useS12Plans(): UseQueryResult<S12PlanCatalogResponse> {
  return useQuery({
    queryKey: S12_PLANS_KEY,
    queryFn: () => request<S12PlanCatalogResponse>("/controlled/s12/plans"),
    retry: false,
    refetchOnWindowFocus: false,
  });
}

export interface S12StartProcessResult {
  job: S12JobResponse;
  process: S12ProcessResponse;
}

export type S12JobStartedCallback = (job: S12JobResponse) => void;

/**
 * One operator action creates one durable job: the start body is the closed
 * ``{plan_id}`` DTO and the returned job is processed exactly once.  The
 * mutation never retries (a lost response never creates a replacement job)
 * and the UI polls the original job id afterwards.
 */
export function useS12StartProcess(
  onJobStarted?: S12JobStartedCallback,
): UseMutationResult<
  S12StartProcessResult,
  Error,
  S12StartJobBody
> {
  return useMutation({
    mutationFn: async (body: S12StartJobBody): Promise<S12StartProcessResult> => {
      const job = await request<S12JobResponse>("/controlled/s12/jobs/start", {
        method: "POST",
        body: JSON.stringify(body),
      });
      onJobStarted?.(job);
      const process = await request<S12ProcessResponse>(
        `/controlled/s12/jobs/${encodeURIComponent(job.job_id)}/process`,
        { method: "POST" },
      );
      return { job, process };
    },
    retry: false,
  });
}

/**
 * The one authoritative durable job read.  ``retry: false`` keeps the fixed
 * polling budget exact: hidden query retries are disabled so every job GET
 * counts against the bounded cycle limit.
 */
export function useS12Job(jobId: string | null): UseQueryResult<S12JobResponse> {
  return useQuery({
    queryKey: S12_JOB_KEY(jobId ?? ""),
    enabled: jobId !== null,
    queryFn: () =>
      request<S12JobResponse>(
        `/controlled/s12/jobs/${encodeURIComponent(jobId ?? "")}`,
      ),
    retry: false,
    refetchOnWindowFocus: false,
  });
}

export type S12JobPollOutcome =
  | "idle"
  | "waiting"
  | "terminal"
  | "timed_out";

/**
 * Bounded polling for one durable S12 job: one job GET per second for at
 * most ``maxAttempts`` cycles (production default 120).  Terminal lifecycle
 * statuses and definitive closed rejections stop the poll immediately;
 * ``timed_out`` is the explicit bounded/unknown end and never claims a
 * terminal report.  The caller never creates another job or execution
 * request from this outcome.
 */
export function useS12JobPoll(
  jobId: string | null,
  active: boolean,
  options: { intervalMs?: number; maxAttempts?: number } = {},
): S12JobPollOutcome {
  const { intervalMs = 1000, maxAttempts = 120 } = options;
  const queryClient = useQueryClient();
  const [outcome, setOutcome] = useState<S12JobPollOutcome>("idle");
  useEffect(() => {
    if (jobId === null || !active) {
      setOutcome("idle");
      return;
    }
    setOutcome("waiting");
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let attempts = 0;
    const poll = async () => {
      if (cancelled) return;
      const cached = queryClient.getQueryData<S12JobResponse>(
        S12_JOB_KEY(jobId),
      );
      if (
        cached !== undefined &&
        S12_TERMINAL_JOB_STATUSES.includes(cached.status)
      ) {
        setOutcome("terminal");
        return;
      }
      attempts += 1;
      await queryClient.refetchQueries({
        queryKey: S12_JOB_KEY(jobId),
      });
      if (cancelled) return;
      const state = queryClient.getQueryState(S12_JOB_KEY(jobId));
      const error = state?.error;
      const hasError = error !== undefined && error !== null;
      if (hasError && isDefinitiveS12Rejection(error)) {
        setOutcome("terminal");
        return;
      }
      if (!hasError) {
        const data = queryClient.getQueryData<S12JobResponse>(
          S12_JOB_KEY(jobId),
        );
        if (data !== undefined && S12_TERMINAL_JOB_STATUSES.includes(data.status)) {
          setOutcome("terminal");
          return;
        }
      }
      if (attempts >= maxAttempts) {
        setOutcome("timed_out");
        return;
      }
      timer = setTimeout(poll, intervalMs);
    };
    timer = setTimeout(poll, intervalMs);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [jobId, active, intervalMs, maxAttempts, queryClient]);
  return outcome;
}

/**
 * The immutable content-addressed bundle read, enabled only after the
 * terminal job exposes a ``bundle_id``.  The browser performs GET reads only
 * and renders the returned sealed bundle without modification.
 */
export function useS12Bundle(
  bundleId: string | null,
): UseQueryResult<S12BundleResponse> {
  return useQuery({
    queryKey: S12_BUNDLE_KEY(bundleId ?? ""),
    enabled: bundleId !== null,
    queryFn: () =>
      request<S12BundleResponse>(
        `/controlled/s12/bundles/${encodeURIComponent(bundleId ?? "")}`,
      ),
    retry: false,
    refetchOnWindowFocus: false,
  });
}

// ---------------------------------------------------------------------------
// T15 — S13 Delivery Operator workflow
// ---------------------------------------------------------------------------

export type S13QueryResponse = components["schemas"]["S13QueryResponse"];

export const S13_DELIVERY_KEY = (applicationId: string) =>
  ["s13", "delivery", applicationId] as const;

/**
 * The one authoritative S13 delivery query.  The server owns
 * Verification Completed, Verification Routing, obligation, and delivery
 * receipt; the browser performs GET reads only and refetches only from
 * explicit presentation controls.  The query is enabled only when the application
 * identifier is present; operator authorization is server-owned and a
 * 403/404 closes with no fallback.
 */
export function useS13Delivery(
  applicationId: string | null,
): UseQueryResult<S13QueryResponse> {
  return useQuery({
    queryKey: S13_DELIVERY_KEY(applicationId ?? ""),
    enabled: Boolean(applicationId),
    queryFn: () =>
      request<S13QueryResponse>(
        `/controlled/s13/delivery/${encodeURIComponent(applicationId ?? "")}`,
      ),
    retry: retryPolicy,
  });
}

// ---------------------------------------------------------------------------
// T16 — S14 lifecycle cancellation / settlement workflow
// ---------------------------------------------------------------------------

export type S14CommandResult = components["schemas"]["S14CommandResult"];

/**
 * The S14 command bodies are bound to the generated OpenAPI request schemas;
 * a backend contract change fails strict typecheck here.
 */
export type S14CancelCommand =
  paths["/controlled/s01/api/commands/applications/{application_id}/cancel"]["post"]["requestBody"]["content"]["application/json"];
export type S14SettleCommand =
  paths["/controlled/s01/api/commands/applications/{application_id}/settle-termination"]["post"]["requestBody"]["content"]["application/json"];
export type S14GrantPermissionCommand =
  paths["/controlled/s01/api/commands/applications/{application_id}/grant-reopen-permission"]["post"]["requestBody"]["content"]["application/json"];
export type S14ReopenCommand =
  paths["/controlled/s01/api/commands/applications/{application_id}/reopen"]["post"]["requestBody"]["content"]["application/json"];

function s14CommandPath(applicationId: string, action: string): string {
  return `/controlled/s01/api/commands/applications/${encodeURIComponent(
    applicationId,
  )}/${action}`;
}

/**
 * The S14 command hooks share one shape: a retry:false POST through the
 * same-origin adapter that resolves the closed typed envelope for every
 * registered domain outcome (accepted, replayed, outstanding, terminated,
 * stale, rejected, unavailable) and invalidates the authoritative S01 reads
 * so the UI converges on server facts only.  An unknown transport outcome
 * never retries and never rotates the caller's idempotency key.
 */
function useS14CommandMutation<TCommand>(
  path: string,
): UseMutationResult<S14CommandResult, Error, TCommand> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (command: TCommand) =>
      requestS14Command<S14CommandResult>(path, {
        method: "POST",
        body: JSON.stringify(command),
      }),
    retry: false,
    // Both authoritative read seams observe S14 outcomes: the integrator's
    // current-route/history under s01 and the operator's S13 delivery view.
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["s01"] });
      void queryClient.invalidateQueries({ queryKey: ["s13"] });
    },
  });
}

/** The authorized integrator's explicit cancellation command. */
export function useS14Cancel(
  applicationId: string,
): UseMutationResult<S14CommandResult, Error, S14CancelCommand> {
  return useS14CommandMutation<S14CancelCommand>(
    s14CommandPath(applicationId, "cancel"),
  );
}

/** The authorized operator's explicit termination settlement command. */
export function useS14Settle(
  applicationId: string,
): UseMutationResult<S14CommandResult, Error, S14SettleCommand> {
  return useS14CommandMutation<S14SettleCommand>(
    s14CommandPath(applicationId, "settle-termination"),
  );
}

/** The authorized operator's governed reopen permission grant. */
export function useS14GrantReopenPermission(
  applicationId: string,
): UseMutationResult<S14CommandResult, Error, S14GrantPermissionCommand> {
  return useS14CommandMutation<S14GrantPermissionCommand>(
    s14CommandPath(applicationId, "grant-reopen-permission"),
  );
}

/** The authorized operator's explicit successor-cycle reopen command. */
export function useS14Reopen(
  applicationId: string,
): UseMutationResult<S14CommandResult, Error, S14ReopenCommand> {
  return useS14CommandMutation<S14ReopenCommand>(
    s14CommandPath(applicationId, "reopen"),
  );
}

/** One durable termination-notification delivery per explicit action. */
export function useS14ProcessNotification(): UseMutationResult<
  S14CommandResult,
  Error,
  void
> {
  return useS14CommandMutation<void>(
    "/controlled/s01/api/commands/process-termination-notification",
  );
}

/** The bounded termination convergence outcomes.  ``terminated`` is only
 * ever reported from the authoritative current-route phase; ``timed_out`` is
 * the explicit bounded unknown and never claims termination; the poll count
 * and elapsed time are never facts. */
export type TerminationConvergence =
  | "idle"
  | "waiting"
  | "terminated"
  | "timed_out";

/**
 * Bounded reconciliation polling while a cancelled cycle stays
 * ``Terminating``: refetch only the authoritative current-route and history
 * queries and stop on the server-owned ``Terminated`` phase, a definitive
 * read rejection, unmount/context change, or the attempt ceiling.  The
 * ceiling surfaces an explicit ``timed_out`` (reconciliation needed); it
 * never derives termination from attempts or elapsed time.
 */
export function useTerminationConvergence(
  applicationId: string | null,
  active: boolean,
  options: { intervalMs?: number; maxAttempts?: number } = {},
): TerminationConvergence {
  const { intervalMs = 1_500, maxAttempts = 240 } = options;
  const queryClient = useQueryClient();
  const [outcome, setOutcome] = useState<TerminationConvergence>("idle");
  useEffect(() => {
    if (applicationId === null || !active) {
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
      await queryClient.refetchQueries({
        queryKey: HISTORY_KEY(applicationId),
      });
      if (cancelled) return;
      const routeState = queryClient.getQueryState(ROUTE_KEY(applicationId));
      const routeError = routeState?.error;
      const hasRouteError = routeError !== undefined && routeError !== null;
      if (hasRouteError && isDefinitiveRejection(routeError)) {
        setOutcome("timed_out");
        return;
      }
      if (!hasRouteError) {
        const route = queryClient.getQueryData<CurrentRouteResponse>(
          ROUTE_KEY(applicationId),
        );
        if (route?.phase === "Terminated") {
          setOutcome("terminated");
          return;
        }
      }
      if (attempts >= maxAttempts) {
        setOutcome("timed_out");
        return;
      }
      timer = setTimeout(poll, intervalMs);
    };
    void poll();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [applicationId, active, intervalMs, maxAttempts, queryClient]);
  return outcome;
}
