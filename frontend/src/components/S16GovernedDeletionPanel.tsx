import { useEffect, useRef, useState } from "react";

import {
  HttpError,
  isDefinitiveS16Rejection,
  type S16ManifestEntry,
  type S16PreflightResponse,
  type S16QueryResponse,
} from "../api/client";
import {
  S16_REQUEST_KEY,
  clearApplicationScopedCache,
  s16RequestQueryFn,
  useS16Approve,
  useS16Cancel,
  useS16Commit,
  useS16ImposeHold,
  useS16Preflight,
  useS16Process,
  useS16Query,
  useS16Receipt,
  useS16ReleaseHold,
  useS16Repair,
} from "../api/hooks";
import { useQueryClient } from "@tanstack/react-query";

/** The closed S16 error-state mapping: the exact registered envelope code
 * renders beside one stable label; no identifiers are invented. */
function s16ErrorState(error: Error): {
  code: string;
  label: string;
  testId: string;
} | null {
  if (!(error instanceof HttpError)) return null;
  const code = error.errorCode ?? `S16_HTTP_${error.status}`;
  if (error.status === 403) {
    return { code, label: "授权被拒绝", testId: "s16-error-forbidden" };
  }
  if (error.status === 404) {
    return { code, label: "不存在", testId: "s16-error-not-found" };
  }
  if (error.status === 409) {
    return {
      code: error.reasonCode ?? code,
      label: "命令被门禁阻止",
      testId: "s16-error-blocked",
    };
  }
  if (error.status === 422) {
    return { code, label: "命令无效", testId: "s16-error-invalid" };
  }
  if (error.status === 503) {
    return { code, label: "平面不可用", testId: "s16-error-unavailable" };
  }
  return { code, label: "请求失败", testId: "s16-error-unavailable" };
}

function S16ErrorState({ error }: { error: Error }) {
  const state = s16ErrorState(error);
  if (state === null) {
    return (
      <p role="status" aria-live="polite" data-testid="s16-unknown-outcome">
        结果未知：网络未确认，请保留同一操作标识并查询权威状态
      </p>
    );
  }
  return (
    <section className="panel" data-testid={state.testId} role="alert">
      <p>{state.label}</p>
      <p data-testid="s16-error-code">{state.code}</p>
    </section>
  );
}

function EntryRow({ entry }: { entry: S16ManifestEntry }) {
  return (
    <tr data-testid="s16-entry-row">
      <td data-testid="s16-entry-owner">{entry.owner_id}</td>
      <td data-testid="s16-entry-class">{entry.copy_class}</td>
      <td data-testid="s16-entry-count">{entry.count}</td>
      <td data-testid="s16-entry-shared">{entry.shared_state}</td>
      <td data-testid="s16-entry-action">{entry.planned_action}</td>
      <td data-testid="s16-entry-due">
        {entry.retention_due_at == null
          ? "—"
          : new Date(entry.retention_due_at * 1000).toISOString()}
      </td>
      <td data-testid="s16-entry-digest" className="break-all">
        {entry.content_sha256.slice(0, 16)}…
      </td>
    </tr>
  );
}

