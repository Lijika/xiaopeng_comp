import { useEffect, useRef, useState } from "react";

import {
  HttpError,
  isDefinitiveS08Rejection,
  type S08CandidateWorkspaceResponse,
  type S09PreviewResponse,
  type S09ProposeRollbackResponse,
} from "../api/client";
import {
  useApproveCandidate,
  useCancelCandidate,
  useCandidateWorkspace,
  useFreezeCandidate,
  useImpactReconciliation,
  useImposeHold,
  useImportLegacy,
  useProposeRollback,
  useRejectCandidate,
  useRecoverHold,
  useRequestValidation,
  useReviseDraft,
  useS08Status,
  useS08StatusPoll,
  useS09Workspace,
  useScheduleActivation,
  useSubmitReview,
  type S08ApproveCommand,
  type S08CancelCommand,
  type S08FreezeCommand,
  type S08ImportCommand,
  type S08RejectCommand,
  type S08ReviseCommand,
  type S08ScheduleCommand,
  type S08SubmitReviewCommand,
  type S08ValidationCommand,
  usePreviewImpact,
  type S09ImposeHoldCommand,
  type S09PreviewCommand,
  type S09ProposeRollbackCommand,
  type S09RecoverHoldCommand,
} from "../api/hooks";
import { Button } from "./ui/button";

export interface PolicyReleasePanelProps {
  candidateId: string | null;
  onCandidateSelected?: (candidateId: string) => void;
}

/** One locked sent command, the only release identity the panel owns.
 * ``key`` is a browser-native ``crypto.randomUUID()`` so a remount or a
 * second candidate can never reuse a key already bound in the service ledger;
 * ``body`` is the exact serialized POST (key and revision included) so an
 * explicit retry replays byte-identical bytes.  ``outcome`` is ``in_flight``
 * while the mutation runs, ``unknown`` after a transport or non-deterministic
 * 5xx result, and ``final`` only after a definitive authoritative response
 * (acceptance or a registered rejection) for the exact sent operation.  A
 * global ledger revision advancing -- even to an authoritative refresh -- is
 * never proof that this command settled, so it never releases the latch. */
interface CommandLatch {
  action: string;
  key: string;
  body: string;
  sentRevision: number;
  outcome: "in_flight" | "unknown" | "final";
}

/** The command-latch surface the panel owns exactly once, at the
 * ``PolicyReleasePanel`` root: every draft and workspace command shares one
 * latch, so an unknown or in-flight command disables every mutation on the
 * page and only the exact retry may proceed. */
interface CommandLatchApi {
  latch: CommandLatch | null;
  lock: (
    action: string,
    command: Record<string, unknown>,
    revision: number,
  ) => { body: string } | null;
  markInFlight: () => void;
  markUnknown: () => void;
  markFinal: () => void;
}

/** One sent attempt registered by the section that owns the sending
 * mutation: ``send`` replays the exact locked bytes through that mutation,
 * while ``onSuccess``/``onError`` run the owning section's settlement
 * handling (its own notice/conflict/error state). */
interface AttemptRecord {
  send: (
    body: string,
    callbacks: {
      onSuccess?: (result: unknown) => void;
      onError?: (error: unknown) => void;
    },
  ) => void;
  onSuccess?: (result: unknown) => void;
  onError?: (error: unknown) => void;
}

/**
 * The panel's sent-command latch.  While a latch is not final no other
 * command may be minted: input changes and action switches neither release
 * nor rotate the identity, and only the exact same action with the exact
 * same serialized body may be re-sent (a byte-identical replay on the same
 * key).  A definitive rejection or acceptance of that exact operation marks
 * the latch final; an authoritative refresh may update display state but
 * never releases the latch, because an unrelated ledger advance is not proof
 * that the locked command settled.
 */
function useCommandLatch() {
  const [latch, setLatch] = useState<CommandLatch | null>(null);

  const lock = (
    action: string,
    command: Record<string, unknown>,
    revision: number,
  ): { body: string } | null => {
    if (latch !== null && latch.outcome !== "final") {
      // Unsettled: only the byte-identical replay of the locked command is
      // permitted; anything else is refused (the UI also disables it).
      const candidate = JSON.stringify({
        ...command,
        idempotency_key: latch.key,
        expected_governance_revision: latch.sentRevision,
      });
      if (latch.action === action && latch.body === candidate) {
        return { body: latch.body };
      }
      return null;
    }
    const key = crypto.randomUUID();
    const body = JSON.stringify({
      ...command,
      idempotency_key: key,
      expected_governance_revision: revision,
    });
    setLatch({ action, key, body, sentRevision: revision, outcome: "in_flight" });
    return { body };
  };

  const markUnknown = () =>
    setLatch((current) =>
      current === null || current.outcome === "final"
        ? current
        : { ...current, outcome: "unknown" },
    );

  const markInFlight = () =>
    setLatch((current) =>
      current === null || current.outcome === "final"
        ? current
        : { ...current, outcome: "in_flight" },
    );

  const markFinal = () =>
    setLatch((current) =>
      current === null ? current : { ...current, outcome: "final" },
    );

  return {
    latch,
    lock,
    markInFlight,
    markUnknown,
    markFinal,
  } satisfies CommandLatchApi;
}

/** Stable server-reason text for a structured S08 rejection, with the exact
 * registered code; never an internal path or raw exception. */
function rejectionText(error: unknown): string {
  if (error instanceof HttpError) {
    return error.message;
  }
  return "请求未完成，请稍后重试";
}

/** Categorized command-rejection text: explicit forbidden / unavailable
 * states for the closed S08 codes, the stable server reason for any other
 * definitive rejection (404/409/422), and the unknown-outcome message (the
 * exact recovery-panel wording) for transport or non-definitive results
 * that may have committed an effect. */
function commandRejectionText(error: unknown): string {
  if (error instanceof HttpError) {
    if (error.status === 403) return "无权限执行此操作";
    if (error.status === 503 && isDefinitiveS08Rejection(error)) {
      return "治理服务暂不可用";
    }
    if (isDefinitiveS08Rejection(error)) return error.message;
    return "结果未知：网络未确认，重试将使用同一幂等键";
  }
  return "结果未知：网络未确认，重试将使用同一幂等键";
}

/** The Rule Administrator draft surface: import a legacy bundle, edit the
 * published non-runtime declarative metadata only (scope / validity /
 * source / reason), then freeze an immutable candidate.  Every command is
 * fenced with the governance revision and a locked idempotency identity;
 * nothing here owns release state. */
