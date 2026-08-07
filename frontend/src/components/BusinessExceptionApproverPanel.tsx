import { useState } from "react";

import { HttpError, isDefinitiveRejection } from "../api/client";
import {
  useBusinessExceptionView,
  useClaimExceptionWorkItem,
  useDecideBusinessException,
  type ExceptionClaimCommand,
  type ExceptionDecisionCommand,
} from "../api/hooks";
import { Button } from "./ui/button";

function newIdempotencyKey(): string {
  return crypto.randomUUID();
}

type PendingCommand =
  | { action: "claim"; command: ExceptionClaimCommand }
  | { action: "decide"; command: ExceptionDecisionCommand };

/** The Exception Approver panel for the dedicated ``/controlled/s05/react``
 * shell.  It renders the server-owned minimized view, claims with the exact
 * server context, and offers approve/reject only when the server action list
 * says so.  It never issues route/expire/invalidate/operations commands and
 * never optimistically transitions any Lifecycle state. */
export default function BusinessExceptionApproverPanel({
  requestId,
}: {
  requestId: string | null;
}) {
  const view = useBusinessExceptionView(requestId);
  const workItemId = view.data?.work_item_id ?? "";
  const claim = useClaimExceptionWorkItem(workItemId);
  const decide = useDecideBusinessException(requestId ?? "");
  const [decisionKey, setDecisionKey] = useState(newIdempotencyKey);
  const [pendingCommand, setPendingCommand] = useState<PendingCommand | null>(
    null,
  );
  const [outcome, setOutcome] = useState<{
    text: string;
    reasonCode: string | null;
    unknown: boolean;
  } | null>(null);

  const active = pendingCommand === null ? null : pendingCommand.action;
  const anyPending = claim.isPending || decide.isPending;
  const transportUnknown =
    active !== null &&
    anyPending === false &&
    ((active === "claim" && claim.isError) ||
      (active === "decide" && decide.isError)) &&
    outcome !== null &&
    outcome.unknown === true;

  const handleRejection = (error: Error) => {
    if (!isDefinitiveRejection(error)) {
      setOutcome({
        text: "结果未知：网络未确认，重试将使用同一幂等键",
        reasonCode: null,
        unknown: true,
      });
      return;
    }
    setPendingCommand(null);
    setOutcome({
      text: "命令未接受，请重新加载权威视图后再试",
      reasonCode: error.reasonCode ?? null,
      unknown: false,
    });
  };

  const handleClaim = () => {
    if (view.data === undefined || anyPending || pendingCommand !== null) return;
    if (!view.data.actions.includes("claim")) return;
    const command: ExceptionClaimCommand = {
      expected_context: view.data.command_context,
    };
    setPendingCommand({ action: "claim", command });
    setOutcome(null);
    claim.mutate(command, {
      onSuccess: () => {
        setPendingCommand(null);
        setOutcome({ text: "已认领，可进行决策", reasonCode: null, unknown: false });
      },
      onError: handleRejection,
    });
  };

  const handleDecide = (decision: "approved" | "rejected") => {
    if (view.data === undefined || anyPending || pendingCommand !== null) return;
    if (!view.data.actions.includes("decide")) return;
    const command: ExceptionDecisionCommand = {
      work_item_id: view.data.work_item_id,
      decision,
      reason_code:
        decision === "approved"
          ? "DOCUMENTED_VARIANCE_ACCEPTED"
          : "DOCUMENTED_VARIANCE_REJECTED",
      expected_fence: view.data.claim_fence,
      expected_context: view.data.command_context,
      idempotency_key: decisionKey,
    };
    setPendingCommand({ action: "decide", command });
    setOutcome(null);
    decide.mutate(command, {
      onSuccess: () => {
        setDecisionKey(newIdempotencyKey());
        setPendingCommand(null);
        setOutcome({
          text: decision === "approved" ? "已批准" : "已拒绝",
          reasonCode: null,
          unknown: false,
        });
      },
      onError: handleRejection,
    });
  };

  const handleRetry = () => {
    if (pendingCommand === null || anyPending) return;
    if (pendingCommand.action === "claim") {
      claim.mutate(pendingCommand.command, {
        onSuccess: () => {
          setPendingCommand(null);
          setOutcome({
            text: "已认领，可进行决策",
            reasonCode: null,
            unknown: false,
          });
        },
        onError: handleRejection,
      });
    } else {
      decide.mutate(pendingCommand.command, {
        onSuccess: () => {
          setDecisionKey(newIdempotencyKey());
          setPendingCommand(null);
          setOutcome({ text: "已决策", reasonCode: null, unknown: false });
        },
        onError: handleRejection,
      });
    }
  };

  if (requestId === null) {
    return (
      <section className="panel" data-testid="approver-empty">
        <h2>业务例外审批</h2>
        <p>未指定请求编号（request 参数）</p>
      </section>
    );
  }
  if (view.isPending) {
    return (
      <section className="panel" data-testid="approver-loading">
        <h2>业务例外审批</h2>
        <p>请求加载中…</p>
      </section>
    );
  }
  if (view.isError || view.data === undefined) {
    const notFound =
      view.error instanceof HttpError && view.error.status === 404;
    return (
      <section className="panel" data-testid="approver-not-found">
        <h2>业务例外审批</h2>
        <p>{notFound ? "未找到或无权访问" : "请求不可用"}</p>
      </section>
    );
  }
  const data = view.data;
  const canClaim =
    data.actions.includes("claim") &&
    !anyPending &&
    pendingCommand === null;
  const canDecide =
    data.actions.includes("decide") &&
    !anyPending &&
    pendingCommand === null;

  return (
    <section
      className="panel"
      data-testid="approver-view"
      aria-labelledby="approver-title"
    >
      <h2 id="approver-title">业务例外审批（服务端权威）</h2>
      <dl className="facts">
        <div>
          <dt>请求编号</dt>
          <dd data-testid="approver-request-id">{data.request_id}</dd>
        </div>
        <div>
          <dt>状态</dt>
          <dd data-testid="approver-status">{data.status}</dd>
        </div>
        <div>
          <dt>当前性</dt>
          <dd data-testid="approver-currentness">{data.currentness_reason}</dd>
        </div>
        <div>
          <dt>请求者</dt>
          <dd data-testid="approver-requester">
            {data.requester.subject} · {data.requester.role}
          </dd>
        </div>
        <div>
          <dt>申请理由</dt>
          <dd data-testid="approver-request-reason">{data.request_reason}</dd>
        </div>
        <div>
          <dt>范围</dt>
          <dd data-testid="approver-scope">{data.scope}</dd>
        </div>
        <div>
          <dt>到期（epoch）</dt>
          <dd data-testid="approver-expiry">{data.expires_at}</dd>
        </div>
        <div>
          <dt>认领状态</dt>
          <dd data-testid="approver-claim-status">{data.claim_status}</dd>
        </div>
        <div>
          <dt>认领围栏</dt>
          <dd data-testid="approver-claim-fence">{data.claim_fence}</dd>
        </div>
      </dl>
      <h3>发现（不改写）</h3>
      <dl className="facts">
        <div>
          <dt>规则</dt>
          <dd data-testid="approver-finding-rule">{data.finding.rule_id}</dd>
        </div>
        <div>
          <dt>机器判定</dt>
          <dd data-testid="approver-verdict">{data.finding.verdict}</dd>
        </div>
        <div>
          <dt>严重度</dt>
          <dd data-testid="approver-severity">{data.finding.severity}</dd>
        </div>
        <div>
          <dt>原因</dt>
          <dd data-testid="approver-finding-reason">{data.finding.reason_code}</dd>
        </div>
      </dl>
      <h3>证据引用（已最小化）</h3>
      <ul data-testid="approver-evidence-references">
        {data.evidence_references.map((reference) => (
          <li key={reference.observation_id ?? "reference"}>
            {reference.document_role ?? "None"} · {reference.field ?? "None"}
            {" · "}
            {reference.source_page ?? "None"} · {reference.source_region ?? "None"}
          </li>
        ))}
      </ul>
      <div className="recovery-actions" data-testid="approver-actions">
        {canClaim && (
          <Button
            onClick={handleClaim}
            disabled={claim.isPending}
            data-testid="approver-claim-button"
          >
            {claim.isPending ? "认领中…" : "认领"}
          </Button>
        )}
        {canDecide && (
          <>
            <Button
              onClick={() => handleDecide("approved")}
              disabled={decide.isPending}
              data-testid="approver-approve-button"
            >
              {decide.isPending ? "提交中…" : "批准"}
            </Button>
            <Button
              variant="outline"
              onClick={() => handleDecide("rejected")}
              disabled={decide.isPending}
              data-testid="approver-reject-button"
            >
              拒绝
            </Button>
          </>
        )}
        {transportUnknown && pendingCommand !== null && (
          <Button
            variant="outline"
            onClick={handleRetry}
            data-testid="approver-retry-button"
          >
            重试
          </Button>
        )}
      </div>
      <p role="status" aria-live="polite" data-testid="approver-outcome">
        {outcome === null
          ? "等待操作"
          : outcome.reasonCode !== null
            ? `${outcome.text}（${outcome.reasonCode}）`
            : outcome.text}
      </p>
    </section>
  );
}
