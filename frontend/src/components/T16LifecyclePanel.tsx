import { useRef, useState } from "react";

import { useCurrentRoute,
  useApplicationHistory,
  useS13Delivery,
  useS14Cancel,
  useS14GrantReopenPermission,
  useS14ProcessNotification,
  useS14Reopen,
  useS14Settle,
  useTerminationConvergence,
  type S14CommandResult,
} from "../api/hooks";
import { HttpError, type CurrentRouteResponse } from "../api/client";
import { Section, leafText } from "./Section";

function newIdempotencyKey(): string {
  return crypto.randomUUID();
}

/**
 * The closed S14 status vocabulary rendered verbatim.  A typed domain
 * outcome is a server fact, not an exception: stale, rejected, unavailable,
 * replayed, outstanding and terminated all surface exactly as returned.
 */
function ResultFacts({
  result,
  children,
}: {
  result: S14CommandResult;
  children?: React.ReactNode;
}) {
  return (
    <div data-testid="t16-command-result">
      <p role="status" aria-live="polite" data-testid="t16-result-status">
        {leafText(result.status)}
        {result.phase !== null && result.phase !== undefined
          ? ` · ${result.phase}`
          : ""}
      </p>
      {(result.reason_code !== null && result.reason_code !== undefined) ||
      (result.reason !== null && result.reason !== undefined) ? (
        <p data-testid="t16-result-reason">
          {[result.reason_code, result.reason].filter(Boolean).join(" · ")}
        </p>
      ) : null}
      {children}
    </div>
  );
}

interface QueryErrorViewProps {
  error: Error;
  onReload: () => void;
  isReloading: boolean;
}

function QueryErrorState({ error, onReload, isReloading }: QueryErrorViewProps) {
  if (!(error instanceof HttpError)) {
    return (
      <div data-testid="t16-error-unknown" role="alert">
        <p>结果未知</p>
        <button type="button" onClick={onReload} disabled={isReloading}>
          {isReloading ? "Reloading" : "Reload authoritative view"}
        </button>
      </div>
    );
  }
  const state =
    error.status === 403
      ? { label: "Authorization denied", testId: "t16-error-forbidden" }
      : error.status === 404
        ? { label: "Not found", testId: "t16-error-not-found" }
        : error.status === 503
          ? { label: "Unavailable", testId: "t16-error-unavailable" }
          : { label: "Request failed", testId: "t16-error-unavailable" };
  return (
    <div data-testid={state.testId} role="alert">
      <p>{state.label}</p>
      <p data-testid="t16-error-code">{error.errorCode ?? `HTTP ${error.status}`}</p>
      <button
        type="button"
        data-testid="t16-reload"
        onClick={onReload}
        disabled={isReloading}
      >
        {isReloading ? "Reloading" : "Reload authoritative view"}
      </button>
    </div>
  );
}

/** The definitive outcomes that prove no effect committed and a fresh
 * semantic idempotency key may be issued after an authoritative reload.
 * Everything else stays visibly unknown with the original key retained. */
function isDefinitiveS14NonAcceptance(result: S14CommandResult | undefined): boolean {
  if (result === undefined) return false;
  return (
    result.status === "rejected" ||
    result.status === "stale" ||
    result.status === "unavailable"
  );
}

/** One shared command-outcome shape: a definitive non-acceptance latches the
 * panel (key retained until an authoritative reload); any other resolved
 * envelope is a fresh semantic command and rotates its idempotency key. */
function handleS14Outcome(
  result: S14CommandResult,
  latch: () => void,
  rotate: () => void,
): void {
  if (isDefinitiveS14NonAcceptance(result)) latch();
  else rotate();
}

const IS_TERMINAL_PHASE: Record<string, true> = {
  Terminating: true,
  Terminated: true,
};

