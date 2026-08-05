import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { HttpError, type RecoveryWorkResponse } from "../api/client";
import {
  WORK_KEY,
  useCurrentRoute,
  useRecoveryWork,
  useVerifyRecovery,
  type VerifyRecoveryCommand,
} from "../api/hooks";
import { Button } from "./ui/button";

function newIdempotencyKey(): string {
  return crypto.randomUUID();
}

function attemptLabel(attempt: {
  attempt: number;
  classification: string;
  status: string;
}): string {
  return `${attempt.attempt} · ${attempt.classification} · ${attempt.status}`;
}

function describeVerifyStatus(
  verify: {
    isPending: boolean;
    isError: boolean;
    isSuccess: boolean;
    error: unknown;
  },
  requiresReload: boolean,
  conflictReason: string | null,
): string {
  if (verify.isPending) return "恢复验证提交中…";
  if (requiresReload && conflictReason !== null) {
    return `恢复验证未接受（${conflictReason}）：请重新加载权威上下文后再试`;
  }
  if (verify.isError) {
    if (!(verify.error instanceof HttpError)) {
      return "结果未知：网络未确认，重试将使用同一幂等键";
    }
    return verify.error.reasonCode ?? "恢复验证被拒绝";
  }
  if (verify.isSuccess) return "恢复事实已接受";
  return "等待操作";
}

function GatePanel({ applicationId }: { applicationId: string }) {
  const gate = useCurrentRoute(applicationId);
  if (gate.isPending) {
    return <p data-testid="gate-loading">路由加载中…</p>;
  }
  if (gate.isError) {
    return <p data-testid="gate-error">当前路由不可用</p>;
  }
  return (
    <section className="panel" data-testid="gate-panel" aria-labelledby="gate-title">
      <h3 id="gate-title">当前路由（服务端权威）</h3>
      <dl className="facts">
        <div>
          <dt>阶段</dt>
          <dd data-testid="gate-phase">{gate.data?.phase}</dd>
        </div>
        <div>
          <dt>路由</dt>
          <dd data-testid="gate-route">{gate.data?.route}</dd>
        </div>
        <div>
          <dt>当前性</dt>
          <dd data-testid="gate-currentness">{gate.data?.currentness_reason}</dd>
        </div>
      </dl>
    </section>
  );
}

function WorkFacts({ work }: { work: RecoveryWorkResponse }) {
  return (
    <dl className="facts">
      <div>
        <dt>状态</dt>
        <dd data-testid="recovery-status">{work.status}</dd>
      </div>
      <div>
        <dt>阶段</dt>
        <dd data-testid="recovery-phase">{work.phase}</dd>
      </div>
      <div>
        <dt>路由</dt>
        <dd data-testid="recovery-route">{work.route}</dd>
      </div>
      <div>
        <dt>生命周期修订</dt>
        <dd data-testid="recovery-lifecycle-revision">{work.lifecycle_revision}</dd>
      </div>
      <div>
        <dt>投影水位</dt>
        <dd data-testid="recovery-watermark">{work.projection_watermark}</dd>
      </div>
      <div>
        <dt>主要原因</dt>
        <dd data-testid="recovery-primary-reason">{work.primary_reason_code}</dd>
      </div>
      <div>
        <dt>相关原因</dt>
        <dd data-testid="recovery-related-reasons">
          {work.related_reason_codes.length === 0
            ? "None"
            : work.related_reason_codes.join(", ")}
        </dd>
      </div>
      <div>
        <dt>操作</dt>
        <dd data-testid="recovery-operation">{work.operation}</dd>
      </div>
      <div>
        <dt>依赖</dt>
        <dd data-testid="recovery-dependency">{work.dependency}</dd>
      </div>
      <div>
        <dt>责任方</dt>
        <dd data-testid="recovery-responsible-party">{work.responsible_party}</dd>
      </div>
      <div>
        <dt>恢复动作</dt>
        <dd data-testid="recovery-action">{work.recovery_action}</dd>
      </div>
      <div>
        <dt>恢复目标</dt>
        <dd data-testid="recovery-target">{work.recovery_target}</dd>
      </div>
      <div>
        <dt>判定标准</dt>
        <dd data-testid="recovery-criterion-id">{work.criterion.id}</dd>
      </div>
      <div>
        <dt>判定摘要</dt>
        <dd data-testid="recovery-criterion-digest">{work.criterion.digest}</dd>
      </div>
      <div>
        <dt>恢复事实数</dt>
        <dd data-testid="recovery-fact-count">{work.recovery_fact_count}</dd>
      </div>
      <div>
        <dt>解决次数</dt>
        <dd data-testid="recovery-resolution-count">{work.resolution_count}</dd>
      </div>
    </dl>
  );
}