function LegalHoldSection({
  scopeFingerprint,
  holds,
}: {
  scopeFingerprint: string;
  holds: S16QueryResponse["legal_holds"];
}) {
  const [reasonCode, setReasonCode] = useState<
    "litigation" | "regulatory" | "internal_investigation"
  >("litigation");
  const [owner, setOwner] = useState<
    "s01" | "s02" | "s12" | "backup" | "s17-disabled" | "all"
  >("all");
  const impose = useS16ImposeHold();
  const release = useS16ReleaseHold();

  const handleImpose = () => {
    impose.mutate({
      scopeFingerprint,
      reasonCode,
      owner,
      effectiveTime: Math.floor(Date.now() / 1000),
      idempotencyKey: `s16-hold-${scopeFingerprint.slice(0, 12)}-${Date.now()}`,
    });
  };

  return (
    <section className="panel" data-testid="s16-legal-holds" aria-labelledby="s16-holds-title">
      <h3 id="s16-holds-title">法律保全（与 commit 同一账本仲裁）</h3>
      <dl className="facts">
        {holds.map((hold) => (
          <div key={hold.hold_id} data-testid="s16-hold-entry">
            <dt>
              {hold.reason_code} · gen {hold.generation}
            </dt>
            <dd>
              {hold.owner} · 生效 {hold.effective_time}
              {hold.expiry !== null && hold.expiry !== undefined
                ? ` · 期限 ${hold.expiry}`
                : ""}
              {hold.released ? " · 已释放" : " · 生效中"}
            </dd>
            {!hold.released && (
              <button
                type="button"
                data-testid={`s16-release-hold-${hold.hold_id}`}
                disabled={release.isPending}
                onClick={() =>
                  release.mutate({
                    holdId: hold.hold_id,
                    idempotencyKey: `s16-release-${hold.hold_id}`,
                  })
                }
              >
                {release.isPending ? "提交中…" : "释放"}
              </button>
            )}
          </div>
        ))}
      </dl>
      {holds.length === 0 && (
        <p data-testid="s16-holds-empty">当前无法律保全</p>
      )}
      <div className="demo-controls">
        <label htmlFor="s16-hold-reason">保全理由（封闭词表）</label>
        <select
          id="s16-hold-reason"
          data-testid="s16-hold-reason"
          value={reasonCode}
          onChange={(event) =>
            setReasonCode(
              event.target.value as
                | "litigation"
                | "regulatory"
                | "internal_investigation",
            )
          }
          disabled={impose.isPending}
        >
          <option value="litigation">litigation</option>
          <option value="regulatory">regulatory</option>
          <option value="internal_investigation">
            internal_investigation
          </option>
        </select>
        <label htmlFor="s16-hold-owner">保全对象（封闭词表）</label>
        <select
          id="s16-hold-owner"
          data-testid="s16-hold-owner"
          value={owner}
          onChange={(event) =>
            setOwner(
              event.target.value as
                | "s01"
                | "s02"
                | "s12"
                | "backup"
                | "s17-disabled"
                | "all",
            )
          }
          disabled={impose.isPending}
        >
          {["s01", "s02", "s12", "backup", "s17-disabled", "all"].map(
            (value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ),
          )}
        </select>
        <button
          type="button"
          data-testid="s16-impose-hold-button"
          disabled={impose.isPending}
          onClick={handleImpose}
        >
          {impose.isPending ? "提交中…" : "实施保全"}
        </button>
      </div>
      {impose.error !== null && <S16ErrorState error={impose.error} />}
      {release.error !== null && <S16ErrorState error={release.error} />}
    </section>
  );
}

function ManifestTable({ preflight }: { preflight: S16PreflightResponse }) {
  return (
    <section className="panel" data-testid="s16-manifest" aria-labelledby="s16-manifest-title">
      <h3 id="s16-manifest-title">聚合 dry-run 清单（服务端权威，仅摘要与指纹）</h3>
      <dl className="facts">
        <div>
          <dt>请求标识</dt>
          <dd data-testid="s16-request-id">{preflight.request_id}</dd>
        </div>
        <div>
          <dt>申请引用（会话内展示）</dt>
          <dd data-testid="s16-application-reference">
            {preflight.application_reference}
          </dd>
        </div>
        <div>
          <dt>scope 指纹</dt>
          <dd className="break-all" data-testid="s16-scope-fingerprint">
            {preflight.scope_fingerprint}
          </dd>
        </div>
        <div>
          <dt>清单摘要</dt>
          <dd className="break-all" data-testid="s16-manifest-digest">
            {preflight.manifest_digest}
          </dd>
        </div>
        <div>
          <dt>保留到期</dt>
          <dd data-testid="s16-retention-due">
            {preflight.retention_due == null
              ? "—"
              : new Date(preflight.retention_due * 1000).toISOString()}
          </dd>
        </div>
        <div>
          <dt>提前删除</dt>
          <dd data-testid="s16-early-deletion">
            {preflight.early_deletion ? "是（需双审批）" : "否"}
          </dd>
        </div>
      </dl>
      <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr>
            <th>owner</th>
            <th>copy class</th>
            <th>数量</th>
            <th>共享</th>
            <th>计划动作</th>
            <th>保留到期</th>
            <th>内容摘要</th>
          </tr>
        </thead>
        <tbody>
          {preflight.entries.map((entry) => (
            <EntryRow key={`${entry.owner_id}:${entry.copy_class}`} entry={entry} />
          ))}
        </tbody>
      </table>
      </div>
    </section>
  );
}