function DraftWorkflowSection({
  onCandidateSelected,
  commandLatch,
  locked,
  onRegisterReconcile,
  onRegisterAttempt,
}: {
  onCandidateSelected: (candidateId: string) => void;
  commandLatch: CommandLatchApi;
  locked: boolean;
  onRegisterReconcile: (fn: () => Promise<number>) => void;
  onRegisterAttempt: (attempt: AttemptRecord) => void;
}) {
  const [sourceBundleId, setSourceBundleId] = useState("");
  const [draftId, setDraftId] = useState<string | null>(null);
  const [scope, setScope] = useState("");
  const [source, setSource] = useState("");
  const [reason, setReason] = useState("");
  const [validFrom, setValidFrom] = useState("");
  const [conflict, setConflict] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const importLegacy = useImportLegacy();
  const reviseDraft = useReviseDraft();
  const freezeCandidate = useFreezeCandidate();

  // The Admin status query is the sole source of the governance revision
  // before a candidate exists; acceptance of any S08 command invalidates it,
  // so every subsequent command fences on the newest server revision.
  const statusQuery = useS08Status();
  const revision = statusQuery.data?.governance_revision ?? 0;

  /** Definitive 409s surface the stable server reason, freeze the action
   * surface and refetch the authoritative status so the next command builds
   * on the newest revision; 422s get the fixed invalid-command text;
   * every other outcome is shown explicitly and an unknown outcome retains
   * the locked idempotency identity for byte-identical retry.  Only an
   * authoritative response (acceptance or registered rejection) marks the
   * latch final; a changed input or a switched action never releases it. */
  const handleError = (error: unknown) => {
    if (isDefinitiveS08Rejection(error)) {
      if (error instanceof HttpError && error.status === 409) {
        setConflict(rejectionText(error));
        void statusQuery.refetch().then((result) => {
          if (result.isSuccess) commandLatch.markFinal();
        });
        return;
      }
      commandLatch.markFinal();
      if (error instanceof HttpError && error.status === 422) {
        setActionError("命令无效：请检查输入后重试");
        return;
      }
    } else {
      commandLatch.markUnknown();
    }
    setActionError(commandRejectionText(error));
  };

  // The section's authoritative reconciliation refetches the Admin status.
  useEffect(() => {
    onRegisterReconcile(async () => {
      const result = await statusQuery.refetch();
      return result.data?.governance_revision ?? 0;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [statusQuery]);

  const submitImport = () => {
    setConflict(null);
    setActionError(null);
    const lockedBody = commandLatch.lock(
      "import_legacy",
      { source_bundle_id: sourceBundleId },
      revision,
    );
    if (lockedBody === null) return;
    const applyImportResult = (result: unknown) => {
      setDraftId((result as { draft_id: string }).draft_id);
    };
    onRegisterAttempt({
      send: (body, callbacks) =>
        importLegacy.mutate(JSON.parse(body) as S08ImportCommand, callbacks),
      onSuccess: applyImportResult,
      onError: handleError,
    });
    importLegacy.mutate(JSON.parse(lockedBody.body) as S08ImportCommand, {
      onSuccess: (result) => {
        commandLatch.markFinal();
        applyImportResult(result);
      },
      onError: handleError,
    });
  };

  const submitRevise = () => {
    if (draftId === null) return;
    setConflict(null);
    setActionError(null);
    const lockedBody = commandLatch.lock(
      "revise_draft",
      {
        draft_id: draftId,
        metadata: {
          scope,
          source,
          reason,
          validity: { valid_from: `${validFrom}:00Z` },
        },
      },
      revision,
    );
    if (lockedBody === null) return;
    onRegisterAttempt({
      send: (body, callbacks) =>
        reviseDraft.mutate(JSON.parse(body) as S08ReviseCommand, callbacks),
      onError: handleError,
    });
    reviseDraft.mutate(JSON.parse(lockedBody.body) as S08ReviseCommand, {
      onSuccess: () => commandLatch.markFinal(),
      onError: handleError,
    });
  };

  const submitFreeze = () => {
    if (draftId === null) return;
    setConflict(null);
    setActionError(null);
    const lockedBody = commandLatch.lock(
      "freeze_candidate",
      { draft_id: draftId },
      revision,
    );
    if (lockedBody === null) return;
    const applyFreezeResult = (result: unknown) => {
      onCandidateSelected((result as { candidate_id: string }).candidate_id);
    };
    onRegisterAttempt({
      send: (body, callbacks) =>
        freezeCandidate.mutate(JSON.parse(body) as S08FreezeCommand, callbacks),
      onSuccess: applyFreezeResult,
      onError: handleError,
    });
    freezeCandidate.mutate(JSON.parse(lockedBody.body) as S08FreezeCommand, {
      onSuccess: (result) => {
        commandLatch.markFinal();
        applyFreezeResult(result);
      },
      onError: handleError,
    });
  };

  return (
    <section className="panel" data-testid="t08-draft-workflow">
      <h2>治理策略候选工作台</h2>
      <dl className="facts">
        <div>
          <dt>发布流程</dt>
          <dd>
            导入受控基线 → 编辑非运行时元数据 → 冻结不可变候选 → 独立审批 →
            调度已审批计划
          </dd>
        </div>
      </dl>
      {statusQuery.isLoading ? (
        <p data-testid="t08-status-loading" role="status">
          正在加载治理状态…
        </p>
      ) : statusQuery.isError ? (
        <p
          data-testid={
            statusQuery.error instanceof HttpError &&
            statusQuery.error.status === 403
              ? "t08-status-forbidden"
              : "t08-status-unavailable"
          }
          role="alert"
        >
          {statusQuery.error instanceof HttpError &&
          statusQuery.error.status === 403
            ? "无权限访问治理状态"
            : "治理状态暂不可用，请稍后重试"}
        </p>
      ) : draftId === null ? (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            submitImport();
          }}
        >
          <label>
            来源包标识
            <input
              value={sourceBundleId}
              onChange={(event) => {
                setSourceBundleId(event.target.value);
              }}
              required
              disabled={locked}
            />
          </label>
          <Button
            type="submit"
            data-testid="t08-import-button"
            disabled={importLegacy.isPending || locked}
          >
            {importLegacy.isPending ? "导入中…" : "导入受控基线"}
          </Button>
        </form>
      ) : (
        <div data-testid="t08-draft-editor">
          <dl className="facts">
            <div>
              <dt>草稿标识</dt>
              <dd data-testid="t08-draft-id">{draftId}</dd>
            </div>
          </dl>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              submitRevise();
            }}
          >
            <label>
              适用范围
              <input
                value={scope}
                onChange={(event) => {
                  setScope(event.target.value);
                }}
                required
                disabled={locked}
              />
            </label>
            <label>
              来源
              <input
                value={source}
                onChange={(event) => {
                  setSource(event.target.value);
                }}
                required
                disabled={locked}
              />
            </label>
            <label>
              变更原因
              <input
                value={reason}
                onChange={(event) => {
                  setReason(event.target.value);
                }}
                required
                disabled={locked}
              />
            </label>
            <label>
              生效起始
              <input
                type="datetime-local"
                value={validFrom}
                onChange={(event) => {
                  setValidFrom(event.target.value);
                }}
                required
                disabled={locked}
              />
            </label>
            <Button
              type="submit"
              data-testid="t08-revise-button"
              disabled={reviseDraft.isPending || locked}
            >
              {reviseDraft.isPending ? "保存中…" : "保存草稿"}
            </Button>
          </form>
          {reviseDraft.isSuccess && (
            <p data-testid="t08-revise-ok" role="status">
              草稿已保存
            </p>
          )}
          <Button
            data-testid="t08-freeze-button"
            onClick={submitFreeze}
            disabled={freezeCandidate.isPending || locked}
          >
            {freezeCandidate.isPending ? "冻结中…" : "冻结为不可变候选"}
          </Button>
        </div>
      )}
      {conflict !== null && (
        <p data-testid="t08-conflict" role="alert">
          状态已变化：{conflict}
        </p>
      )}
      {actionError !== null && (
        <p data-testid="t08-action-error" role="alert">
          {actionError}
        </p>
      )}
    </section>
  );
}

function epochSeconds(datetimeLocal: string): number {
  return Math.floor(new Date(datetimeLocal).getTime() / 1000);
}

/** Stable poll-termination predicates: an outcome that ended diagnostic is
 * a terminal end the poll must stop on.  Module-level so the polling effect
 * never re-arms with a fresh identity each render. */
function s08ValidationEndedDiagnostic(
  workspace: S08CandidateWorkspaceResponse,
): boolean {
  return workspace.validation_outcome?.status === "failed";
}

function s08ActivationEndedDiagnostic(
  workspace: S08CandidateWorkspaceResponse,
): boolean {
  return workspace.activation_outcome?.status === "failed";
}

function ActionButton({
  testId,
  label,
  pendingLabel,
  disabled,
  pending,
  onClick,
}: {
  testId: string;
  label: string;
  pendingLabel: string;
  disabled?: boolean;
  pending: boolean;
  onClick: () => void;
}) {
  return (
    <Button
      data-testid={testId}
      onClick={onClick}
      disabled={disabled || pending}
    >
      {pending ? pendingLabel : label}
    </Button>
  );
}

/** One server-owned action rendered from the workspace ``actions`` list.
 * The backend remains the final authorization and status authority; the
 * panel never computes transitions, never substitutes a digest, and only
 * ever POSTs the exact values the server returned. */
