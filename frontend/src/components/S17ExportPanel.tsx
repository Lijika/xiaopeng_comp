import { useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { HttpError } from "../api/client";
import {
  useS17Access,
  useS17Approve,
  useS17Commit,
  useS17Confirm,
  useS17Expire,
  useS17Preview,
  useS17Process,
  useS17Query,
  useS17Receipt,
  useS17Revoke,
} from "../api/hooks";

type Draft = {
  purpose: string;
  recipient_id: string;
  fields: string;
  artifacts: string;
  classification: string;
  expiry: string;
  scope_reference: string;
};

const initialDraft: Draft = {
  purpose: "regulatory_review",
  recipient_id: "s17-recipient-1",
  fields: "application_fingerprint, lifecycle_phase",
  artifacts: "route_metadata",
  classification: "confidential",
  expiry: "3600",
  scope_reference: "APP-REF-1",
};

type OperationName = "preview" | "approve" | "commit" | "process" | "access" | "confirm" | "expire" | "revoke";

function idempotencyKey(prefix: string, requestId = "") {
  return `${prefix}-${requestId}-${Date.now()}`;
}

function isDefinitiveS17Rejection(error: unknown): boolean {
  if (!(error instanceof HttpError)) return false;
  if ([400, 401, 403, 404, 409, 422].includes(error.status)) return true;
  return error.status === 503 && error.errorCode === "S17_UNAVAILABLE";
}

function parseList(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function ErrorState({ error, action, unknown }: { error: Error; action?: string; unknown?: boolean }) {
  const code = error instanceof HttpError ? error.errorCode ?? `S17_HTTP_${error.status}` : "S17_HTTP_UNKNOWN";
  const reason = error instanceof HttpError ? error.reasonCode : undefined;
  return (
    <div className="demo-error" data-testid="s17-error" role="alert">
      <p>{action ?? "导出操作失败"}</p>
      <p data-testid="s17-error-code">{code}</p>
      {reason ? <p data-testid="s17-error-reason">{reason}</p> : null}
      {unknown ? <p data-testid="s17-unknown">结果未知，请查询权威状态后再决定是否重试。</p> : null}
    </div>
  );
}

function StatusFacts({ status, query }: { status: string; query: ReturnType<typeof useS17Query> }) {
  const data = query.data;
  return (
    <section className="panel" data-testid="s17-status" aria-labelledby="s17-status-title">
      <h3 id="s17-status-title">权威状态</h3>
      <dl className="facts">
        <div><dt>请求状态</dt><dd data-testid="s17-request-status">{status}</dd></div>
        <div><dt>投递状态</dt><dd data-testid="s17-delivery-status">{data?.delivery_status ?? "pending"}</dd></div>
        <div><dt>有效期</dt><dd data-testid="s17-expiry-status">{data?.expiry ?? "—"}</dd></div>
        <div><dt>水印登记</dt><dd data-testid="s17-watermark-status">{data?.watermark_id ? "已登记" : "待生成"}</dd></div>
        <div><dt>加密登记</dt><dd data-testid="s17-encryption-status">{data?.package_digest ? "已登记" : "待生成"}</dd></div>
        <div><dt>生成尝试</dt><dd data-testid="s17-attempt">{data?.attempt ?? 0}</dd></div>
      </dl>
    </section>
  );
}

export default function S17ExportPanel() {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<Draft>(initialDraft);
  const [requestId, setRequestId] = useState<string | null>(() => {
    if (typeof window === "undefined") return null;
    return new URLSearchParams(window.location.search).get("request");
  });
  const [previewDigest, setPreviewDigest] = useState("");
  const [frozenDraft, setFrozenDraft] = useState<Draft | null>(null);
  const [approverToken, setApproverToken] = useState("");
  const [workerToken, setWorkerToken] = useState("");
  const [deliveryToken, setDeliveryToken] = useState("");
  const [recipientCredential, setRecipientCredential] = useState("");
  const [approved, setApproved] = useState(false);
  const [showReceipt, setShowReceipt] = useState(false);
  const [lastStatus, setLastStatus] = useState("draft");
  const [commitConfirmed, setCommitConfirmed] = useState(false);
  const [generationResult, setGenerationResult] = useState<{ status: string; reasonCode?: string }>({ status: "idle" });
  const [unknownActions, setUnknownActions] = useState<Set<OperationName>>(new Set());
  const [identityDenied, setIdentityDenied] = useState(false);
  const operationKeys = useRef(new Map<string, string>());

  const preview = useS17Preview();
  const approve = useS17Approve();
  const commit = useS17Commit();
  const process = useS17Process();
  const access = useS17Access();
  const confirm = useS17Confirm();
  const expire = useS17Expire();
  const revoke = useS17Revoke();
  const query = useS17Query(requestId);
  const receipt = useS17Receipt(requestId, showReceipt);
  const status = query.data?.status ?? lastStatus;
  const effectiveApproved = approved || ["approved", "queued", "delivered", "accessed", "confirmed"].includes(status);
  const requestReady = useMemo(
    () => draft.purpose.trim() !== "" && draft.recipient_id.trim() !== "" && draft.scope_reference.trim() !== "" && (parseList(draft.fields).length > 0 || parseList(draft.artifacts).length > 0),
    [draft],
  );
  const frozen: Draft = query.data
    ? {
        purpose: query.data.purpose ?? "",
        recipient_id: query.data.recipient_id ?? "",
        fields: (query.data.fields ?? []).join(", "),
        artifacts: (query.data.artifacts ?? []).join(", "),
        classification: query.data.classification ?? "",
        expiry: query.data.expiry === null || query.data.expiry === undefined ? "" : String(query.data.expiry),
        scope_reference: frozenDraft?.scope_reference ?? "已冻结",
      }
    : frozenDraft ?? draft;
  const disabled = requestId !== null;

  const getOperationKey = (operation: OperationName, id = requestId ?? "") => {
    const key = `${operation}:${id}`;
    const existing = operationKeys.current.get(key);
    if (existing) return existing;
    const created = idempotencyKey(`s17-${operation}`, id);
    operationKeys.current.set(key, created);
    return created;
  };
  const clearOperationKey = (operation: OperationName, id = requestId ?? "") => {
    operationKeys.current.delete(`${operation}:${id}`);
  };
  const handleOperationError = (operation: OperationName, error: Error, clearCredential?: () => void, keyId = requestId ?? "") => {
    clearCredential?.();
    if (error instanceof HttpError && error.status === 403) {
      setApproverToken("");
      setWorkerToken("");
      setDeliveryToken("");
      setRecipientCredential("");
      return;
    }
    if (isDefinitiveS17Rejection(error)) {
      clearOperationKey(operation, keyId);
      setUnknownActions((current) => {
        const next = new Set(current);
        next.delete(operation);
        return next;
      });
      return;
    }
    setUnknownActions((current) => new Set(current).add(operation));
  };

  useEffect(() => {
    if (query.error instanceof HttpError && query.error.status === 403) {
      queryClient.removeQueries({ queryKey: ["s17"] });
      setApproverToken("");
      setWorkerToken("");
      setDeliveryToken("");
      setRecipientCredential("");
      setIdentityDenied(true);
      return;
    }
    if (!query.data) return;
    if (!previewDigest && query.data.preview_digest) setPreviewDigest(query.data.preview_digest);
    if (!approved && ["approved", "queued", "delivered", "accessed", "confirmed"].includes(query.data.status)) setApproved(true);
  }, [approved, previewDigest, query.data, query.error, queryClient]);

  const updateDraft = (key: keyof Draft, value: string) => {
    clearOperationKey("preview", "draft");
    setUnknownActions((current) => {
      const next = new Set(current);
      next.delete("preview");
      return next;
    });
    setDraft((current) => ({ ...current, [key]: value }));
  };
  const handlePreview = () => {
    if (!requestReady) return;
    const expiry = Math.floor(Date.now() / 1000) + Math.max(1, Number.parseInt(draft.expiry, 10) || 1);
    preview.mutate(
      {
        purpose: draft.purpose.trim(),
        recipient_id: draft.recipient_id.trim(),
        fields: parseList(draft.fields),
        artifacts: parseList(draft.artifacts),
        classification: draft.classification.trim(),
        expiry,
        scope_reference: draft.scope_reference.trim(),
        idempotency_key: getOperationKey("preview", "draft"),
      },
      {
        onSuccess: (result) => {
          if (!result.request_id) return;
          setRequestId(result.request_id);
          setPreviewDigest(result.preview_digest);
          setFrozenDraft({ ...draft });
          setLastStatus(result.status);
          clearOperationKey("preview", "draft");
          setUnknownActions((current) => { const next = new Set(current); next.delete("preview"); return next; });
          if (typeof window !== "undefined") {
            const params = new URLSearchParams(window.location.search);
            params.set("request", result.request_id);
            window.history.replaceState(null, "", `?${params.toString()}`);
          }
        },
        onError: (error) => handleOperationError("preview", error, undefined, "draft"),
      },
    );
  };

  const handleApprove = () => {
    if (!requestId || !approverToken || !previewDigest) return;
    approve.mutate(
      { requestId, preview_digest: previewDigest, idempotency_key: getOperationKey("approve"), approverToken },
      { onSuccess: (result) => { setApproved(result.status === "approved" || result.status === "replayed"); setLastStatus(result.status); setApproverToken(""); approve.reset(); clearOperationKey("approve"); setUnknownActions((current) => { const next = new Set(current); next.delete("approve"); return next; }); }, onError: (error) => handleOperationError("approve", error, () => setApproverToken("")) },
    );
  };
  const handleCommit = () => {
    if (!requestId || !effectiveApproved || !commitConfirmed) return;
    commit.mutate(
      { requestId, idempotency_key: getOperationKey("commit" ) },
      { onSuccess: (result) => { setLastStatus(result.status); clearOperationKey("commit"); setUnknownActions((current) => { const next = new Set(current); next.delete("commit"); return next; }); }, onError: (error) => handleOperationError("commit", error) },
    );
  };
  const handleProcess = () => {
    if (!workerToken) return;
    process.mutate(
      { workerToken },
      { onSuccess: (result) => { setLastStatus(result.status); setGenerationResult({ status: result.status, reasonCode: result.reason_code ?? undefined }); setWorkerToken(""); process.reset(); setUnknownActions((current) => { const next = new Set(current); next.delete("process"); return next; }); }, onError: (error) => handleOperationError("process", error, () => setWorkerToken("")) },
    );
  };
  const handleAccess = () => {
    if (!requestId || !deliveryToken) return;
    access.mutate(
      { requestId, token: deliveryToken, recipientToken: recipientCredential },
      { onSuccess: (result) => { setLastStatus(result.status); setDeliveryToken(""); setRecipientCredential(""); access.reset(); setUnknownActions((current) => { const next = new Set(current); next.delete("access"); return next; }); }, onError: (error) => handleOperationError("access", error, () => { setDeliveryToken(""); setRecipientCredential(""); }) },
    );
  };
  const handleConfirm = () => {
    if (!requestId) return;
    confirm.mutate({ requestId, idempotency_key: getOperationKey("confirm"), recipientToken: recipientCredential }, { onSuccess: (result) => { setLastStatus(result.status); setRecipientCredential(""); clearOperationKey("confirm"); setUnknownActions((current) => { const next = new Set(current); next.delete("confirm"); return next; }); }, onError: (error) => handleOperationError("confirm", error, () => setRecipientCredential("")) });
  };
  const handleExpire = () => {
    if (!requestId || !workerToken) return;
    expire.mutate({ requestId, idempotency_key: getOperationKey("expire"), workerToken }, { onSuccess: (result) => { setLastStatus(result.status); setWorkerToken(""); expire.reset(); clearOperationKey("expire"); setUnknownActions((current) => { const next = new Set(current); next.delete("expire"); return next; }); }, onError: (error) => handleOperationError("expire", error, () => setWorkerToken("")) });
  };
  const handleRevoke = () => {
    if (!requestId) return;
    revoke.mutate({ requestId, idempotency_key: getOperationKey("revoke") }, { onSuccess: (result) => { setLastStatus(result.status); clearOperationKey("revoke"); setUnknownActions((current) => { const next = new Set(current); next.delete("revoke"); return next; }); }, onError: (error) => handleOperationError("revoke", error) });
  };

  if (identityDenied) {
    return <section className="panel" data-testid="s17-export-panel"><h2>受控导出</h2><ErrorState error={query.error ?? new HttpError(403, { error: "S17_FORBIDDEN" })} action="当前身份无权访问受控导出" /></section>;
  }

  return (
    <section className="panel" data-testid="s17-export-panel">
      <h2>受控导出</h2>
      <p className="demo-status" data-testid="s17-privacy-note">请求只保留摘要；凭据、原始值、包内容与结果地址不会进入页面状态。</p>
      <section className="panel" data-testid="s17-request-form" aria-labelledby="s17-request-title">
        <h3 id="s17-request-title">固定导出请求</h3>
        <div className="s17-form-grid">
          <label>业务目的<input data-testid="s17-purpose" value={draft.purpose} onChange={(event) => updateDraft("purpose", event.target.value)} disabled={disabled} /></label>
          <label>接收方标识<input data-testid="s17-recipient" value={draft.recipient_id} onChange={(event) => updateDraft("recipient_id", event.target.value)} disabled={disabled} /></label>
          <label>最小字段（逗号分隔）<input data-testid="s17-fields" value={draft.fields} onChange={(event) => updateDraft("fields", event.target.value)} disabled={disabled} /></label>
          <label>最小产物（逗号分隔）<input data-testid="s17-artifacts" value={draft.artifacts} onChange={(event) => updateDraft("artifacts", event.target.value)} disabled={disabled} /></label>
          <label>数据分类<input data-testid="s17-classification" value={draft.classification} onChange={(event) => updateDraft("classification", event.target.value)} disabled={disabled} /></label>
          <label>有效期（秒）<input data-testid="s17-expiry" type="number" min="1" value={draft.expiry} onChange={(event) => updateDraft("expiry", event.target.value)} disabled={disabled} /></label>
          <label>范围引用<input data-testid="s17-scope" value={draft.scope_reference} onChange={(event) => updateDraft("scope_reference", event.target.value)} disabled={disabled} /></label>
        </div>
        <button type="button" data-testid="s17-preview-button" onClick={handlePreview} disabled={!requestReady || preview.isPending || disabled}>{preview.isPending ? "提交中…" : "预览并冻结请求"}</button>
        {preview.error ? <ErrorState error={preview.error} action="预览未被接受" unknown={unknownActions.has("preview")} /> : null}
      </section>

      {requestId && query.error ? <ErrorState error={query.error} action="请求状态读取失败" /> : null}
      {requestId && !query.error ? (
        <>
          <section className="panel" data-testid="s17-export-state" aria-labelledby="s17-fixed-title">
            <h3 id="s17-fixed-title">不可变请求</h3>
            <dl className="facts" data-testid="s17-request-summary">
              <div><dt>目的</dt><dd>{frozen.purpose}</dd></div>
              <div><dt>接收方</dt><dd>{frozen.recipient_id}</dd></div>
              <div><dt>字段</dt><dd>{frozen.fields}</dd></div>
              <div><dt>产物</dt><dd>{frozen.artifacts}</dd></div>
              <div><dt>分类</dt><dd>{frozen.classification}</dd></div>
              <div><dt>范围</dt><dd>{frozen.scope_reference}</dd></div>
              <div><dt>有效期</dt><dd>{frozen.expiry} 秒</dd></div>
              <div><dt>请求指纹</dt><dd>{requestId.slice(-12)}</dd></div>
              <div><dt>预览摘要</dt><dd>{previewDigest.slice(0, 12)}</dd></div>
            </dl>
            <button type="button" data-testid="s17-deny-button" onClick={handleRevoke} disabled={revoke.isPending || status === "revoked" || status === "confirmed"}>撤销请求（拒绝导出）</button>
            {revoke.error ? <ErrorState error={revoke.error} action="撤销未被接受" unknown={unknownActions.has("revoke")} /> : null}
          </section>

          <StatusFacts status={status} query={query} />

          <section className="panel" data-testid="s17-approval" aria-labelledby="s17-approval-title">
            <h3 id="s17-approval-title">独立审批</h3>
            <p data-testid="s17-approval-status">{status === "revoked" ? "已拒绝" : approved ? "已批准" : "等待独立审批"}</p>
            <label>审批人凭据（仅本次动作）<input data-testid="s17-approver-token" type="password" autoComplete="off" value={approverToken} onChange={(event) => setApproverToken(event.target.value)} disabled={approved || approve.isPending} /></label>
            <button type="button" data-testid="s17-approve-button" onClick={handleApprove} disabled={!approverToken || approved || approve.isPending || status === "revoked"}>{approve.isPending ? "提交中…" : "独立批准固定请求"}</button>
            {approve.error ? <ErrorState error={approve.error} action="审批未被接受" unknown={unknownActions.has("approve")} /> : null}
          </section>

          <section className="panel" data-testid="s17-generation" aria-labelledby="s17-generation-title">
            <h3 id="s17-generation-title">服务端生成与交付</h3>
            <label><input type="checkbox" data-testid="s17-commit-confirm" checked={commitConfirmed} onChange={(event) => setCommitConfirmed(event.target.checked)} />我确认提交当前固定请求</label>
            <button type="button" data-testid="s17-commit-button" onClick={handleCommit} disabled={!effectiveApproved || !commitConfirmed || commit.isPending || status === "revoked"}>{commit.isPending ? "提交中…" : "提交生成"}</button>
            <div className="demo-controls">
              <label>生成工作器凭据（仅本次动作）<input data-testid="s17-worker-token" type="password" autoComplete="off" value={workerToken} onChange={(event) => setWorkerToken(event.target.value)} /></label>
              <button type="button" data-testid="s17-process-button" onClick={handleProcess} disabled={!workerToken || !effectiveApproved || process.isPending}>{process.isPending ? "生成中…" : "执行一次生成"}</button>
              <button type="button" data-testid="s17-expire-button" onClick={handleExpire} disabled={!workerToken || expire.isPending || status === "confirmed"}>{expire.isPending ? "处理中…" : "执行过期清理"}</button>
            </div>
            {commit.error ? <ErrorState error={commit.error} action="生成请求未被接受" unknown={unknownActions.has("commit")} /> : null}
            {process.error ? <ErrorState error={process.error} action="生成失败，临时产物已由服务端清理" unknown={unknownActions.has("process")} /> : null}
            {expire.error ? <ErrorState error={expire.error} action="过期处理未被接受" unknown={unknownActions.has("expire")} /> : null}
            {generationResult.reasonCode ? <p data-testid="s17-generation-failure" role="status">{generationResult.reasonCode} · 服务端已清理临时产物</p> : null}
          </section>

          <section className="panel" data-testid="s17-delivery" aria-labelledby="s17-delivery-title">
            <h3 id="s17-delivery-title">一次性访问</h3>
            <label>接收方会话凭据（仅本次动作）<input data-testid="s17-recipient-credential" type="password" autoComplete="off" value={recipientCredential} onChange={(event) => setRecipientCredential(event.target.value)} disabled={status === "expired" || status === "revoked" || status === "confirmed"} /></label>
            <label>一次性投递凭据（仅本次动作）<input data-testid="s17-delivery-token" type="password" autoComplete="off" value={deliveryToken} onChange={(event) => setDeliveryToken(event.target.value)} disabled={status === "expired" || status === "revoked" || status === "confirmed"} /></label>
            <button type="button" data-testid="s17-access-button" onClick={handleAccess} disabled={!deliveryToken || access.isPending || status === "expired" || status === "revoked"}>{access.isPending ? "校验中…" : "访问一次性结果"}</button>
            <button type="button" data-testid="s17-confirm-button" onClick={handleConfirm} disabled={status !== "accessed" || confirm.isPending}>{confirm.isPending ? "确认中…" : "确认已接收"}</button>
            {access.error ? <ErrorState error={access.error} action="访问未被接受" unknown={unknownActions.has("access")} /> : null}
            {confirm.error ? <ErrorState error={confirm.error} action="确认未被接受" unknown={unknownActions.has("confirm")} /> : null}
          </section>

          <section className="panel" data-testid="s17-receipt-section" aria-labelledby="s17-receipt-title">
            <h3 id="s17-receipt-title">审计回执</h3>
            <button type="button" data-testid="s17-receipt-button" onClick={() => setShowReceipt(true)} disabled={receipt.isFetching}>查看无原值回执</button>
            {receipt.error ? <ErrorState error={receipt.error} action="回执读取失败" /> : null}
            {receipt.data ? <dl className="facts" data-testid="s17-export-receipt">
              <div><dt>回执标识</dt><dd data-testid="s17-receipt-id">{receipt.data.receipt_id.slice(-12)}</dd></div>
              <div><dt>状态</dt><dd data-testid="s17-receipt-status">{receipt.data.status}</dd></div>
              <div><dt>投递</dt><dd data-testid="s17-receipt-delivery">{receipt.data.delivery_status ?? "—"}</dd></div>
              <div><dt>清理</dt><dd data-testid="s17-receipt-cleanup">{receipt.data.cleanup_result ?? "—"}</dd></div>
              <div><dt>包摘要</dt><dd data-testid="s17-receipt-digest">{receipt.data.package_digest?.slice(0, 12) ?? "—"}</dd></div>
            </dl> : null}
          </section>
        </>
      ) : null}
    </section>
  );
}