function ApprovalSection({
  preflight,
  approved,
  onApproved,
}: {
  preflight: S16PreflightResponse;
  approved: number;
  onApproved: () => void;
}) {
  const [approverToken, setApproverToken] = useState("");
  const [approverIndex, setApproverIndex] = useState(() => approved + 1);
  const approve = useS16Approve();
  const [unknown, setUnknown] = useState(false);

  const handleApprove = () => {
    if (approverToken === "") return;
    approve.mutate(
      {
        requestId: preflight.request_id,
        manifestDigest: preflight.manifest_digest,
        idempotencyKey: `s16-approve-${approverIndex}-${preflight.request_id}`,
        approverToken,
      },
      {
        onSuccess: () => {
          setApproverToken("");
          setApproverIndex((current) => current + 1);
          onApproved();
        },
        onError: (error) => {
          // R3 (P1-15): an approver-surface 403 invalidates only the
          // approver identity — token, index and any previous approval
          // error are cleared so the stale approver can never be reused.
          if (error instanceof HttpError && error.status === 403) {
            setApproverToken("");
            setUnknown(false);
            approve.reset();
            return;
          }
          if (!isDefinitiveS16Rejection(error)) setUnknown(true);
        },
      },
    );
  };

  return (
    <section className="panel" data-testid="s16-approvals" aria-labelledby="s16-approvals-title">
      <h3 id="s16-approvals-title">提前删除双审批</h3>
      <p data-testid="s16-approved-count">
        已批准 {approved} / 2（两名互异审批人，均不同于申请人）
      </p>
      {approve.error !== null && <S16ErrorState error={approve.error} />}
      {unknown && (
        <p data-testid="s16-approve-unknown" role="alert">
          批准结果未知：请使用同一操作标识重试，服务端保证幂等。
        </p>
      )}
      <div className="demo-controls">
        <label htmlFor="s16-approver-token">审批人凭据（仅本次动作，不持久化）</label>
        <input
          id="s16-approver-token"
          type="password"
          data-testid="s16-approver-token"
          value={approverToken}
          onChange={(event) => setApproverToken(event.target.value)}
          disabled={approve.isPending || approved >= 2}
          autoComplete="off"
        />
        <button
          type="button"
          data-testid="s16-approve-button"
          disabled={approverToken === "" || approve.isPending || approved >= 2}
          onClick={handleApprove}
        >
          {approve.isPending ? "提交中…" : `以第 ${approverIndex} 名审批人批准`}
        </button>
      </div>
    </section>
  );
}

function CommitSection({
  requestId,
  earlyDeletion,
  approvedCount,
  onIdentityDenied,
}: {
  requestId: string;
  earlyDeletion: boolean;
  approvedCount: number;
  onIdentityDenied: () => void;
}) {
  const [confirmed, setConfirmed] = useState(false);
  const [unknown, setUnknown] = useState(false);
  const commit = useS16Commit();
  const cancel = useS16Cancel();
  const ready = !earlyDeletion || approvedCount >= 2;

  const handleCommit = () => {
    if (!confirmed || !ready) return;
    commit.mutate(
      { requestId, idempotencyKey: `s16-commit-${requestId}` },
      {
        onError: (error) => {
          if (error instanceof HttpError && error.status === 403) {
            onIdentityDenied();
            return;
          }
          if (!isDefinitiveS16Rejection(error)) setUnknown(true);
        },
      },
    );
  };

  return (
    <section className="panel" data-testid="s16-commit" aria-labelledby="s16-commit-title">
      <h3 id="s16-commit-title">提交删除（唯一不可逆边界）</h3>
      {!ready && (
        <p data-testid="s16-commit-blocked" role="alert">
          提前删除需两名审批人批准后才能提交。
        </p>
      )}
      {commit.error !== null && <S16ErrorState error={commit.error} />}
      {unknown && (
        <p data-testid="s16-commit-unknown" role="alert">
          提交结果未知：页面不会重复提交，请查询权威任务状态。
        </p>
      )}
      <div className="demo-controls">
        <label>
          <input
            type="checkbox"
            data-testid="s16-commit-confirm"
            checked={confirmed}
            onChange={(event) => setConfirmed(event.target.checked)}
          />
          我确认已核对清单、保留、法律保全与审批链
        </label>
        <button
          type="button"
          data-testid="s16-commit-button"
          disabled={!confirmed || !ready || commit.isPending || commit.isSuccess}
          onClick={handleCommit}
        >
          {commit.isPending ? "提交中…" : "提交删除"}
        </button>
        <button
          type="button"
          data-testid="s16-cancel-button"
          disabled={cancel.isPending || commit.isSuccess}
          onClick={() =>
            cancel.mutate(
              { requestId, idempotencyKey: `s16-cancel-${requestId}` },
              {
                onError: (error) => {
                  if (error instanceof HttpError && error.status === 403) {
                    onIdentityDenied();
                    return;
                  }
                  if (!isDefinitiveS16Rejection(error)) setUnknown(true);
                },
              },
            )
          }
        >
          {cancel.isPending ? "取消中…" : "取消请求（提交前）"}
        </button>
      </div>
      {cancel.error !== null && <S16ErrorState error={cancel.error} />}
    </section>
  );
}