function WorkspaceActions({
  workspace,
  revision,
  onConflict,
  commandLatch,
  locked,
  onRegisterAttempt,
  onRefresh,
}: {
  workspace: S08CandidateWorkspaceResponse;
  revision: number;
  onConflict: (message: string) => void;
  commandLatch: CommandLatchApi;
  locked: boolean;
  onRegisterAttempt: (attempt: AttemptRecord) => void;
  onRefresh: () => Promise<S08CandidateWorkspaceResponse | undefined>;
}) {
  const [activationTime, setActivationTime] = useState("");
  const [recoveryReleaseId, setRecoveryReleaseId] = useState(
    () => workspace.active_anchor?.candidate_id ?? "",
  );
  const [reasonCode, setReasonCode] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);
  const [validating, setValidating] = useState(false);
  const [activating, setActivating] = useState(false);
  const [validationEnd, setValidationEnd] = useState<
    "none" | "unavailable" | "timed_out"
  >("none");
  const [activationEnd, setActivationEnd] = useState<
    "none" | "unavailable" | "timed_out"
  >("none");
  const [notice, setNotice] = useState<string | null>(null);
  /** The last accepted server impact preview for this workspace: the only
   * source of the exact ``preview_manifest_id`` and the revision the
   * approval fences on.  Cleared on any conflict and on acceptance, so a
   * stale preview can never feed a later approval. */
  const [previewDto, setPreviewDto] = useState<S09PreviewResponse | null>(null);

  const validate = useRequestValidation();
  const submitReview = useSubmitReview();
  const previewImpact = usePreviewImpact();
  const approve = useApproveCandidate();
  const reject = useRejectCandidate();
  const schedule = useScheduleActivation();
  const cancel = useCancelCandidate();

  const actions = new Set(workspace.actions ?? []);

  /** The shared settlement handling for this section's commands: a
   * definitive rejection (409 conflict surfaces the stable server reason and
   * the parent refetches) marks the latch final; an unknown outcome keeps
   * the byte-identical identity locked for the exact retry. */
  const handleError = (error: unknown) => {
    if (isDefinitiveS08Rejection(error)) {
      if (error instanceof HttpError && error.status === 409) {
        // A conflict proves the ledger moved: any accepted preview is stale
        // for the next command and must be recomputed after the refetch.
        setPreviewDto(null);
        onConflict(rejectionText(error));
        return;
      }
      commandLatch.markFinal();
    } else {
      commandLatch.markUnknown();
    }
    if (error instanceof HttpError && error.status === 422) {
      setActionError("命令无效：请检查输入后重试");
      return;
    }
    setActionError(commandRejectionText(error));
  };

  const run = <TCommand extends Record<string, unknown>>(
    mutation: {
      mutate: (
        command: TCommand,
        callbacks?: {
          onSuccess?: (result: unknown) => void;
          onError?: (error: unknown) => void;
        },
      ) => void;
      isPending: boolean;
    },
    action: string,
    command: Omit<
      TCommand,
      "idempotency_key" | "expected_governance_revision"
    >,
    onSuccess?: (result: unknown) => void,
    revisionOverride?: number,
  ) => {
    onConflict("");
    setActionError(null);
    setNotice(null);
    const lockedBody = commandLatch.lock(
      action,
      command,
      revisionOverride ?? revision,
    );
    if (lockedBody === null) return;
    onRegisterAttempt({
      send: (body, callbacks) =>
        mutation.mutate(JSON.parse(body) as TCommand, callbacks),
      onSuccess,
      onError: handleError,
    });
    mutation.mutate(JSON.parse(lockedBody.body) as TCommand, {
      onSuccess: (result: unknown) => {
        commandLatch.markFinal();
        onSuccess?.(result);
      },
      onError: handleError,
    });
  };

  /** Run the immutable impact preview through the shared command latch and
   * hand the accepted server DTO to ``previewDone``.  The preview is its own
   * complete command: a definitive acceptance marks the latch final and the
   * approval then starts a fresh locked command. */
  const runPreview = (previewDone: (preview: S09PreviewResponse) => void) => {
    run<S09PreviewCommand>(
      previewImpact,
      "preview_impact",
      { candidate_id: workspace.candidate_id },
      (result) => {
        const preview = result as S09PreviewResponse;
        setPreviewDto(preview);
        previewDone(preview);
      },
    );
  };

  /** The approval binds the exact server previewed manifest and fences on
   * the revision that preview returned (the preview appended one immutable
   * fact); it never re-derives a digest or a revision. */
  const runApprove = (preview: S09PreviewResponse) => {
    run<S08ApproveCommand>(
      approve,
      "approve",
      {
        candidate_id: workspace.candidate_id,
        activation_time: epochSeconds(activationTime),
        recovery_release_id: recoveryReleaseId,
        preview_manifest_id: preview.manifest_id,
      },
      () => {
        setNotice("已审批并锁定发布计划");
        // The preview fact is consumed by the approval; the workspace
        // refetch lands the new revision, and the next approval must
        // preview afresh.
        setPreviewDto(null);
      },
      preview.governance_revision,
    );
  };

  /** Approval requires an accepted preview DTO that has been rendered on
   * the page: the button stays disabled until one exists (P-1), a 409
   * clears it, and a fresh explicit preview is the only way to approve
   * again.  No approval can ever auto-run a preview behind the user's
   * back. */
  const submitApprove = () => {
    setActionError(null);
    setNotice(null);
    if (previewDto === null) {
      return;
    }
    runApprove(previewDto);
  };

  /** Authoritative reconciliation after an unavailable activation poll:
   * refetch the workspace; while the authority is back and the job is still
   * pending, resume the bounded poll, otherwise the terminal outcome
   * (active/failed) renders from the fresh snapshot. */
  const refreshActivation = async () => {
    const refreshed = await onRefresh();
    if (refreshed?.activation_outcome?.status === "pending") {
      setActivationEnd("none");
      setActivating(true);
    }
  };

  const validationPoll = useS08StatusPoll(
    workspace.candidate_id,
    validating,
    ["validated", "rejected"],
    { alsoTerminal: s08ValidationEndedDiagnostic },
  );
  const activationPoll = useS08StatusPoll(
    workspace.candidate_id,
    activating,
    ["active"],
    { alsoTerminal: s08ActivationEndedDiagnostic },
  );

  useEffect(() => {
    if (validationPoll === "converged") {
      setValidating(false);
    } else if (validationPoll === "terminal") {
      setValidating(false);
      // A rejected or diagnostic-failed candidate is rendered from the
      // authoritative validation_outcome; every other terminal end (a
      // closed 503 on the poll) is the explicit unavailable state.
      const outcome = workspace.validation_outcome?.status;
      if (outcome !== "rejected" && outcome !== "failed") {
        setValidationEnd("unavailable");
      }
    } else if (validationPoll === "timed_out") {
      setValidationEnd("timed_out");
    }
    if (activationPoll === "converged") {
      setActivating(false);
    } else if (activationPoll === "terminal") {
      setActivating(false);
      if (workspace.activation_outcome?.status !== "failed") {
        setActivationEnd("unavailable");
      }
    } else if (activationPoll === "timed_out") {
      setActivationEnd("timed_out");
    }
  }, [validationPoll, activationPoll, workspace.validation_outcome, workspace.activation_outcome]);

  return (
    <div className="recovery-actions" data-testid="t08-actions">
      {actions.has("request_validation") && (
        <ActionButton
          testId="t08-validate-button"
          label="请求验证"
          pendingLabel="验证中…"
          pending={validate.isPending}
          disabled={locked}
          onClick={() =>
            run<S08ValidationCommand>(
              validate,
              "request_validation",
              { candidate_id: workspace.candidate_id },
              () => setValidating(true),
            )
          }
        />
      )}
      {validating && validationPoll === "waiting" && (
        <p data-testid="t08-polling" role="status">
          等待服务端验证完成…
        </p>
      )}
      {validationPoll === "timed_out" && (
        <p data-testid="t08-polling-timeout" role="alert">
          验证仍在服务端进行，请稍后手动刷新
        </p>
      )}
      {validationEnd === "unavailable" && (
        <p data-testid="t08-validation-unavailable" role="alert">
          验证状态暂不可用，请稍后刷新
        </p>
      )}
      {workspace.validation_outcome?.status === "rejected" && (
        <div data-testid="t08-validation-rejected" role="alert">
          <p>
            验证未通过：{workspace.validation_outcome.reason_code ?? "被拒绝"}
          </p>
          {workspace.validation_bundle != null && (
            <ul data-testid="t08-validation-rejected-evidence">
              {(workspace.validation_bundle.results?.checks ?? []).map(
                (check) => (
                  <li key={check.check_id}>
                    {check.check_id}: {check.outcome}
                  </li>
                ),
              )}
            </ul>
          )}
        </div>
      )}
      {workspace.validation_outcome?.status === "failed" && (
        <div data-testid="t08-validation-failed" role="alert">
          <p>
            验证诊断失败：
            {workspace.validation_outcome.reason_code ?? "验证服务异常"}
          </p>
        </div>
      )}
      {actions.has("submit_review") && (
        <ActionButton
          testId="t08-submit-button"
          label="提交独立审批"
          pendingLabel="提交中…"
          pending={submitReview.isPending}
          disabled={locked}
          onClick={() =>
            run<S08SubmitReviewCommand>(
              submitReview,
              "submit_review",
              { candidate_id: workspace.candidate_id },
            )
          }
        />
      )}
      {actions.has("approve") && (
        <form
          data-testid="t08-approve-form"
          onSubmit={(event) => {
            event.preventDefault();
            submitApprove();
          }}
        >
          <label>
            生效时间
            <input
              type="datetime-local"
              aria-label="生效时间"
              value={activationTime}
              onChange={(event) => {
                setActivationTime(event.target.value);
              }}
              required
              disabled={locked}
            />
          </label>
          <label>
            回滚发布标识
            <input
              value={recoveryReleaseId}
              onChange={(event) => {
                setRecoveryReleaseId(event.target.value);
              }}
              required
              disabled={locked}
            />
          </label>
          <Button
            type="button"
            data-testid="t08-preview-button"
            onClick={() => runPreview(() => {})}
            disabled={previewImpact.isPending || locked}
          >
            {previewImpact.isPending ? "影响预览计算中…" : "影响预览"}
          </Button>
          {previewDto !== null && (
            <div data-testid="t08-preview" role="status">
              <h3>影响预览（服务端只读）</h3>
              <dl className="facts">
                <div>
                  <dt>清单标识</dt>
                  <dd data-testid="t08-preview-manifest">
                    {previewDto.manifest_id}
                  </dd>
                </div>
                <div>
                  <dt>摘要</dt>
                  <dd>{previewDto.digest}</dd>
                </div>
                <div>
                  <dt>范围</dt>
                  <dd>{previewDto.scope}</dd>
                </div>
                <div>
                  <dt>成员数</dt>
                  <dd data-testid="t08-preview-members">
                    {previewDto.member_count}
                  </dd>
                </div>
                <div>
                  <dt>分区</dt>
                  <dd>
                    {Object.entries(previewDto.partition_counts)
                      .map(([name, count]) => `${name}: ${count}`)
                      .join("，")}
                  </dd>
                </div>
                <div>
                  <dt>扩张标记</dt>
                  <dd data-testid="t08-preview-expansion">
                    {previewDto.expanded_to_full_scope
                      ? "已扩张到完整范围"
                      : "未扩张"}
                  </dd>
                </div>
                <div>
                  <dt>目标代次</dt>
                  <dd data-testid="t08-preview-generation">
                    {previewDto.target_generation}
                  </dd>
                </div>
              </dl>
            </div>
          )}
          <Button
            type="submit"
            data-testid="t08-approve-button"
            disabled={
              approve.isPending ||
              previewImpact.isPending ||
              locked ||
              previewDto === null
            }
          >
            {approve.isPending ? "审批中…" : "审批并锁定发布计划"}
          </Button>
        </form>
      )}
      {actions.has("reject") && (
        <form
          data-testid="t08-reject-form"
          onSubmit={(event) => {
            event.preventDefault();
            run<S08RejectCommand>(reject, "reject", {
              candidate_id: workspace.candidate_id,
              reason_code: reasonCode,
            });
          }}
        >
          <label>
            驳回原因码
            <input
              value={reasonCode}
              onChange={(event) => {
                setReasonCode(event.target.value);
              }}
              required
              disabled={locked}
            />
          </label>
          <Button
            type="submit"
            data-testid="t08-reject-button"
            disabled={reject.isPending || locked}
          >
            {reject.isPending ? "驳回中…" : "驳回候选"}
          </Button>
        </form>
      )}
      {actions.has("schedule") && (
        <div data-testid="t08-schedule-form">
          <dl className="facts">
            <div>
              <dt>已绑定生效时间</dt>
              <dd data-testid="t08-binding-time">
                {workspace.approval_binding
                  ? String(workspace.approval_binding.activation_time)
                  : "—"}
              </dd>
            </div>
          </dl>
          <ActionButton
            testId="t08-schedule-button"
            label="调度已审批计划"
            pendingLabel="调度中…"
            pending={schedule.isPending}
            disabled={
              workspace.approval_binding == null ||
              locked
            }
            onClick={() => {
              const binding = workspace.approval_binding;
              if (binding == null || workspace.approval_binding_id == null) {
                return;
              }
              run<S08ScheduleCommand>(
                schedule,
                "schedule",
                {
                  approval_binding_id: workspace.approval_binding_id,
                  activation_at: binding.activation_time,
                },
                () => {
                  setActivating(true);
                  setNotice("已调度已审批计划");
                },
              );
            }}
          />
        </div>
      )}
      {actions.has("cancel") && (
        <ActionButton
          testId="t08-cancel-button"
          label="取消候选"
          pendingLabel="取消中…"
          pending={cancel.isPending}
          disabled={locked}
          onClick={() =>
            run<S08CancelCommand>(cancel, "cancel", {
              candidate_id: workspace.candidate_id,
              reason_code: "t08-cancel",
            })
          }
        />
      )}
      {activating && activationPoll === "waiting" && (
        <p data-testid="t08-activation-polling" role="status">
          等待服务端激活完成…
        </p>
      )}
      {workspace.activation_outcome?.status === "failed" && (
        <div data-testid="t08-activation-failed" role="alert">
          <p>
            激活失败：
            {workspace.activation_outcome.reason_code ?? "诊断失败"}
          </p>
          <p>
            当前活跃发布仍为：{workspace.active_anchor
              ? `${workspace.active_anchor.candidate_id} / ${workspace.active_anchor.manifest_digest}`
              : "—"}
          </p>
        </div>
      )}
      {activationEnd === "unavailable" && (
        <div data-testid="t08-activation-unavailable" role="alert">
          <p>激活状态暂不可用，恢复后请点击刷新对账</p>
          <Button
            data-testid="t08-activation-refresh"
            onClick={refreshActivation}
          >
            刷新激活状态
          </Button>
        </div>
      )}
      {activationEnd === "timed_out" && (
        <p data-testid="t08-activation-timeout" role="alert">
          激活仍在服务端进行，请稍后手动刷新
        </p>
      )}
      {notice !== null && (
        <p data-testid="t08-action-ok" role="status">
          {notice}
        </p>
      )}
      {actionError !== null && (
        <p data-testid="t08-action-error" role="alert">
          {actionError}
        </p>
      )}
    </div>
  );
}