export default function T16LifecyclePanel({
  applicationId,
  selectedCycle,
  onCycleSelected,
  convergenceOptions,
}: {
  applicationId: string | null;
  selectedCycle?: number | null;
  onCycleSelected?: (cycle: number) => void;
  convergenceOptions?: { intervalMs?: number; maxAttempts?: number };
}) {
  const route = useCurrentRoute(applicationId);
  const history = useApplicationHistory(applicationId);
  const cancel = useS14Cancel(applicationId ?? "");
  const [cancelKey, setCancelKey] = useState<string>(newIdempotencyKey);
  const [reasonCode, setReasonCode] = useState("UPSTREAM_WITHDRAWN");
  const [latched, setLatched] = useState(false);

  const convergence = useTerminationConvergence(
    applicationId,
    route.data?.phase === "Terminating",
    convergenceOptions,
  );

  if (!applicationId) {
    return (
      <div data-testid="t16-lifecycle-panel">
        <p data-testid="t16-no-application" role="status" aria-live="polite">
          请选择应用以查看生命周期
        </p>
      </div>
    );
  }
  if (route.isPending || (route.data === undefined && !route.isError)) {
    return (
      <div data-testid="t16-lifecycle-panel">
        <p data-testid="t16-loading" role="status" aria-live="polite">
          正在加载生命周期视图…
        </p>
      </div>
    );
  }
  if (route.isError) {
    return (
      <div data-testid="t16-lifecycle-panel">
        <QueryErrorState
          error={route.error}
          onReload={() => void route.refetch()}
          isReloading={route.isFetching}
        />
      </div>
    );
  }

  const data: CurrentRouteResponse = route.data;
  const phase = data.phase;
  const cancellable =
    IS_TERMINAL_PHASE[phase] !== true &&
    !latched &&
    !cancel.isPending &&
    !(cancel.data !== undefined && cancel.data.status === "accepted");
  const transportUnknown = cancel.isError;

  const handleCancel = () => {
    if (!cancellable) return;
    cancel.mutate(
      {
        expected_lifecycle_revision: data.lifecycle_revision,
        idempotency_key: cancelKey,
        reason_code: reasonCode.trim() || "UPSTREAM_WITHDRAWN",
      },
      {
        onSuccess: (result) => {
          if (isDefinitiveS14NonAcceptance(result)) setLatched(true);
        },
      },
    );
  };

  // Only a definitive server rejection plus one successful authoritative
  // reload proves the previous key was never accepted; every other path —
  // unknown transport, failed refetch — retains the byte-identical command.
  const handleReload = async () => {
    if (cancel.isPending) return;
    try {
      await route.refetch({ throwOnError: true });
    } catch {
      return;
    }
    if (isDefinitiveS14NonAcceptance(cancel.data)) {
      setCancelKey(newIdempotencyKey());
      cancel.reset();
      setLatched(false);
      return;
    }
    // A successor cycle is server-eligibly cancellable again: after an
    // accepted cancel, the authoritative reload releases the latch so the
    // in-panel reload agrees with a full page reload on the same facts.
    if (
      route.data !== undefined &&
      IS_TERMINAL_PHASE[route.data.phase] !== true &&
      cancel.data?.status === "accepted"
    ) {
      cancel.reset();
    }
    setLatched(false);
  };

  const fencedEffects = cancel.data?.fenced_effects ?? null;

  return (
    <div data-testid="t16-lifecycle-panel">
      <Section
        id="t16-facts-title"
        title="当前路由（服务器权威）"
        testId="t16-facts-section"
      >
        <dl className="facts">
          <div>
            <dt>Phase</dt>
            <dd data-testid="t16-phase">{leafText(phase)}</dd>
          </div>
          <div>
            <dt>Route</dt>
            <dd data-testid="t16-route">{leafText(data.route)}</dd>
          </div>
          <div>
            <dt>Cycle</dt>
            <dd data-testid="t16-cycle">{leafText(data.cycle)}</dd>
          </div>
          <div>
            <dt>Lifecycle Revision</dt>
            <dd data-testid="t16-lifecycle-revision">
              {leafText(data.lifecycle_revision)}
            </dd>
          </div>
          <div>
            <dt>Evidence Revision</dt>
            <dd>{leafText(data.evidence_revision)}</dd>
          </div>
        </dl>
      </Section>

      {phase === "Terminating" && (
        <p
          role="status"
          aria-live="polite"
          data-testid="t16-terminating-status"
        >
          Terminating — 在途效果对账中；终止仅由服务器确认
        </p>
      )}
      {convergence === "timed_out" && phase === "Terminating" && (
        <p role="alert" data-testid="t16-poll-timeout">
          有界轮询未收敛：状态未知，需要操作员对账（bounded unknown）
        </p>
      )}
      {phase === "Terminated" && (
        <p role="status" data-testid="t16-terminated">
          Terminated — 该处理周期已被服务器封存
        </p>
      )}

      <Section
        id="t16-cancel-title"
        title="取消申请（授权上游集成方）"
        testId="t16-cancel-section"
      >
        <div className="recovery-actions t16-command-row">
          <label htmlFor="t16-cancel-reason-input">取消原因代码</label>
          <input
            id="t16-cancel-reason-input"
            data-testid="t16-cancel-reason"
            value={reasonCode}
            onChange={(event) => setReasonCode(event.target.value)}
            maxLength={200}
          />
          <button
            type="button"
            data-testid="t16-cancel-button"
            onClick={handleCancel}
            disabled={!cancellable}
          >
            {cancel.isPending ? "取消提交中…" : "取消申请"}
          </button>
          <button
            type="button"
            data-testid="t16-reload"
            onClick={() => void handleReload()}
            disabled={cancel.isPending}
          >
            重新加载权威上下文
          </button>
        </div>
        {cancel.isError && (
          <>
            <p role="alert" data-testid="t16-error-unknown">
              传输结果未知：同一幂等键可安全重试
            </p>
            <button
              type="button"
              data-testid="t16-retry"
              onClick={handleCancel}
              disabled={!cancellable}
            >
              重试（保留幂等键）
            </button>
          </>
        )}
        {transportUnknown ? null : cancel.data !== undefined ? (
          <ResultFacts result={cancel.data}>
            {fencedEffects !== null && (
              <dl data-testid="t16-cancel-fenced-effects">
                {Object.entries(fencedEffects).map(([kind, count]) => (
                  <div key={kind}>
                    <dt>{kind}</dt>
                    <dd>{leafText(count)}</dd>
                  </div>
                ))}
              </dl>
            )}
          </ResultFacts>
        ) : null}
        {latched && (
          <p role="alert" data-testid="t16-reload-required">
            命令未被接受：请重新加载权威上下文后再试
          </p>
        )}
      </Section>

      <Section
        id="t16-history-title"
        title="历史周期与运行（只读）"
        testId="t16-history-section"
      >
        {history.data === undefined ? (
          <p data-testid="t16-history-empty">—</p>
        ) : history.data.runs.length === 0 ? (
          <p data-testid="t16-history-empty">No recorded runs</p>
        ) : (
          <ol className="history-list" data-testid="t16-history-runs">
            {history.data.runs.map((run) => (
              <li key={run.run_id} data-testid="t16-history-run">
                <dl className="facts">
                  <div>
                    <dt>Cycle</dt>
                    <dd>{leafText(run.cycle)}</dd>
                  </div>
                  <div>
                    <dt>Run ID</dt>
                    <dd>{leafText(run.run_id)}</dd>
                  </div>
                  <div>
                    <dt>Status</dt>
                    <dd>{leafText(run.status)}</dd>
                  </div>
                  <div>
                    <dt>Current</dt>
                    <dd>{run.current ? "current" : "superseded"}</dd>
                  </div>
                </dl>
                {onCycleSelected !== undefined && (
                  <button
                    type="button"
                    data-testid="t16-history-run-cycle"
                    onClick={() =>
                      onCycleSelected(typeof run.cycle === "number" ? run.cycle : 0)
                    }
                    aria-current={
                      selectedCycle !== null &&
                      selectedCycle !== undefined &&
                      run.cycle === selectedCycle
                        ? "true"
                        : undefined
                    }
                  >
                    查看 Cycle {leafText(run.cycle)}
                  </button>
                )}
              </li>
            ))}
          </ol>
        )}
      </Section>
    </div>
  );
}