function JobSection({
  query,
  onIdentityDenied,
}: {
  query: S16QueryResponse;
  onIdentityDenied: () => void;
}) {
  const process = useS16Process();
  const repair = useS16Repair();
  const [repairFact, setRepairFact] = useState("");
  const job = query.job;
  if (job == null) {
    return (
      <p role="status" aria-live="polite" data-testid="s16-job-none">
        尚未创建删除任务
      </p>
    );
  }

  return (
    <section className="panel" data-testid="s16-job" aria-labelledby="s16-job-title">
      <h3 id="s16-job-title">持久删除任务</h3>
      <dl className="facts">
        <div>
          <dt>状态</dt>
          <dd data-testid="s16-job-status">{job.status}</dd>
        </div>
        <div>
          <dt>尝试次数</dt>
          <dd data-testid="s16-job-attempt">{job.attempt}</dd>
        </div>
        <div>
          <dt>fence</dt>
          <dd data-testid="s16-job-fence">{job.fence}</dd>
        </div>
        {job.stable_failure != null && (
          <div>
            <dt>稳定失败</dt>
            <dd data-testid="s16-stable-failure">
              {job.stable_failure.owner_id} · {job.stable_failure.reason_code} ·
              责任方 {job.stable_failure.responsible_party} · 恢复动作{" "}
              {job.stable_failure.recovery_action}
            </dd>
          </div>
        )}
      </dl>
      {job.status === "repair_required" && (
        <div className="demo-controls">
          <label htmlFor="s16-repair-fact">恢复证明（由运维方提供）</label>
          <input
            id="s16-repair-fact"
            data-testid="s16-repair-fact"
            value={repairFact}
            onChange={(event) => setRepairFact(event.target.value)}
            disabled={repair.isPending}
          />
          <button
            type="button"
            data-testid="s16-repair-button"
            disabled={repairFact === "" || repair.isPending}
            onClick={() =>
              repair.mutate(
                {
                  requestId: query.request_id,
                  ownerId: job.stable_failure?.owner_id ?? "s02",
                  repairFact,
                  idempotencyKey: `s16-repair-${query.request_id}`,
                },
                {
                  onError: (error) => {
                    if (error instanceof HttpError && error.status === 403) {
                      onIdentityDenied();
                      return;
                    }
                    if (!isDefinitiveS16Rejection(error)) {
                      void process; // keep the authoritative state observable
                    }
                  },
                },
              )
            }
          >
            {repair.isPending ? "提交中…" : "修复并继续原任务"}
          </button>
          {repair.error !== null && <S16ErrorState error={repair.error} />}
        </div>
      )}
      <div className="demo-controls">
        <button
          type="button"
          data-testid="s16-process-button"
          disabled={process.isPending || job.status === "complete"}
          onClick={() =>
            process.mutate(undefined, {
              onError: (error) => {
                if (error instanceof HttpError && error.status === 403) {
                  onIdentityDenied();
                }
              },
            })
          }
        >
          {process.isPending ? "执行中…" : "执行一次删除尝试（受控）"}
        </button>
      </div>
      {process.error !== null && <S16ErrorState error={process.error} />}
    </section>
  );
}