/** The candidate workspace: read-only server-owned facts plus the server
 * action list.  Loading, conflict, forbidden, not-found, unavailable and
 * success states are all explicit; a 409 refetches the authoritative
 * workspace so the next command is built on the server revision. */
function WorkspaceSection({
  candidateId,
  commandLatch,
  locked,
  onRegisterReconcile,
  onRegisterAttempt,
}: {
  candidateId: string;
  commandLatch: CommandLatchApi;
  locked: boolean;
  onRegisterReconcile: (fn: () => Promise<number>) => void;
  onRegisterAttempt: (attempt: AttemptRecord) => void;
}) {
  const query = useCandidateWorkspace(candidateId);
  const [conflict, setConflict] = useState<string | null>(null);
  const [refetched, setRefetched] = useState(false);

  useEffect(() => {
    if (query.isError && query.error instanceof HttpError) {
      setRefetched(true);
    }
  }, [query.isError, query.error]);

  // The section's authoritative reconciliation refetches the workspace.
  useEffect(() => {
    onRegisterReconcile(async () => {
      const result = await query.refetch();
      return (
        result.data?.governance_revision ??
        query.data?.governance_revision ??
        0
      );
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query]);

  // A conflict keeps its stable server reason visible until the next
  // explicit command: the authoritative workspace refetch below lands the
  // server revision, and the next submit builds on it with a fresh key.
  const handleConflict = (message: string) => {
    setConflict(message);
    if (message !== "") {
      void query.refetch().then((result) => {
        if (result.isSuccess) commandLatch.markFinal();
      });
    }
  };

  if (query.isLoading) {
    return (
      <section className="panel" data-testid="t08-loading">
        正在加载候选工作区…
      </section>
    );
  }
  // A refetch failure (e.g. the activation poll hitting a closed 503) must
  // not replace an already-loaded workspace: the last authoritative snapshot
  // stays rendered while the polling surface reports the unavailable end
  // state explicitly.
  if (query.data === undefined && query.isError) {
    const error = query.error;
    if (error instanceof HttpError && error.status === 403) {
      return (
        <section className="panel" data-testid="t08-forbidden" role="alert">
          {rejectionText(error)}
        </section>
      );
    }
    if (error instanceof HttpError && error.status === 404) {
      return (
        <section className="panel" data-testid="t08-not-found" role="alert">
          候选不可访问
        </section>
      );
    }
    if (
      error instanceof HttpError &&
      error.status === 503 &&
      isDefinitiveS08Rejection(error)
    ) {
      return (
        <section className="panel" data-testid="t08-unavailable" role="alert">
          治理服务暂不可用
        </section>
      );
    }
    if (error instanceof HttpError && error.status === 422) {
      return (
        <section className="panel" data-testid="t08-invalid" role="alert">
          请求无效：请刷新后重试
        </section>
      );
    }
    return (
      <section className="panel" data-testid="t08-error" role="alert">
        {rejectionText(error)}
      </section>
    );
  }

  const workspace = query.data;
  if (workspace === undefined) {
    return (
      <section className="panel" data-testid="t08-loading">
        正在加载候选工作区…
      </section>
    );
  }

  const bundle = workspace.validation_bundle;
  const review = workspace.review_material;

  return (
    <section className="panel" data-testid="t08-workspace">
      <h2>治理策略候选工作区</h2>
      <dl className="facts">
        <div>
          <dt>候选标识</dt>
          <dd>{workspace.candidate_id}</dd>
        </div>
        <div>
          <dt>状态</dt>
          <dd data-testid="t08-workspace-status">{workspace.status}</dd>
        </div>
        <div>
          <dt>当前角色</dt>
          <dd data-testid="t08-workspace-role">{workspace.actor_role}</dd>
        </div>
        <div>
          <dt>治理修订号</dt>
          <dd data-testid="t08-workspace-revision">
            {workspace.governance_revision}
          </dd>
        </div>
        <div>
          <dt>候选摘要</dt>
          <dd data-testid="t08-workspace-digest">
            {workspace.manifest_digest ?? "—"}
          </dd>
        </div>
        <div>
          <dt>当前活跃锚点</dt>
          <dd data-testid="t08-workspace-anchor">
            {workspace.active_anchor
              ? `${workspace.active_anchor.candidate_id} / ${workspace.active_anchor.manifest_digest}`
              : "—"}
          </dd>
        </div>
        {workspace.manifest_id != null && (
          <div>
            <dt>清单标识</dt>
            <dd>{workspace.manifest_id}</dd>
          </div>
        )}
        {workspace.approval_binding_id != null && (
          <div>
            <dt>审批绑定</dt>
            <dd>{workspace.approval_binding_id}</dd>
          </div>
        )}
        {workspace.approval_binding_digest != null && (
          <div>
            <dt>审批绑定摘要</dt>
            <dd>{workspace.approval_binding_digest}</dd>
          </div>
        )}
      </dl>

      {bundle != null && (
        <div data-testid="t08-validation">
          <h3>验证证据</h3>
          <dl className="facts">
            <div>
              <dt>验证包</dt>
              <dd>
                {workspace.validation_bundle_id ?? "—"} /{" "}
                {workspace.validation_bundle_digest ?? "—"}
              </dd>
            </div>
            <div>
              <dt>结果</dt>
              <dd>
                {bundle.results?.failed_count ?? "—"} 失败 /{" "}
                {bundle.results?.checks?.length ?? 0} 项
              </dd>
            </div>
          </dl>
          <ul data-testid="t08-validation-checks">
            {(bundle.results?.checks ?? []).map((check) => (
              <li key={check.check_id}>
                {check.check_id}: {check.outcome}
              </li>
            ))}
          </ul>
        </div>
      )}

      {review != null && (
        <div data-testid="t08-review">
          <h3>审批比对</h3>
          <ul data-testid="t08-review-changes">
            {(review.changes ?? []).map((change) => (
              <li key={`${change.component}:${change.change}`}>
                {change.change} · {change.component}
              </li>
            ))}
            {(review.changes ?? []).length === 0 && <li>无变更</li>}
          </ul>
        </div>
      )}

      {workspace.events.length > 0 && (
        <div data-testid="t08-events">
          <h3>治理事件（服务端只读）</h3>
          <ol>
            {workspace.events.map((event) => (
              <li key={event.event_id} data-testid="t08-event">
                {event.kind}
                {" · "}
                {event.actor.subject}
                {" · "}
                {event.reason_code ?? "—"}
              </li>
            ))}
          </ol>
        </div>
      )}

      {conflict !== null && (
        <p data-testid="t08-conflict" role="alert">
          状态已变化：{conflict}
        </p>
      )}
      {refetched && (
        <p data-testid="t08-refetched" role="status">
          已刷新服务端最新状态
        </p>
      )}

      <WorkspaceActions
        workspace={workspace}
        revision={workspace.governance_revision}
        onConflict={handleConflict}
        commandLatch={commandLatch}
        locked={locked}
        onRegisterAttempt={onRegisterAttempt}
        onRefresh={async () => (await query.refetch()).data}
      />
    </section>
  );
}

/** The one React surface for the governed policy-release workspace.  The
 * candidate id arrives as non-sensitive URL navigation state; authorization
 * and existence are decided by the exact candidate query.  With no
 * candidate, the Admin sees the draft workflow; with one, the workspace
 * drives every action from the server-owned list.  Exactly one command
 * latch lives here: while it is in flight or unknown, every mutation input
 * and action across the page is disabled and only the exact byte-identical
 * retry may proceed; the authoritative refresh is display-only and never
 * releases the latch. */
export default function PolicyReleasePanel({
  candidateId,
  onCandidateSelected,
}: PolicyReleasePanelProps) {
  const commandLatch = useCommandLatch();
  const reconcileRef = useRef<(() => Promise<number>) | null>(null);
  const attemptRef = useRef<AttemptRecord | null>(null);
  const latch = commandLatch.latch;
  const locked = latch !== null && latch.outcome !== "final";

  /** The only re-send while the latch is unknown: the exact locked bytes on
   * the exact locked key, replayed through the mutation that sent them. */
  const retry = () => {
    const lockedLatch = commandLatch.latch;
    const attempt = attemptRef.current;
    if (lockedLatch === null || attempt === null) return;
    commandLatch.markInFlight();
    attempt.send(lockedLatch.body, {
      onSuccess: (result: unknown) => {
        commandLatch.markFinal();
        attempt.onSuccess?.(result);
      },
      onError: (error: unknown) => {
        if (
          isDefinitiveS08Rejection(error) &&
          !(error instanceof HttpError && error.status === 409)
        ) {
          commandLatch.markFinal();
        } else if (!isDefinitiveS08Rejection(error)) {
          commandLatch.markUnknown();
        }
        attempt.onError?.(error);
      },
    });
  };

  /** Display-only authoritative refresh: refetches the visible section's
   * authoritative read so the page shows the newest server state, but never
   * releases the command latch -- a global ledger revision advance is not
   * proof that the locked operation settled.  Only the exact replay's
   * definitive response releases it. */
  const refresh = () => {
    if (reconcileRef.current === null) return;
    void reconcileRef.current();
  };

  const sectionProps = {
    commandLatch,
    locked,
    onRegisterReconcile: (fn: () => Promise<number>) => {
      reconcileRef.current = fn;
    },
    onRegisterAttempt: (attempt: AttemptRecord) => {
      attemptRef.current = attempt;
    },
  };

  return (
    <>
      {candidateId !== null && candidateId !== "" ? (
        <WorkspaceSection candidateId={candidateId} {...sectionProps} />
      ) : (
        <DraftWorkflowSection
          onCandidateSelected={onCandidateSelected ?? (() => {})}
          {...sectionProps}
        />
      )}
      {latch !== null && latch.outcome === "unknown" && (
        <div data-testid="t08-command-unknown" role="alert">
          <p>结果未知：网络未确认，重试将使用同一幂等键</p>
          <Button
            data-testid="t08-command-retry"
            onClick={retry}
            disabled={attemptRef.current === null}
          >
            重试上一命令
          </Button>
          <Button data-testid="t08-command-reconcile" onClick={refresh}>
            刷新权威状态
          </Button>
        </div>
      )}
    </>
  );
}

/**
 * The T09 governance workspace: one atomic server projection rendered for
 * the four governance roles.  The server owns the actor role, the action
 * list, the active release, the known-good recovery anchor, the active hold
 * union and the append-only event refs; this panel renders exactly those
 * facts, imposes scoped safety holds through the same command-latch
 * discipline as the T08 surface, and shows the Auditor the per-member
 * impact reconciliation for the active final impact.  FastAPI remains the
 * sole authority; the panel never derives a transition, a hold or a
 * recovery option, and protected mutations use ``retry: false`` with an
 * unknown result retaining the byte-identical locked command.
 */
export function GovernanceWorkspacePanel() {
  const commandLatch = useCommandLatch();
  const reconcileRef = useRef<(() => Promise<number>) | null>(null);
  const attemptRef = useRef<AttemptRecord | null>(null);
  const latch = commandLatch.latch;
  const locked = latch !== null && latch.outcome !== "final";
  const workspaceQuery = useS09Workspace();
  const imposeHold = useImposeHold();
  const proposeRollback = useProposeRollback();
  const recoverHold = useRecoverHold();
  const [conflict, setConflict] = useState<string | null>(null);
  const [conflictReloadFailed, setConflictReloadFailed] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [holdReason, setHoldReason] = useState("");
  const [holdScope, setHoldScope] = useState("open_cycle");
  const [rollbackReason, setRollbackReason] = useState("");
  const [rollbackRelease, setRollbackRelease] = useState("");
  const [rollbackResult, setRollbackResult] =
    useState<S09ProposeRollbackResponse | null>(null);
  const [recoverHoldId, setRecoverHoldId] = useState("");
  const [recoverGeneration, setRecoverGeneration] = useState("");

  // The Auditor's reconciliation read is enabled only for the auditor role
  // and an active final impact digest; the hook itself stays mounted so the
  // hook order never varies.
  const workspaceData = workspaceQuery.data;
  const finalDigest =
    workspaceData?.active_release?.final_impact_digest ?? null;
  const reconciliation = useImpactReconciliation(
    workspaceData?.actor_role === "auditor" ? finalDigest : null,
  );

  // F-SPEC-2 cached refetch currentness: a failed refetch after a successful
  // load keeps the old server facts in TanStack Query, so the panel must
  // classify that stale authority explicitly — deterministic 403/404 hide
  // the protected surface, transient 5xx/transport failures label the
  // last-known facts and fence every mutation until an explicit reload.
  const workspaceRefetchFailed =
    workspaceQuery.isRefetchError && workspaceQuery.data !== undefined;
  const workspaceRefetchError = workspaceQuery.error;
  const staleWorkspace =
    workspaceRefetchFailed &&
    !(
      workspaceRefetchError instanceof HttpError &&
      (workspaceRefetchError.status === 403 ||
        workspaceRefetchError.status === 404)
    );
  const reconRefetchFailed =
    reconciliation.isRefetchError && reconciliation.data !== undefined;
  const reconRefetchError = reconciliation.error;
  const reconDetailHidden =
    reconRefetchFailed &&
    reconRefetchError instanceof HttpError &&
    (reconRefetchError.status === 403 || reconRefetchError.status === 404);
  // The page-level command fence: the sent-command latch plus any stale
  // workspace authority that cannot be presented as current.
  const fenced = locked || staleWorkspace;

  // The panel's authoritative reconciliation refetches the workspace.
  useEffect(() => {
    reconcileRef.current = async () => {
      const result = await workspaceQuery.refetch();
      return (
        result.data?.governance_revision ??
        workspaceQuery.data?.governance_revision ??
        0
      );
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceQuery]);

  const handleConflict = (message: string) => {
    setConflict(message);
    if (message !== "") {
      setConflictReloadFailed(false);
      void workspaceQuery.refetch().then((result) => {
        if (result.isSuccess) {
          commandLatch.markFinal();
          setConflictReloadFailed(false);
        } else {
          // The definitive 409 settled the submitted command; the
          // authoritative reload failed, so stale commands stay fenced and
          // the page offers an explicit reload control (P-2).
          setConflictReloadFailed(true);
        }
      });
    }
  };

  /** The only way out of a failed conflict reconciliation (P-2): retry the
   * authoritative refetch; on success the settled command identity is
   * released and the next command fences on the fresh server revision. */
  const retryConflictReload = () => {
    setActionError(null);
    void workspaceQuery.refetch().then((result) => {
      if (result.isSuccess) {
        commandLatch.markFinal();
        setConflictReloadFailed(false);
      }
    });
  };

  const handleError = (error: unknown) => {
    if (isDefinitiveS08Rejection(error)) {
      if (error instanceof HttpError && error.status === 409) {
        // A conflict proves the ledger moved: any accepted hold, rollback
        // verdict or recovery is stale and must be recomputed after the
        // authoritative refetch.
        setRollbackResult(null);
        handleConflict(rejectionText(error));
        return;
      }
      commandLatch.markFinal();
    } else {
      commandLatch.markUnknown();
    }
    if (error instanceof HttpError && error.status === 422) {
      setActionError("命令无效：请检查输入后重试");
      return;
    }
    setActionError(commandRejectionText(error));
  };

  const run = <TCommand extends Record<string, unknown>>(
    mutation: {
      mutate: (
        command: TCommand,
        callbacks?: {
          onSuccess?: (result: unknown) => void;
          onError?: (error: unknown) => void;
        },
      ) => void;
      isPending: boolean;
    },
    action: string,
    command: Omit<
      TCommand,
      "idempotency_key" | "expected_governance_revision"
    >,
    onSuccess?: (result: unknown) => void,
    revisionOverride?: number,
  ) => {
    setConflict(null);
    setActionError(null);
    setNotice(null);
    const revision = workspaceQuery.data?.governance_revision ?? 0;
    const lockedBody = commandLatch.lock(
      action,
      command,
      revisionOverride ?? revision,
    );
    if (lockedBody === null) return;
    attemptRef.current = {
      send: (body, callbacks) =>
        mutation.mutate(JSON.parse(body) as TCommand, callbacks),
      onSuccess,
      onError: handleError,
    };
    mutation.mutate(JSON.parse(lockedBody.body) as TCommand, {
      onSuccess: (result: unknown) => {
        commandLatch.markFinal();
        onSuccess?.(result);
      },
      onError: handleError,
    });
  };

  /** The only re-send while the latch is unknown: the exact locked bytes on
   * the exact locked key, replayed through the mutation that sent them. */
  const retry = () => {
    const lockedLatch = commandLatch.latch;
    const attempt = attemptRef.current;
    if (lockedLatch === null || attempt === null) return;
    commandLatch.markInFlight();
    attempt.send(lockedLatch.body, {
      onSuccess: (result: unknown) => {
        commandLatch.markFinal();
        attempt.onSuccess?.(result);
      },
      onError: (error: unknown) => {
        if (
          isDefinitiveS08Rejection(error) &&
          !(error instanceof HttpError && error.status === 409)
        ) {
          commandLatch.markFinal();
        } else if (!isDefinitiveS08Rejection(error)) {
          commandLatch.markUnknown();
        }
        attempt.onError?.(error);
      },
    });
  };

  /** Display-only authoritative refresh: never releases the command latch. */
  const refresh = () => {
    if (reconcileRef.current === null) return;
    void reconcileRef.current();
  };

  const submitImposeHold = () => {
    run<S09ImposeHoldCommand>(
      imposeHold,
      "impose_hold",
      { reason_code: holdReason, hold_scope: holdScope },
      () => setNotice("已施加安全冻结"),
    );
  };

  const submitProposeRollback = () => {
    run<S09ProposeRollbackCommand>(
      proposeRollback,
      "propose_rollback",
      { release_candidate_id: rollbackReleaseValue, reason_code: rollbackReason },
      (result) => setRollbackResult(result as S09ProposeRollbackResponse),
    );
  };

  const submitRecoverHold = () => {
    run<S09RecoverHoldCommand>(
      recoverHold,
      "recover_hold",
      { hold_id: recoverHoldIdValue, recovery_generation: Number(recoverGenerationValue) },
      () => setNotice("已确认恢复并释放安全冻结"),
    );
  };

  // F-SPEC-2: a cached refetch 403/404 hides the stale protected surface —
  // actions, holds, Security Audit refs — before any authorization state is
  // presented as current.
  if (
    workspaceRefetchFailed &&
    workspaceRefetchError instanceof HttpError &&
    (workspaceRefetchError.status === 403 ||
      workspaceRefetchError.status === 404)
  ) {
    return (
      <section
        className="panel"
        data-testid={
          workspaceRefetchError.status === 403
            ? "t09-forbidden"
            : "t09-not-found"
        }
        role="alert"
      >
        {workspaceRefetchError.status === 403
          ? rejectionText(workspaceRefetchError)
          : "治理工作区不可访问"}
      </section>
    );
  }

  if (workspaceQuery.isLoading || workspaceQuery.data === undefined) {
    if (!workspaceQuery.isError) {
      return (
        <section className="panel" data-testid="t09-loading" role="status">
          正在加载治理工作区…
        </section>
      );
    }
    const error = workspaceQuery.error;
    if (error instanceof HttpError && error.status === 403) {
      return (
        <section className="panel" data-testid="t09-forbidden" role="alert">
          {rejectionText(error)}
        </section>
      );
    }
    if (error instanceof HttpError && error.status === 404) {
      return (
        <section className="panel" data-testid="t09-not-found" role="alert">
          治理工作区不可访问
        </section>
      );
    }
    if (
      error instanceof HttpError &&
      error.status === 503 &&
      isDefinitiveS08Rejection(error)
    ) {
      return (
        <section className="panel" data-testid="t09-unavailable" role="alert">
          治理服务暂不可用
        </section>
      );
    }
    if (error instanceof HttpError && error.status === 422) {
      return (
        <section className="panel" data-testid="t09-invalid" role="alert">
          请求无效：请刷新后重试
        </section>
      );
    }
    return (
      <section className="panel" data-testid="t09-error" role="alert">
        {rejectionText(error)}
      </section>
    );
  }

  const workspace = workspaceQuery.data;
  const actions = new Set(workspace.actions ?? []);
  const active = workspace.active_release ?? null;
  const recoveryRelease =
    workspace.recovery_anchor?.release_candidate_id ?? "";
  // The rollback form's only prefill is the server-recorded known-good
  // recovery anchor; an explicit user entry wins.
  const rollbackReleaseValue =
    rollbackRelease === "" ? recoveryRelease : rollbackRelease;
  const recoverHoldIdValue =
    recoverHoldId === "" ? (workspace.holds[0]?.hold_id ?? "") : recoverHoldId;
  const recoverGenerationValue =
    recoverGeneration === ""
      ? String(active?.active_generation ?? "")
      : recoverGeneration;

  return (
    <>
      <section className="panel" data-testid="t09-workspace">
        <h2>治理影响与安全冻结工作区</h2>
        {staleWorkspace && (
          <div data-testid="t09-stale" role="alert">
            <p>
              连接不稳定：显示的是上次已知权威状态，命令已禁用，请重新加载
            </p>
            <Button
              data-testid="t09-workspace-reload"
              onClick={() => void workspaceQuery.refetch()}
            >
              重新加载权威状态
            </Button>
          </div>
        )}
        <dl className="facts">
          <div>
            <dt>当前角色</dt>
            <dd data-testid="t09-role">{workspace.actor_role}</dd>
          </div>
          <div>
            <dt>治理修订号</dt>
            <dd data-testid="t09-revision">{workspace.governance_revision}</dd>
          </div>
          <div>
            <dt>作用域</dt>
            <dd>{workspace.scope}</dd>
          </div>
          <div>
            <dt>服务器授权动作</dt>
            <dd data-testid="t09-action-list">
              {workspace.actions.length === 0
                ? "—"
                : workspace.actions.join("，")}
            </dd>
          </div>
          <div>
            <dt>当前活跃发布</dt>
            <dd data-testid="t09-active-release">
              {active === null
                ? "—"
                : `${active.candidate_id} / ${active.manifest_digest} / 代次 ${active.active_generation}`}
            </dd>
          </div>
          <div>
            <dt>已知良好恢复锚点</dt>
            <dd data-testid="t09-recovery-anchor">
              {workspace.recovery_anchor === null ||
              workspace.recovery_anchor === undefined
                ? "—"
                : workspace.recovery_anchor.release_candidate_id}
            </dd>
          </div>
        </dl>

        <div data-testid="t09-holds">
          <h3>安全冻结（服务端只读）</h3>
          {workspace.holds.length === 0 ? (
            <p data-testid="t09-holds-empty">当前无生效安全冻结</p>
          ) : (
            <ul>
              {workspace.holds.map((hold) => (
                <li key={hold.hold_id} data-testid="t09-hold">
                  <dl className="facts">
                    <div>
                      <dt>范围</dt>
                      <dd data-testid="t09-hold-scope">{hold.hold_scope}</dd>
                    </div>
                    <div>
                      <dt>原因</dt>
                      <dd data-testid="t09-hold-reason">{hold.reason_code}</dd>
                    </div>
                    <div>
                      <dt>施加者</dt>
                      <dd data-testid="t09-hold-actor">{hold.imposed_by}</dd>
                    </div>
                    <div>
                      <dt>恢复判定</dt>
                      <dd data-testid="t09-hold-criterion">
                        {hold.recovery_criterion_id ?? "—"}
                      </dd>
                    </div>
                    <div>
                      <dt>判定摘要</dt>
                      <dd>{hold.recovery_criterion_digest ?? "—"}</dd>
                    </div>
                    <div>
                      <dt>权威修订</dt>
                      <dd data-testid="t09-hold-authority-revision">
                        {hold.authority_revision ?? "—"}
                      </dd>
                    </div>
                    <div>
                      <dt>状态</dt>
                      <dd data-testid="t09-hold-status">
                        生效中，持续至显式恢复
                      </dd>
                    </div>
                  </dl>
                </li>
              ))}
            </ul>
          )}
        </div>

        {workspace.events.length > 0 && (
          <div data-testid="t09-events">
            <h3>治理事件（服务端只读，只追加）</h3>
            <ol>
              {workspace.events.map((event) => (
                <li key={event.event_id} data-testid="t09-event">
                  {event.kind} · {event.actor.subject} ·{" "}
                  {event.reason_code ?? "—"} · 修订 {event.revision}
                  {event.hold_id !== null && event.hold_id !== undefined
                    ? ` · 冻结 ${event.hold_id}`
                    : ""}
                  {event.release_candidate_id !== null &&
                  event.release_candidate_id !== undefined
                    ? ` · 回滚目标 ${event.release_candidate_id}`
                    : ""}
                </li>
              ))}
            </ol>
          </div>
        )}

        {workspace.actor_role === "auditor" &&
          workspace.audit_events.length > 0 && (
            <div data-testid="t09-audit">
              <h3>安全审计记录（服务端只读，只追加）</h3>
              <ol>
                {workspace.audit_events.map((record) => (
                  <li key={record.event_id} data-testid="t09-audit-record">
                    {record.action} · {record.subject} · {record.role} ·{" "}
                    {record.result}
                    {record.reason_code !== null &&
                    record.reason_code !== undefined
                      ? ` · ${record.reason_code}`
                      : ""}
                    {record.hold_id !== null && record.hold_id !== undefined
                      ? ` · 冻结 ${record.hold_id}`
                      : ""}
                    {record.release_candidate_id !== null &&
                    record.release_candidate_id !== undefined
                      ? ` · 回滚目标 ${record.release_candidate_id}`
                      : ""}
                    {record.rollback_candidate_id !== null &&
                    record.rollback_candidate_id !== undefined
                      ? ` · 新回滚候选 ${record.rollback_candidate_id}`
                      : ""}
                    {record.recovery_generation !== null &&
                    record.recovery_generation !== undefined
                      ? ` · 恢复代次 ${record.recovery_generation}`
                      : ""}
                  </li>
                ))}
              </ol>
            </div>
          )}

        {actions.has("impose_hold") && (
          <form
            data-testid="t09-impose-form"
            onSubmit={(event) => {
              event.preventDefault();
              submitImposeHold();
            }}
          >
            <label>
              冻结原因码
              <input
                value={holdReason}
                onChange={(event) => {
                  setHoldReason(event.target.value);
                }}
                required
                disabled={fenced}
              />
            </label>
            <label>
              冻结范围
              <input
                value={holdScope}
                onChange={(event) => {
                  setHoldScope(event.target.value);
                }}
                required
                disabled={fenced}
              />
            </label>
            <Button
              type="submit"
              data-testid="t09-impose-button"
              disabled={imposeHold.isPending || fenced}
            >
              {imposeHold.isPending ? "冻结中…" : "施加安全冻结"}
            </Button>
          </form>
        )}

        {actions.has("propose_rollback") && (
          <form
            data-testid="t09-rollback-form"
            onSubmit={(event) => {
              event.preventDefault();
              submitProposeRollback();
            }}
          >
            <label>
              回滚发布标识
              <input
                value={rollbackReleaseValue}
                onChange={(event) => {
                  setRollbackRelease(event.target.value);
                }}
                required
                disabled={fenced}
              />
            </label>
            <label>
              回滚原因码
              <input
                value={rollbackReason}
                onChange={(event) => {
                  setRollbackReason(event.target.value);
                }}
                required
                disabled={fenced}
              />
            </label>
            <Button
              type="submit"
              data-testid="t09-rollback-button"
              disabled={proposeRollback.isPending || fenced}
            >
              {proposeRollback.isPending ? "校验中…" : "提出兼容回滚"}
            </Button>
            {rollbackResult !== null && (
              <div data-testid="t09-rollback-result" role="status">
                <p data-testid="t09-rollback-compatibility">
                  {rollbackResult.compatibility.compatible
                    ? "回滚兼容：可恢复"
                    : "回滚不兼容"}{" "}
                  · {rollbackResult.compatibility.reason_code}
                </p>
                <p data-testid="t09-rollback-candidate">
                  新回滚候选：{rollbackResult.candidate_id}
                </p>
                {rollbackResult.compatibility.compatible && (
                  <a
                    data-testid="t09-rollback-link"
                    href={`/controlled/s08/react?candidate=${encodeURIComponent(
                      rollbackResult.candidate_id,
                    )}`}
                  >
                    在策略发布工作台继续审批回滚候选
                  </a>
                )}
              </div>
            )}
          </form>
        )}

        {actions.has("recover_hold") && (
          <form
            data-testid="t09-recover-form"
            onSubmit={(event) => {
              event.preventDefault();
              submitRecoverHold();
            }}
          >
            <label>
              冻结标识
              <input
                value={recoverHoldIdValue}
                onChange={(event) => {
                  setRecoverHoldId(event.target.value);
                }}
                required
                disabled={fenced}
              />
            </label>
            <label>
              恢复代次
              <input
                type="number"
                value={recoverGenerationValue}
                onChange={(event) => {
                  setRecoverGeneration(event.target.value);
                }}
                required
                readOnly
                disabled={fenced}
              />
            </label>
            <Button
              type="submit"
              data-testid="t09-recover-button"
              disabled={recoverHold.isPending || fenced}
            >
              {recoverHold.isPending ? "恢复中…" : "确认恢复（释放冻结）"}
            </Button>
          </form>
        )}

        {workspace.actor_role === "auditor" && finalDigest !== null && (
          <section className="panel" data-testid="t09-recon">
            <h3>影响对账（审计明细）</h3>
            {reconciliation.isLoading && (
              <p data-testid="t09-recon-loading" role="status">
                对账明细加载中…
              </p>
            )}
            {reconRefetchFailed &&
              reconRefetchError instanceof HttpError &&
              reconRefetchError.status === 403 && (
                <p data-testid="t09-recon-forbidden" role="alert">
                  无权访问对账明细
                </p>
              )}
            {reconRefetchFailed &&
              reconRefetchError instanceof HttpError &&
              reconRefetchError.status === 404 && (
                <p data-testid="t09-recon-pending" role="alert">
                  对账投影尚未生成，请稍后刷新
                </p>
              )}
            {reconRefetchFailed && !reconDetailHidden && (
              <>
                <p data-testid="t09-recon-unavailable" role="alert">
                  对账明细暂不可用
                </p>
                <Button
                  data-testid="t09-recon-reload"
                  onClick={() => void reconciliation.refetch()}
                >
                  重新加载对账明细
                </Button>
                {reconciliation.data !== undefined && (
                  <p data-testid="t09-recon-stale" role="alert">
                    上次已知对账明细（可能过期）
                  </p>
                )}
              </>
            )}
            {reconciliation.data === undefined && reconciliation.isError && (
              <p
                data-testid={
                  reconciliation.error instanceof HttpError &&
                  reconciliation.error.status === 403
                    ? "t09-recon-forbidden"
                    : reconciliation.error instanceof HttpError &&
                        reconciliation.error.status === 404
                      ? "t09-recon-pending"
                      : "t09-recon-unavailable"
                }
                role="alert"
              >
                {reconciliation.error instanceof HttpError &&
                reconciliation.error.status === 403
                  ? "无权访问对账明细"
                  : reconciliation.error instanceof HttpError &&
                      reconciliation.error.status === 404
                    ? "对账投影尚未生成，请稍后刷新"
                    : "对账明细暂不可用"}
              </p>
            )}
            {reconciliation.data === undefined &&
              reconciliation.isError &&
              !(
                reconciliation.error instanceof HttpError &&
                reconciliation.error.status === 403
              ) && (
                <Button
                  data-testid="t09-recon-reload"
                  onClick={() => void reconciliation.refetch()}
                >
                  重新加载对账明细
                </Button>
              )}
            {reconciliation.data !== undefined && !reconDetailHidden && (
              <>
                <dl className="facts">
                  <div>
                    <dt>成员数</dt>
                    <dd>{reconciliation.data.member_count}</dd>
                  </div>
                  <div>
                    <dt>未消费</dt>
                    <dd>{reconciliation.data.unconsumed_count}</dd>
                  </div>
                </dl>
                {(reconciliation.data.unconsumed_count > 0 ||
                  reconciliation.data.members.some(
                    (member) => member.disposition === "outstanding",
                  )) && (
                  <p data-testid="t09-recon-partial" role="alert">
                    存在未消费或待处理的影响处置：恢复前必须全部对账完成
                  </p>
                )}
                <ul data-testid="t09-recon-members">
                  {reconciliation.data.members.map((member) => (
                    <li
                      key={`${member.application_id}:${member.cycle}`}
                    >
                      {member.application_id} · {member.partition} ·{" "}
                      {member.disposition} · 目标代次{" "}
                      {member.target_generation} · 重评任务{" "}
                      {member.reevaluation_job_count}
                    </li>
                  ))}
                </ul>
              </>
            )}
          </section>
        )}

        {conflict !== null && (
          <p data-testid="t09-conflict" role="alert">
            状态已变化：{conflict}
          </p>
        )}
        {conflictReloadFailed && (
          <div data-testid="t09-conflict-reload" role="alert">
            <p>权威状态刷新失败：命令已保持冻结，请重新加载最新状态</p>
            <Button
              data-testid="t09-conflict-reload-button"
              onClick={retryConflictReload}
            >
              重新加载权威状态
            </Button>
          </div>
        )}
        {notice !== null && (
          <p data-testid="t09-action-ok" role="status">
            {notice}
          </p>
        )}
        {actionError !== null && (
          <p data-testid="t09-action-error" role="alert">
            {actionError}
          </p>
        )}
      </section>
      {latch !== null && latch.outcome === "unknown" && (
        <div data-testid="t09-command-unknown" role="alert">
          <p>结果未知：网络未确认，重试将使用同一幂等键</p>
          <Button
            data-testid="t09-command-retry"
            onClick={retry}
            disabled={attemptRef.current === null}
          >
            重试上一命令
          </Button>
          <Button data-testid="t09-command-reconcile" onClick={refresh}>
            刷新权威状态
          </Button>
        </div>
      )}
    </>
  );
}