export default function RecoveryWorkPanel({ workId }: { workId: string }) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  const queryClient = useQueryClient();
  const work = useRecoveryWork(workId);
  const verify = useVerifyRecovery(workId);
  const [requiresReload, setRequiresReload] = useState(false);
  const [conflictReason, setConflictReason] = useState<string | null>(null);
  const [verifyKey, setVerifyKey] = useState<string>(newIdempotencyKey);

  useEffect(() => {
    headingRef.current?.focus();
  }, [workId]);

  useEffect(() => {
    if (
      verify.isError &&
      verify.error instanceof HttpError &&
      verify.error.status === 409
    ) {
      setRequiresReload(true);
      setConflictReason(verify.error.reasonCode ?? "conflict");
    }
  }, [verify.isError, verify.error]);

  if (work.isPending || work.data === undefined) {
    const notFound =
      work.isError && work.error instanceof HttpError && work.error.status === 404;
    return (
      <section
        className="panel"
        data-testid="recovery-panel"
        aria-labelledby="recovery-title"
      >
        <h2 id="recovery-title" tabIndex={-1} ref={headingRef}>
          恢复工作
        </h2>
        {work.isPending ? (
          <p data-testid="recovery-loading">恢复工作加载中…</p>
        ) : (
          <p data-testid="recovery-error">
            {notFound ? "未找到或无权访问" : "恢复工作不可用"}
          </p>
        )}
      </section>
    );
  }

  const data = work.data;
  const canVerify =
    data.can_verify === true &&
    data.status === "open" &&
    !requiresReload &&
    !verify.isSuccess;
  const command: VerifyRecoveryCommand = {
    expected_lifecycle_revision: data.lifecycle_revision,
    expected_criterion_digest: data.criterion.digest,
    idempotency_key: verifyKey,
  };

  const verifyOutcome = describeVerifyStatus(
    verify,
    requiresReload,
    conflictReason,
  );
  const transportUnknown =
    verify.isError && !(verify.error instanceof HttpError);

  const handleVerify = () => {
    if (!canVerify) return;
    verify.mutate(command);
  };

  const handleReload = async () => {
    // A command outcome that is still pending must never be reset or given
    // a fresh semantic key, and an accepted outcome must never be cleared
    // by a reload.
    if (verify.isPending) return;
    // Authoritative reload.  A failed refetch must never clear the conflict
    // fence nor rotate the semantic idempotency key, so the refetch throws
    // and the fence is kept on failure.
    try {
      await queryClient.refetchQueries(
        { queryKey: WORK_KEY(workId) },
        { throwOnError: true },
      );
    } catch {
      return;
    }
    await queryClient.invalidateQueries({ queryKey: ["s01"] });
    const knownRejection = verify.isError && verify.error instanceof HttpError;
    if (knownRejection) {
      // A known server rejection (e.g. 409) proves the previous key was
      // never accepted; a fresh semantic key is safe.  An unknown transport
      // outcome keeps the original key so a retry replays idempotently, and
      // an accepted outcome keeps the latch until server-owned detail
      // converges.
      setVerifyKey(newIdempotencyKey());
      verify.reset();
      setRequiresReload(false);
      setConflictReason(null);
    }
  };

  return (
    <section
      className="panel"
      data-testid="recovery-panel"
      aria-labelledby="recovery-title"
    >
      <h2 id="recovery-title" tabIndex={-1} ref={headingRef}>
        恢复工作
      </h2>
      <WorkFacts work={data} />
      <ol className="history-list" data-testid="recovery-attempts">
        {data.attempts.map((attempt) => (
          <li key={`${attempt.attempt}`}>{attemptLabel(attempt)}</li>
        ))}
      </ol>
      <div className="recovery-actions" data-testid="recovery-actions">
        <Button
          variant="secondary"
          onClick={handleReload}
          disabled={verify.isPending}
        >
          重新加载
        </Button>
        <Button onClick={handleVerify} disabled={!canVerify || verify.isPending}>
          验证恢复
        </Button>
        {transportUnknown && data.status === "open" && (
          <Button variant="outline" onClick={handleVerify}>
            重试
          </Button>
        )}
      </div>
      <p role="status" aria-live="polite" data-testid="recovery-command-status">
        {verifyOutcome}
      </p>
      {work.isError && (
        <p
          className="text-sm text-muted-foreground"
          data-testid="recovery-refetch-error"
        >
          状态刷新失败：显示上次已确认的服务端状态
        </p>
      )}
      {!data.can_verify && (
        <p className="text-sm text-muted-foreground" data-testid="recovery-role-note">
          当前角色无法执行恢复验证
        </p>
      )}
      {data.status === "resolved" && !data.can_verify && (
        <GatePanel applicationId={data.application_id} />
      )}
    </section>
  );
}
