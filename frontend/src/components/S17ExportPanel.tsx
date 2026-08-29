import { useState } from "react";
import { useS17Approve, useS17Commit, useS17Preview, useS17Query, useS17Receipt } from "../api/hooks";

/** Minimal operator surface. It renders digests and state only; package bytes,
 * credentials and locators remain behind the registered delivery seam. */
export default function S17ExportPanel() {
  const [requestId, setRequestId] = useState<string | null>(null);
  const [previewDigest, setPreviewDigest] = useState("");
  const [scopeReference, setScopeReference] = useState("APP-REF-1");
  const preview = useS17Preview();
  const approve = useS17Approve();
  const commit = useS17Commit();
  const query = useS17Query(requestId);
  const receipt = useS17Receipt(requestId);
  const submitPreview = () => preview.mutate({ purpose: "regulatory_review", fields: ["application_fingerprint", "lifecycle_phase"], artifacts: ["route_metadata"], recipient_id: "s17-recipient-1", classification: "confidential", expiry: Math.floor(Date.now() / 1000) + 3600, scope_reference: scopeReference, idempotency_key: `s17-preview-${Date.now()}` }, { onSuccess: (result) => { if (result.request_id) setRequestId(result.request_id); setPreviewDigest(result.preview_digest); } });
  return <section className="panel" data-testid="s17-export-panel">
    <h2>受控导出</h2>
    <label>范围引用 <input value={scopeReference} onChange={(event) => setScopeReference(event.target.value)} /></label>
    <button type="button" onClick={submitPreview} disabled={preview.isPending}>预览导出范围</button>
    {requestId && <div data-testid="s17-export-state">
      <p>请求指纹 {requestId.slice(-12)}</p>
      <p>状态 {query.data?.status ?? preview.data?.status ?? "previewed"}</p>
      <button type="button" onClick={() => approve.mutate({ requestId, preview_digest: previewDigest, idempotency_key: `s17-approve-${requestId}` })} disabled={approve.isPending}>独立批准</button>
      <button type="button" onClick={() => commit.mutate({ requestId, idempotency_key: `s17-commit-${requestId}` })} disabled={commit.isPending}>生成受控包</button>
      <button type="button" onClick={() => void receipt.refetch()}>查看回执</button>
      {receipt.data && <p data-testid="s17-export-receipt">回执 {receipt.data.receipt_id.slice(-12)}</p>}
    </div>}
  </section>;
}
