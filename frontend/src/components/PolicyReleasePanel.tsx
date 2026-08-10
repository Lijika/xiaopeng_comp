import { useEffect, useRef, useState } from "react";

import {
  HttpError,
  isDefinitiveS08Rejection,
  type S08CandidateWorkspaceResponse,
} from "../api/client";
import {
  useApproveCandidate,
  useCancelCandidate,
  useCandidateWorkspace,
  useFreezeCandidate,
  useImportLegacy,
  useRejectCandidate,
  useRequestValidation,
  useReviseDraft,
  useS08Status,
  useS08StatusPoll,
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

  const validate = useRequestValidation();
  const submitReview = useSubmitReview();
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
  ) => {
    onConflict("");
    setActionError(null);
    setNotice(null);
    const lockedBody = commandLatch.lock(action, command, revision);
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
            run<S08ApproveCommand>(
              approve,
              "approve",
              {
                candidate_id: workspace.candidate_id,
                activation_time: epochSeconds(activationTime),
                recovery_release_id: recoveryReleaseId,
              },
              () => setNotice("已审批并锁定发布计划"),
            );
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
            type="submit"
            data-testid="t08-approve-button"
            disabled={approve.isPending || locked}
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