function ReceiptSection({
  requestId,
  onIdentityDenied,
}: {
  requestId: string;
  onIdentityDenied: () => void;
}) {
  const receipt = useS16Receipt(requestId);
  if (receipt.isPending) {
    return (
      <p role="status" aria-live="polite" data-testid="s16-receipt-loading">
        读取凭证…
      </p>
    );
  }
  if (receipt.error !== null) {
    if (
      receipt.error instanceof HttpError &&
      receipt.error.status === 403
    ) {
      onIdentityDenied();
    }
    return <S16ErrorState error={receipt.error} />;
  }
  const data = receipt.data;
  if (data === undefined) return null;
  return (
    <section className="panel" data-testid="s16-receipt" aria-labelledby="s16-receipt-title">
      <h3 id="s16-receipt-title">无原值删除凭证</h3>
      <dl className="facts">
        <div>
          <dt>凭证标识</dt>
          <dd data-testid="s16-receipt-id">{data.receipt_id}</dd>
        </div>
        <div>
          <dt>结果</dt>
          <dd data-testid="s16-receipt-result">{data.result}</dd>
        </div>
        <div>
          <dt>授权方</dt>
          <dd>{data.authority}</dd>
        </div>
        <div>
          <dt>恢复重放</dt>
          <dd data-testid="s16-receipt-replay">{data.restore_replay_status}</dd>
        </div>
        <div>
          <dt>各 owner 删除计数</dt>
          <dd data-testid="s16-receipt-owner-counts">
            {Object.entries(data.owner_counts)
              .map(([owner, count]) => `${owner}:${count}`)
              .join(" · ")}
          </dd>
        </div>
      </dl>
      <p className="text-sm text-muted-foreground">
        凭证仅含指纹与计数；完成后所有受限查询返回同一无值结果。
      </p>
    </section>
  );
}

/**
 * The S16 governed-deletion surface mounted only for ``/controlled/s16``
 * (and its alias).  Sequence: submit an application reference -> read the
 * nine-class dry-run manifest -> (early deletion) two approvers -> explicit
 * commit -> bounded worker attempts -> repair when required -> value-free
 * receipt.  No raw value, object reference or internal path is ever
 * rendered; every request carries its own idempotency key and a lost
 * response keeps the key for exact replay.
 */
