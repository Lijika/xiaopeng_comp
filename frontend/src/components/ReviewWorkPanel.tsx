import { useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import {
  HttpError,
  isDefinitiveRejection,
  type ApplicationHistoryResponse,
  type ReviewWorkResponse,
  type WorkspaceResponse,
} from "../api/client";
import {
  MANUAL_WORK_KEY,
  useApplicationHistory,
  useClaimWorkItem,
  useCurrentRoute,
  useManualWork,
  useReleaseWorkItem,
  useRenewWorkItem,
  useSubmitVerification,
  useWorkspace,
  type ClaimCommand,
  type FencedCommand,
  type SubmitCommand,
} from "../api/hooks";
import { Button } from "./ui/button";
import GateSection from "./GateSection";

function newIdempotencyKey(): string {
  return crypto.randomUUID();
}

type Action = "claim" | "renew" | "release" | "submit";

type Outcome = "confirmed" | "not_confirmed" | "inconclusive";

const OUTCOMES: readonly Outcome[] = [
  "confirmed",
  "not_confirmed",
  "inconclusive",
];

type PendingCommand = {
  action: Action;
  command: ClaimCommand | FencedCommand | SubmitCommand;
};

const ACTION_LABELS: Record<Action, string> = {
  claim: "认领",
  renew: "续期",
  release: "释放",
  submit: "核验",
};

/** Builds the structured manual verification from the Reviewer's explicit
 * outcome choice; every finding decision carries that same chosen value and
 * no automatic verdict/route/target is ever copied into the decision. */
function buildVerification(
  work: ReviewWorkResponse,
  outcome: Outcome,
): SubmitCommand["verification"] {
  return {
    schema_version: "human-decision/1",
    outcome,
    reason_code: "HUMAN_REVIEW_COMPLETED",
    finding_decisions: work.automatic_findings.map((finding) => ({
      finding_id: finding.finding_id,
      outcome,
    })),
  };
}

function WorkFacts({ work }: { work: ReviewWorkResponse }) {
  return (
    <section aria-labelledby="review-facts-title">
      <h3 id="review-facts-title">工作项（服务端权威）</h3>
      <dl className="facts">
        <div>
          <dt>状态</dt>
          <dd data-testid="review-status">{work.status}</dd>
        </div>
        <div>
          <dt>阶段</dt>
          <dd data-testid="review-phase">{work.phase}</dd>
        </div>
        <div>
          <dt>路由</dt>
          <dd data-testid="review-route">{work.route}</dd>
        </div>
        <div>
          <dt>认领围栏</dt>
          <dd data-testid="review-claim-fence">{work.claim_fence}</dd>
        </div>
        <div>
          <dt>认领过期（epoch）</dt>
          <dd data-testid="review-claim-expiry">{work.claim_expires_at}</dd>
        </div>
        <div>
          <dt>生命周期修订</dt>
          <dd data-testid="review-lifecycle-revision">
            {work.lifecycle_revision}
          </dd>
        </div>
        <div>
          <dt>证据修订</dt>
          <dd data-testid="review-evidence-revision">
            {work.evidence_revision}
          </dd>
        </div>
      </dl>
      <h4>自动发现（不改写）</h4>
      <ul data-testid="review-findings">
        {work.automatic_findings.map((finding) => (
          <li key={finding.finding_id} data-testid="review-finding">
            <span data-testid="review-finding-rule">{finding.rule_id}</span>
            {" · "}
            <span data-testid="review-finding-verdict">{finding.verdict}</span>
            {" · "}
            {finding.severity}
            {" · "}
            {finding.reason_code}
          </li>
        ))}
      </ul>
      <dl className="facts">
        <div>
          <dt>运行权威摘要</dt>
          <dd data-testid="review-run-digest">
            {work.run_authority.authority_digest}
          </dd>
        </div>
      </dl>
    </section>
  );
}

function WorkspaceSection({
  work,
  workspace,
}: {
  work: ReviewWorkResponse;
  workspace: UseQueryResult<WorkspaceResponse>;
}) {
  if (work.status === "completed") {
    return (
      <section data-testid="review-workspace-gone" className="panel-inline">
        <p className="text-sm text-muted-foreground">
          人工核验已完成：工作区已随生命周期结束
        </p>
      </section>
    );
  }
  if (workspace.isPending) {
    return (
      <section data-testid="review-workspace-loading">
        <p>工作区加载中…</p>
      </section>
    );
  }
  if (workspace.isError || workspace.data === undefined) {
    const notFound =
      workspace.error instanceof HttpError && workspace.error.status === 404;
    return (
      <section data-testid="review-workspace-error">
        <p>{notFound ? "工作区未找到或无权访问" : "工作区不可用"}</p>
      </section>
    );
  }
  const finding = workspace.data.selected_finding ?? null;
  return (
    <section aria-labelledby="review-workspace-title">
      <h3 id="review-workspace-title">最小工作区（发现优先）</h3>
      <dl className="facts">
        <div>
          <dt>认领过期（epoch）</dt>
          <dd data-testid="review-workspace-expiry">
            {workspace.data.claim_expires_at}
          </dd>
        </div>
        <div>
          <dt>生命周期修订</dt>
          <dd data-testid="review-workspace-lifecycle">
            {workspace.data.lifecycle_revision}
          </dd>
        </div>
        <div>
          <dt>证据修订</dt>
          <dd data-testid="review-workspace-evidence-revision">
            {workspace.data.evidence_revision}
          </dd>
        </div>
        <div>
          <dt>投影水位</dt>
          <dd data-testid="review-workspace-watermark">
            {workspace.data.projection_watermark}
          </dd>
        </div>
        <div>
          <dt>当前运行</dt>
          <dd data-testid="review-workspace-current-run">
            {workspace.data.current_run_id ?? "None"}
          </dd>
        </div>
        <div>
          <dt>证据快照</dt>
          <dd data-testid="review-workspace-snapshot">
            {workspace.data.evidence_snapshot_id ?? "None"}
          </dd>
        </div>
      </dl>
      {finding === null ? (
        <p data-testid="review-workspace-empty" className="text-sm text-muted-foreground">
          无可复核发现
        </p>
      ) : (
        <>
          <dl className="facts">
            <div>
              <dt>规则</dt>
              <dd data-testid="review-workspace-rule">{finding.rule_id}</dd>
            </div>
            <div>
              <dt>判定</dt>
              <dd data-testid="review-workspace-verdict">{finding.verdict}</dd>
            </div>
            <div>
              <dt>严重度</dt>
              <dd data-testid="review-workspace-severity">{finding.severity}</dd>
            </div>
            <div>
              <dt>原因</dt>
              <dd data-testid="review-workspace-reason">{finding.reason_code}</dd>
            </div>
          </dl>
          <h4>证据（已掩码）</h4>
          <ul data-testid="review-evidence-links">
            {finding.evidence_links.map((link) => (
              <li key={link.observation_id} data-testid="review-evidence-link">
                {link.document_id} · {link.field} ·{" "}
                <span data-testid="review-evidence-masked">
                  {link.raw_masked ?? link.value_state}
                </span>
                <dl className="facts">
                  <div>
                    <dt>文档角色</dt>
                    <dd data-testid="review-evidence-role">
                      {link.document_role}
                    </dd>
                  </div>
                  <div>
                    <dt>来源页</dt>
                    <dd data-testid="review-evidence-source-page">
                      {link.source_page ?? "None"}
                    </dd>
                  </div>
                  <div>
                    <dt>来源区域</dt>
                    <dd data-testid="review-evidence-source-region">
                      {link.source_region ?? "None"}
                    </dd>
                  </div>
                  <div>
                    <dt>来源出处摘要</dt>
                    <dd data-testid="review-evidence-provenance">
                      {link.provenance_manifest_digest ?? "None"}
                    </dd>
                  </div>
                  <div>
                    <dt>证据资格</dt>
                    <dd data-testid="review-evidence-eligibility">
                      {link.evidence_eligible === true
                        ? (link.eligibility_reason ?? "eligible")
                        : "ineligible"}
                    </dd>
                  </div>
                </dl>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

function HistorySection({
  history,
}: {
  history: UseQueryResult<ApplicationHistoryResponse>;
}) {
  if (history.isPending) {
    return <p data-testid="review-history-loading">历史加载中…</p>;
  }
  if (history.isError || history.data === undefined) {
    const notFound =
      history.error instanceof HttpError && history.error.status === 404;
    return (
      <p data-testid="review-history-error">
        {notFound ? "历史未找到或无权访问" : "历史不可用"}
      </p>
    );
  }
  const current = history.data.runs.find((run) => run.current === true);
  return (
    <section className="panel" data-testid="history-panel" aria-labelledby="review-history-title">
      <h3 id="review-history-title">历史（服务端权威）</h3>
      <ol data-testid="review-history-runs">
        {history.data.runs.map((run) => (
          <li key={run.run_id}>
            {run.run_id}
            {" · "}
            {run.status}
            {" · "}
            {run.currentness_reason}
            {run.current === true ? " · 当前" : ""}
          </li>
        ))}
      </ol>
      <dl className="facts">
        <div>
          <dt>当前运行决策</dt>
          <dd data-testid="review-history-decisions">
            {current === undefined || current.decision_ids.length === 0
              ? "None"
              : current.decision_ids.join(", ")}
          </dd>
        </div>
      </dl>
    </section>
  );
}

export default function ReviewWorkPanel({ workId }: { workId: string }) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  const queryClient = useQueryClient();
  const work = useManualWork(workId);
  const workspace = useWorkspace(
    work.data !== undefined && work.data.status !== "completed"
      ? work.data.application_id
      : null,
  );
  const history = useApplicationHistory(work.data?.application_id ?? null);
  // The route query is hoisted so action gating can require a current
  // authoritative route read; GateSection observes the same shared query.
  const gate = useCurrentRoute(work.data?.application_id ?? null);

  const claim = useClaimWorkItem(workId);
  const renew = useRenewWorkItem(workId);
  const release = useReleaseWorkItem(workId);
  const submit = useSubmitVerification(workId);

  const [renewKey, setRenewKey] = useState(newIdempotencyKey);
  const [releaseKey, setReleaseKey] = useState(newIdempotencyKey);
  const [submitKey, setSubmitKey] = useState(newIdempotencyKey);
  const [pendingCommand, setPendingCommand] = useState<PendingCommand | null>(
    null,
  );
  const [requiresReload, setRequiresReload] = useState(false);
  const [conflictReason, setConflictReason] = useState<string | null>(null);
  const [lastAccepted, setLastAccepted] = useState<Action | null>(null);
  const [rejectedAction, setRejectedAction] = useState<Action | null>(null);
  const [outcome, setOutcome] = useState<Outcome>("confirmed");

  useEffect(() => {
    headingRef.current?.focus();
  }, [workId]);

  const anyPending =
    claim.isPending || renew.isPending || release.isPending || submit.isPending;

  const mutations: Record<
    Action,
    { isPending: boolean; isError: boolean; isSuccess: boolean; error: unknown }
  > = {
    claim,
    renew,
    release,
    submit,
  };
  const active = pendingCommand === null ? null : pendingCommand.action;
  const activeMutation = active === null ? undefined : mutations[active];
  const transportUnknown =
    active !== null &&
    activeMutation !== undefined &&
    activeMutation.isError &&
    !isDefinitiveRejection(activeMutation.error);

  const keyRotations: Record<Action, () => void> = {
    claim: () => {},
    renew: () => setRenewKey(newIdempotencyKey()),
    release: () => setReleaseKey(newIdempotencyKey()),
    submit: () => setSubmitKey(newIdempotencyKey()),
  };

  const accepted = (action: Action) => () => {
    setPendingCommand(null);
    setLastAccepted(action);
    setRejectedAction(null);
    setRequiresReload(false);
    setConflictReason(null);
    // A command key is bound to one logical command (its fingerprint covers
    // the fence); the next logical command needs a fresh key, otherwise a
    // later fence change would collide with the retained fingerprint.
    keyRotations[action]();
  };

  /** A definitive rejection proves the semantic key was never accepted, so
   * the key rotates once at that proof and the rejected action stays latched
   * until the owning work-item refetch succeeds (FIX-1). */
  const rejected = (action: Action) => (error: Error) => {
    if (!isDefinitiveRejection(error)) return;
    keyRotations[action]();
    setPendingCommand(null);
    setRejectedAction(action);
    setRequiresReload(true);
    setConflictReason(error.reasonCode ?? "conflict");
  };

  const owningReadsCurrent =
    work.isSuccess &&
    workspace.isSuccess &&
    history.isSuccess &&
    gate.isSuccess;

  const handleClaim = () => {
    if (work.data === undefined || anyPending || pendingCommand !== null) return;
    if (requiresReload || work.isError || !owningReadsCurrent) return;
    const command: ClaimCommand = {
      expected_context: work.data.command_context,
    };
    setPendingCommand({ action: "claim", command });
    setLastAccepted(null);
    setRejectedAction(null);
    claim.mutate(command, {
      onSuccess: accepted("claim"),
      onError: rejected("claim"),
    });
  };

  const handleRenew = () => {
    if (work.data === undefined || anyPending || pendingCommand !== null) return;
    if (requiresReload || work.isError || !owningReadsCurrent) return;
    const command: FencedCommand = {
      expected_fence: work.data.claim_fence,
      expected_context: work.data.command_context,
      idempotency_key: renewKey,
    };
    setPendingCommand({ action: "renew", command });
    setLastAccepted(null);
    setRejectedAction(null);
    renew.mutate(command, {
      onSuccess: accepted("renew"),
      onError: rejected("renew"),
    });
  };

  const handleRelease = () => {
    if (work.data === undefined || anyPending || pendingCommand !== null) return;
    if (requiresReload || work.isError || !owningReadsCurrent) return;
    const command: FencedCommand = {
      expected_fence: work.data.claim_fence,
      expected_context: work.data.command_context,
      idempotency_key: releaseKey,
    };
    setPendingCommand({ action: "release", command });
    setLastAccepted(null);
    setRejectedAction(null);
    release.mutate(command, {
      onSuccess: accepted("release"),
      onError: rejected("release"),
    });
  };

  const handleSubmit = () => {
    if (work.data === undefined || anyPending || pendingCommand !== null) return;
    if (requiresReload || work.isError || !owningReadsCurrent) return;
    const command: SubmitCommand = {
      expected_fence: work.data.claim_fence,
      expected_context: work.data.command_context,
      idempotency_key: submitKey,
      verification: buildVerification(work.data, outcome),
    };
    setPendingCommand({ action: "submit", command });
    setLastAccepted(null);
    setRejectedAction(null);
    submit.mutate(command, {
      onSuccess: accepted("submit"),
      onError: rejected("submit"),
    });
  };

  /** Reconciling retry: a claim has no idempotency key, so its unknown
   * outcome is resolved by an authoritative refetch that identifies the live
   * lease instead of blindly issuing a second business effect. */
  const reconcileClaim = async () => {
    try {
      await queryClient.refetchQueries(
        { queryKey: MANUAL_WORK_KEY(workId) },
        { throwOnError: true },
      );
    } catch {
      return;
    }
    await queryClient.invalidateQueries({ queryKey: ["s01"] });
    const reconciled = queryClient.getQueryData<ReviewWorkResponse>(
      MANUAL_WORK_KEY(workId),
    );
    setPendingCommand(null);
    setRejectedAction(null);
    setRequiresReload(false);
    setConflictReason(null);
    if (reconciled?.status === "claimed") {
      setLastAccepted("claim");
    }
  };

  const handleRetry = () => {
    if (pendingCommand === null || anyPending) return;
    if (pendingCommand.action === "claim") {
      void reconcileClaim();
      return;
    }
    const command = pendingCommand.command;
    if (pendingCommand.action === "renew") {
      renew.mutate(command as FencedCommand, {
        onSuccess: accepted("renew"),
        onError: rejected("renew"),
      });
    } else if (pendingCommand.action === "release") {
      release.mutate(command as FencedCommand, {
        onSuccess: accepted("release"),
        onError: rejected("release"),
      });
    } else {
      submit.mutate(command as SubmitCommand, {
        onSuccess: accepted("submit"),
        onError: rejected("submit"),
      });
    }
  };

  const handleReload = async () => {
    // A command outcome that is still pending must never be reset or given a
    // fresh semantic key, and an accepted outcome must never be cleared by a
    // reload.
    if (anyPending) return;
    // Authoritative reload.  A failed refetch must never clear the conflict
    // fence nor rotate the semantic idempotency keys, so the refetch throws
    // and the fence is kept on failure.  The rejection latch clears only
    // after the owning work-item refetch succeeds.
    try {
      await queryClient.refetchQueries(
        { queryKey: MANUAL_WORK_KEY(workId) },
        { throwOnError: true },
      );
    } catch {
      return;
    }
    await queryClient.invalidateQueries({ queryKey: ["s01"] });
    setRejectedAction(null);
    setRequiresReload(false);
    setConflictReason(null);
  };

  let statusText = "等待操作";
  if (anyPending && active !== null) {
    statusText = `${ACTION_LABELS[active]}提交中…`;
  } else if (
    requiresReload &&
    conflictReason !== null &&
    rejectedAction !== null
  ) {
    statusText = `${ACTION_LABELS[rejectedAction]}未接受（${conflictReason}）：请重新加载权威上下文后再试`;
  } else if (transportUnknown) {
    statusText = "结果未知：网络未确认，重试将使用同一幂等键";
  } else if (lastAccepted !== null) {
    statusText = `${ACTION_LABELS[lastAccepted]}已接受`;
  }

  const workspaceRequired =
    work.data !== undefined && work.data.status !== "completed";
  const dependentError =
    (workspaceRequired && workspace.isError) || history.isError || gate.isError;

  if (work.isPending || work.isError || work.data === undefined || dependentError) {
    if (work.isPending) {
      return (
        <section
          className="panel"
          data-testid="review-panel"
          aria-labelledby="review-title"
        >
          <h2 id="review-title" tabIndex={-1} ref={headingRef}>
            人工核验
          </h2>
          <p data-testid="review-loading">工作项加载中…</p>
        </section>
      );
    }
    const notFound = [
      work.error,
      workspaceRequired ? workspace.error : null,
      history.error,
      gate.error,
    ].some((error) => error instanceof HttpError && error.status === 404);
    return (
      <section
        className="panel"
        data-testid="review-panel"
        aria-labelledby="review-title"
      >
        <h2 id="review-title" tabIndex={-1} ref={headingRef}>
          人工核验
        </h2>
        <p data-testid="review-error">
          {notFound
            ? "未找到或无权访问"
            : work.isError
              ? "工作项不可用"
              : "相关权威不可用"}
        </p>
      </section>
    );
  }

  const data = work.data;
  const claimed = data.status === "claimed";
  const commandBlocked =
    pendingCommand !== null || transportUnknown || requiresReload;
  const canClaim =
    data.status !== "claimed" &&
    data.status !== "completed" &&
    !commandBlocked &&
    !work.isError &&
    owningReadsCurrent;
  const canFenced =
    claimed && !commandBlocked && !work.isError && owningReadsCurrent;
  const canSubmit =
    claimed &&
    data.automatic_findings.length > 0 &&
    !commandBlocked &&
    !work.isError &&
    owningReadsCurrent;

  return (
    <section
      className="panel"
      data-testid="review-panel"
      aria-labelledby="review-title"
    >
      <h2 id="review-title" tabIndex={-1} ref={headingRef}>
        人工核验
      </h2>
      <WorkFacts work={data} />
      <WorkspaceSection work={data} workspace={workspace} />
      <GateSection applicationId={data.application_id} />
      <HistorySection history={history} />
      <div className="recovery-actions" data-testid="review-actions">
        <Button
          variant="secondary"
          onClick={handleReload}
          disabled={anyPending}
          data-testid="reload-button"
        >
          重新加载
        </Button>
        <Button
          onClick={handleClaim}
          disabled={!canClaim || anyPending}
          data-testid="claim-button"
        >
          认领
        </Button>
        <Button
          onClick={handleRenew}
          disabled={!canFenced || anyPending}
          data-testid="renew-button"
        >
          续期
        </Button>
        <Button
          onClick={handleRelease}
          disabled={!canFenced || anyPending}
          data-testid="release-button"
        >
          释放
        </Button>
        {claimed && (
          <label className="text-sm">
            核验结论
            <select
              data-testid="review-outcome"
              value={outcome}
              onChange={(event) => setOutcome(event.target.value as Outcome)}
              disabled={!canSubmit || anyPending}
            >
              {OUTCOMES.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>
        )}
        <Button
          onClick={handleSubmit}
          disabled={!canSubmit || anyPending}
          data-testid="submit-button"
        >
          提交人工核验
        </Button>
        {transportUnknown && (
          <Button variant="outline" onClick={handleRetry} data-testid="retry-button">
            重试
          </Button>
        )}
      </div>
      <p role="status" aria-live="polite" data-testid="review-command-status">
        {statusText}
      </p>
      {requiresReload && (
        <p className="text-sm text-muted-foreground" data-testid="review-reload-note">
          请重新加载权威上下文后再试
        </p>
      )}
    </section>
  );
}