interface ReopenBinding {
  permissionId: string;
  artifactDigest: string;
}

/** The operator termination-settlement console.  The operator context owns
 * no reviewer session: its authoritative read seam is the released S13
 * delivery view, and every lifecycle change is an explicit typed command
 * whose exact server outcome is rendered verbatim. */
export function T16SettlementPanel({
  applicationId,
}: {
  applicationId: string | null;
}) {
  const delivery = useS13Delivery(applicationId);
  const settle = useS14Settle(applicationId ?? "");
  const notify = useS14ProcessNotification();
  const grant = useS14GrantReopenPermission(applicationId ?? "");
  const reopen = useS14Reopen(applicationId ?? "");
  const [settleKey, setSettleKey] = useState<string>(newIdempotencyKey);
  const [grantKey, setGrantKey] = useState<string>(newIdempotencyKey);
  const [reopenKey, setReopenKey] = useState<string>(newIdempotencyKey);
  const [approverSubject, setApproverSubject] = useState("");
  const [permissionId, setPermissionId] = useState("");
  const [targetPhase, setTargetPhase] = useState<"Intake" | "Assembly">("Intake");
  const [binding, setBinding] = useState<ReopenBinding | null>(null);
  const [latched, setLatched] = useState(false);
  const revisionRef = useRef<number | null>(null);

  if (!applicationId) {
    return (
      <div data-testid="t16-settlement-panel">
        <p data-testid="t16-settlement-no-application" role="status" aria-live="polite">
          请选择应用以执行终止结算
        </p>
      </div>
    );
  }
  if (delivery.isPending || (delivery.data === undefined && !delivery.isError)) {
    return (
      <div data-testid="t16-settlement-panel">
        <p data-testid="t16-settlement-loading" role="status" aria-live="polite">
          正在加载终止结算视图…
        </p>
      </div>
    );
  }
  if (delivery.isError) {
    return (
      <div data-testid="t16-settlement-panel">
        <QueryErrorState
          error={delivery.error}
          onReload={() => void delivery.refetch()}
          isReloading={delivery.isFetching}
        />
      </div>
    );
  }

  const view = delivery.data;
  const phase = view.phase;
  revisionRef.current = view.lifecycle_revision;

  const anyPending =
    settle.isPending || notify.isPending || grant.isPending || reopen.isPending;

  const handleSettle = () => {
    if (anyPending || latched || phase !== "Terminating") return;
    settle.mutate(
      {
        expected_lifecycle_revision: view.lifecycle_revision,
        idempotency_key: settleKey,
      },
      {
        onSuccess: (data) =>
          handleS14Outcome(data, () => setLatched(true), () =>
            setSettleKey(newIdempotencyKey()),
          ),
      },
    );
  };

  const handleNotify = () => {
    if (anyPending || latched) return;
    notify.mutate(undefined, {
      onSuccess: (data) =>
        handleS14Outcome(data, () => setLatched(true), () => undefined),
    });
  };

  const handleGrant = () => {
    if (anyPending || latched || phase !== "Terminated") return;
    if (!approverSubject.trim() || !permissionId.trim()) return;
    grant.mutate(
      {
        expected_lifecycle_revision: view.lifecycle_revision,
        approver_subject: approverSubject.trim(),
        permission_id: permissionId.trim(),
        idempotency_key: grantKey,
        ttl_seconds: 3600,
      },
      {
        onSuccess: (data) => {
          if (data.status === "accepted" && data.permission_id && data.artifact_release_digest) {
            setBinding({
              permissionId: data.permission_id,
              artifactDigest: data.artifact_release_digest,
            });
          }
          handleS14Outcome(data, () => setLatched(true), () =>
            setGrantKey(newIdempotencyKey()),
          );
        },
      },
    );
  };

  const handleReopen = () => {
    if (anyPending || latched || binding === null || phase !== "Terminated") return;
    reopen.mutate(
      {
        expected_lifecycle_revision: view.lifecycle_revision,
        idempotency_key: reopenKey,
        target_phase: targetPhase,
        reopen_policy: {
          permission_id: binding.permissionId,
          release_digest: binding.artifactDigest,
        },
      },
      {
        onSuccess: (data) =>
          handleS14Outcome(data, () => setLatched(true), () =>
            setReopenKey(newIdempotencyKey()),
          ),
      },
    );
  };

  // Only a successful authoritative refresh after a definitive non-acceptance
  // rotates a semantic key; unknown transports retain theirs verbatim.
  const handleReload = async () => {
    if (anyPending) return;
    try {
      await delivery.refetch({ throwOnError: true });
    } catch {
      return;
    }
    let rotated = false;
    if (isDefinitiveS14NonAcceptance(settle.data)) {
      setSettleKey(newIdempotencyKey());
      rotated = true;
    }
    if (isDefinitiveS14NonAcceptance(grant.data)) {
      setGrantKey(newIdempotencyKey());
      rotated = true;
    }
    if (isDefinitiveS14NonAcceptance(reopen.data)) {
      setReopenKey(newIdempotencyKey());
      rotated = true;
    }
    if (rotated) {
      settle.reset();
      notify.reset();
      grant.reset();
      reopen.reset();
    }
    setLatched(false);
  };

  return (
    <div data-testid="t16-settlement-panel">
      <Section
        id="t16-settlement-facts-title"
        title="生命周期事实（操作员权威读取）"
        testId="t16-settlement-facts-section"
      >
        <dl className="facts">
          <div>
            <dt>Phase</dt>
            <dd data-testid="t16-settlement-phase">{leafText(phase)}</dd>
          </div>
          <div>
            <dt>Cycle</dt>
            <dd data-testid="t16-settlement-cycle">{leafText(view.cycle)}</dd>
          </div>
          <div>
            <dt>Lifecycle Revision</dt>
            <dd data-testid="t16-settlement-lifecycle-revision">
              {leafText(view.lifecycle_revision)}
            </dd>
          </div>
        </dl>
      </Section>

      <Section
        id="t16-settle-title"
        title="终止结算（授权操作员）"
        testId="t16-settle-section"
      >
        <div className="recovery-actions">
          <button
            type="button"
            data-testid="t16-settle-button"
            onClick={handleSettle}
            disabled={anyPending || latched || phase !== "Terminating"}
          >
            {settle.isPending ? "结算提交中…" : "结算终止"}
          </button>
          <button
            type="button"
            data-testid="t16-notification-button"
            onClick={handleNotify}
            disabled={anyPending || latched}
          >
            {notify.isPending ? "通知处理中…" : "处理终止通知"}
          </button>
          <button
            type="button"
            data-testid="t16-reload"
            onClick={() => void handleReload()}
            disabled={anyPending}
          >
            重新加载权威上下文
          </button>
        </div>
        {settle.isError && (
          <p role="alert" data-testid="t16-settle-unknown">
            结算传输结果未知：同一幂等键可安全重试
          </p>
        )}
        {!settle.isError && settle.data !== undefined && (
          <ResultFacts result={settle.data}>
            {settle.data.unresolved_effects !== null &&
            settle.data.unresolved_effects !== undefined &&
            settle.data.unresolved_effects.length > 0 ? (
              <ul data-testid="t16-unresolved-effects">
                {settle.data.unresolved_effects.map((effect) => (
                  <li key={`${effect.kind}:${effect.id}`}>
                    {effect.kind} · {leafText(effect.detail)}
                  </li>
                ))}
              </ul>
            ) : null}
            {settle.data.settled_effects !== null &&
            settle.data.settled_effects !== undefined &&
            settle.data.settled_effects.length > 0 ? (
              <ul data-testid="t16-settled-effects">
                {settle.data.settled_effects.map((effect) => (
                  <li key={`${effect.kind}:${effect.id}`}>
                    {effect.kind} · {leafText(effect.result)}
                  </li>
                ))}
              </ul>
            ) : null}
          </ResultFacts>
        )}
        {!notify.isError && notify.data !== undefined && (
          <p role="status" aria-live="polite" data-testid="t16-notification-status">
            {leafText(notify.data.status)}
          </p>
        )}
        {notify.isError && (
          <>
            <p role="alert" data-testid="t16-notification-unknown">
              通知传输结果未知：需要对账，请重试或重新加载权威上下文
            </p>
            <button
              type="button"
              data-testid="t16-notification-retry"
              onClick={handleNotify}
              disabled={anyPending || latched}
            >
              重试处理终止通知
            </button>
          </>
        )}
      </Section>

      <Section
        id="t16-reopen-title"
        title="显式重开（独立许可 + 精确摘要）"
        testId="t16-reopen-section"
      >
        <div className="recovery-actions t16-command-row">
          <label htmlFor="t16-grant-approver-input">审批人主体</label>
          <input
            id="t16-grant-approver-input"
            data-testid="t16-grant-approver"
            value={approverSubject}
            onChange={(event) => setApproverSubject(event.target.value)}
            maxLength={200}
          />
          <label htmlFor="t16-grant-permission-input">许可标识</label>
          <input
            id="t16-grant-permission-input"
            data-testid="t16-grant-permission-id"
            value={permissionId}
            onChange={(event) => setPermissionId(event.target.value)}
            maxLength={200}
          />
          <button
            type="button"
            data-testid="t16-grant-button"
            onClick={handleGrant}
            disabled={
              anyPending ||
              latched ||
              phase !== "Terminated" ||
              !approverSubject.trim() ||
              !permissionId.trim()
            }
          >
            {grant.isPending ? "许可授予中…" : "授予重开许可"}
          </button>
        </div>
        {!grant.isError && grant.data !== undefined && (
          <ResultFacts result={grant.data}>
            {grant.data.permission_id !== null &&
            grant.data.permission_id !== undefined &&
            grant.data.artifact_release_digest ? (
              <p data-testid="t16-grant-binding" className="t16-binding">
                {grant.data.permission_id} · {grant.data.artifact_release_digest}
              </p>
            ) : null}
          </ResultFacts>
        )}
        <div className="recovery-actions t16-command-row">
          <label htmlFor="t16-reopen-target-select">目标阶段</label>
          <select
            id="t16-reopen-target-select"
            data-testid="t16-reopen-target"
            value={targetPhase}
            onChange={(event) =>
              setTargetPhase(event.target.value === "Assembly" ? "Assembly" : "Intake")
            }
          >
            <option value="Intake">Intake</option>
            <option value="Assembly">Assembly</option>
          </select>
          <button
            type="button"
            data-testid="t16-reopen-button"
            onClick={handleReopen}
            disabled={
              anyPending ||
              latched ||
              binding === null ||
              phase !== "Terminated"
            }
          >
            {reopen.isPending ? "重开提交中…" : "重开新周期"}
          </button>
        </div>
        {!reopen.isError && reopen.data !== undefined && (
          <ResultFacts result={reopen.data}>
            <p data-testid="t16-reopen-result">
              cycle {leafText(reopen.data.cycle)} · {leafText(reopen.data.phase)} ·
              predecessor {leafText(reopen.data.predecessor_cycle)} · target{" "}
              {leafText(reopen.data.target_phase)}
            </p>
          </ResultFacts>
        )}
        {latched && (
          <p role="alert" data-testid="t16-reload-required">
            命令未被接受：请重新加载权威上下文后再试
          </p>
        )}
      </Section>
    </div>
  );
}