export default function S16GovernedDeletionPanel() {
  const [reference, setReference] = useState("");
  const [requestId, setRequestId] = useState<string | null>(null);
  const [preflight, setPreflight] = useState<S16PreflightResponse | null>(null);
  const [preflightUnknown, setPreflightUnknown] = useState(false);
  const [cancelled, setCancelled] = useState(false);
  const [approvedCount, setApprovedCount] = useState(0);
  const [receiptEpoch, setReceiptEpoch] = useState(0);
  const queryClient = useQueryClient();
  const preflightMutation = useS16Preflight();
  const query = useS16Query(requestId);

  // After a completed deletion every application-scoped cache is dropped
  // and the S16 plane is refetched from the authority.
  const cacheClearedRef = useRef(false);
  const cacheKeyRef = useRef<string | null>(null);
  useEffect(() => {
    if (cacheKeyRef.current !== requestId) {
      cacheKeyRef.current = requestId;
      cacheClearedRef.current = false;
    }
    if (
      requestId !== null &&
      query.data?.job?.status === "complete" &&
      !cacheClearedRef.current
    ) {
      cacheClearedRef.current = true;
      clearApplicationScopedCache(queryClient);
      // R3 (P1-7): the S16 caches were removed; the request query is
      // deterministically re-populated from the authority, and the
      // receipt observer is re-mounted so the value-free receipt
      // refetches instead of lingering in a removed-query limbo.
      void queryClient.fetchQuery({
        queryKey: S16_REQUEST_KEY(requestId),
        queryFn: s16RequestQueryFn(requestId),
      });
      setReceiptEpoch((current) => current + 1);
    }
  }, [query.data?.job?.status, queryClient, requestId]);

  // Identity invalidation (R2 P1-14): a governance-surface 403 clears the
  // local S16 state and every S16/application-scoped cache so no previous
  // identity's manifest, reference or approval survives; the panel shows
  // only the authorization error.  Approver-surface 403s (the approval
  // section's own hook) leave the governance state intact.
  const [identityDenied, setIdentityDenied] = useState(false);
  const governance403 =
    identityDenied ||
    (query.error instanceof HttpError && query.error.status === 403) ||
    (preflightMutation.error instanceof HttpError &&
      preflightMutation.error.status === 403);
  useEffect(() => {
    if (!governance403) return;
    setIdentityDenied(true);
    setReference("");
    setPreflight(null);
    setRequestId(null);
    setCancelled(false);
    setApprovedCount(0);
    setPreflightUnknown(false);
    queryClient.removeQueries({ queryKey: ["s16", "deletions"] });
    queryClient.removeQueries({ queryKey: ["s16", "legal-holds"] });
    clearApplicationScopedCache(queryClient);
  }, [governance403, queryClient]);

  useEffect(() => {
    if (preflight !== null) return;
    const data = query.data;
    if (data === undefined) return;
    if (data.cancelled) setCancelled(true);
  }, [preflight, query.data]);

  const handlePreflight = () => {
    if (reference.trim() === "" || preflightMutation.isPending) return;
    preflightMutation.mutate(
      {
        application_reference: reference.trim(),
        idempotency_key: `s16-preflight-${reference.trim()}`,
      },
      {
        onSuccess: (result) => {
          setPreflight(result);
          setRequestId(result.request_id);
          setPreflightUnknown(false);
        },
        onError: (error) => {
          if (!isDefinitiveS16Rejection(error)) setPreflightUnknown(true);
        },
      },
    );
  };

  // R2 (P1-14): an invalidated governance identity renders ONLY the
  // authorization error — no reference, manifest, hold, approval or commit
  // surface survives.
  if (governance403) {
    return (
      <section className="panel" data-testid="s16-governed-deletion" aria-labelledby="s16-title">
        <h2 id="s16-title">合规删除（服务端权威）</h2>
        {query.error instanceof HttpError ? (
          <S16ErrorState error={query.error} />
        ) : preflightMutation.error instanceof HttpError ? (
          <S16ErrorState error={preflightMutation.error} />
        ) : (
          <S16ErrorState error={new HttpError(403, { error: "S16_FORBIDDEN" })} />
        )}
      </section>
    );
  }

  // R2 (P1-13): after completion the page renders ONLY the value-free
  // receipt and its summary — every preflight, hold, approval, commit and
  // repair surface is unloaded.
  const jobComplete = query.data?.job?.status === "complete";
  if (jobComplete && requestId !== null && query.data !== undefined) {
    return (
      <section className="panel" data-testid="s16-governed-deletion" aria-labelledby="s16-title">
        <h2 id="s16-title">合规删除（服务端权威）</h2>
        <p data-testid="s16-complete-only" role="status">
          删除已完成：仅保留无原值凭证。
        </p>
        {/* R3 (P1-7): the terminal job surface stays mounted with the
            final "complete" status beside the value-free receipt. */}
        <JobSection
          query={query.data}
          onIdentityDenied={() => setIdentityDenied(true)}
        />
        <ReceiptSection
          key={receiptEpoch}
          requestId={requestId}
          onIdentityDenied={() => setIdentityDenied(true)}
        />
      </section>
    );
  }

  return (
    <section className="panel" data-testid="s16-governed-deletion" aria-labelledby="s16-title">
      <h2 id="s16-title">合规删除（服务端权威）</h2>
      <div className="demo-controls">
        <label htmlFor="s16-reference">申请引用（按当前授权范围）</label>
        <input
          id="s16-reference"
          data-testid="s16-reference"
          value={reference}
          onChange={(event) => setReference(event.target.value)}
          disabled={preflightMutation.isPending || requestId !== null}
          autoComplete="off"
        />
        <button
          type="button"
          data-testid="s16-preflight-button"
          disabled={
            reference.trim() === "" ||
            preflightMutation.isPending ||
            requestId !== null
          }
          onClick={handlePreflight}
        >
          {preflightMutation.isPending ? "执行中…" : "生成 dry-run 清单"}
        </button>
      </div>
      {preflightMutation.error !== null && (
        <S16ErrorState error={preflightMutation.error} />
      )}
      {preflightUnknown && (
        <p data-testid="s16-preflight-unknown" role="alert">
          dry-run 结果未知：请保留同一引用与操作标识重试。
        </p>
      )}

      {preflight !== null && <ManifestTable preflight={preflight} />}

      {preflight !== null && (
        <LegalHoldSection
          scopeFingerprint={preflight.scope_fingerprint}
          holds={query.data?.legal_holds ?? []}
        />
      )}

      {preflight !== null && cancelled && (
        <p data-testid="s16-cancelled" role="status">
          该请求已取消，所有副本保持原样。
        </p>
      )}

      {preflight !== null &&
        !cancelled &&
        preflight.early_deletion && (
          <ApprovalSection
            preflight={preflight}
            approved={approvedCount}
            onApproved={() => setApprovedCount((current) => current + 1)}
          />
        )}

      {preflight !== null && !cancelled && (
        <CommitSection
          requestId={preflight.request_id}
          earlyDeletion={preflight.early_deletion}
          approvedCount={approvedCount}
          onIdentityDenied={() => setIdentityDenied(true)}
        />
      )}

      {query.data !== undefined && query.data.job !== null && (
        <JobSection
          query={query.data}
          onIdentityDenied={() => setIdentityDenied(true)}
        />
      )}
      {query.error !== null && <S16ErrorState error={query.error} />}
    </section>
  );
}
