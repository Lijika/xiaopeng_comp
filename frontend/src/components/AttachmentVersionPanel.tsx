import { useEffect, useRef, useState } from "react";

import {
  HttpError,
  isDefinitiveIntegratorRejection,
  type AttachmentSubmissionResponse,
} from "../api/client";
import {
  useIntegratorSupplementRequest,
  useSubmitAttachmentVersion,
  type AttachmentSubmissionCommand,
} from "../api/hooks";
import { Button } from "./ui/button";

function readParam(name: string): string | null {
  return new URLSearchParams(window.location.search).get(name);
}

function newIdempotencyKey(): string {
  return crypto.randomUUID();
}

/** The exact parsed command held while its transport outcome is unknown, for
 * byte-identical replay with the same semantic idempotency key. */
type IssuedSubmission = {
  command: AttachmentSubmissionCommand;
};

/** The truthful receipt announcement: every disposition maps to a distinct
 * announcement, and a replayed original receipt stays ``accepted`` with the
 * replay made explicit.  The receipt is authoritative server data; the panel
 * never announces acceptance for any other disposition. */
function dispositionAnnouncement(
  receipt: AttachmentSubmissionResponse,
): string {
  const reason =
    receipt.reason_code === null ? "" : `（${receipt.reason_code}）`;
  switch (receipt.disposition) {
    case "accepted":
      return receipt.replayed
        ? "附件版本已接受（重放原回执）"
        : "附件版本已接受";
    case "rejected":
      return `附件版本被拒绝${reason}`;
    case "quarantined":
      return `附件版本被隔离${reason}`;
    case "awaiting_predecessor":
      return `附件版本等待前驱${reason}`;
    default:
      return `附件版本处置：${receipt.disposition}`;
  }
}

/**
 * The Integrator's focused attachment-version panel.  It binds the next
 * ``submit_attachment_version`` command exclusively to the server-derived
 * current request projection: submit stays disabled until a fresh
 * successful projection loads, edits lock while a command is pending or
 * unknown, a known receipt refetches the projection, and a definitive
 * rejection rotates the key and requires an authoritative reload.  The
 * browser parses and transports the registered envelope JSON; it never
 * recreates domain validation.
 */
export default function AttachmentVersionPanel() {
  const requestId = readParam("request");
  const projection = useIntegratorSupplementRequest(requestId);
  const submit = useSubmitAttachmentVersion();

  const [envelopeText, setEnvelopeText] = useState("");
  const [issuedRef, setIssued] = useState<IssuedSubmission | null>(null);
  const [requiresReload, setRequiresReload] = useState(false);
  const [rejectionReason, setRejectionReason] = useState<string | null>(null);
  const [lastReceipt, setLastReceipt] =
    useState<AttachmentSubmissionResponse | null>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    headingRef.current?.focus();
  }, []);

  const parsed: unknown = (() => {
    if (envelopeText.trim() === "") return null;
    try {
      return JSON.parse(envelopeText) as unknown;
    } catch {
      return undefined;
    }
  })();

  const requestEnded =
    projection.data !== undefined &&
    (projection.data.status !== "open" || !projection.data.current);
  const transportUnknown =
    submit.isError && !isDefinitiveIntegratorRejection(submit.error);
  const submitBlocked =
    projection.isPending ||
    projection.isError ||
    projection.data === undefined ||
    requestEnded ||
    issuedRef !== null ||
    requiresReload ||
    submit.isPending ||
    transportUnknown ||
    parsed === null ||
    parsed === undefined;

  const handleSubmit = () => {
    if (submitBlocked || parsed === null || parsed === undefined) return;
    if (projection.data === undefined) return;
    const command: AttachmentSubmissionCommand = {
      idempotency_key: newIdempotencyKey(),
      submission: parsed as Record<string, unknown>,
    };
    setIssued({ command });
    setLastReceipt(null);
    submit.mutate(command, {
      onSuccess: (receipt) => {
        setLastReceipt(receipt);
        setIssued(null);
      },
      onError: (error) => {
        if (!isDefinitiveIntegratorRejection(error)) return;
        setIssued(null);
        setRequiresReload(true);
        setRejectionReason(error.reasonCode ?? error.errorCode ?? "rejected");
      },
    });
  };

  const handleRetry = () => {
    if (issuedRef === null || submit.isPending) return;
    submit.mutate(issuedRef.command, {
      onSuccess: (receipt) => {
        setLastReceipt(receipt);
        setIssued(null);
      },
      onError: (error) => {
        if (!isDefinitiveIntegratorRejection(error)) return;
        setIssued(null);
        setRequiresReload(true);
        setRejectionReason(error.reasonCode ?? error.errorCode ?? "rejected");
      },
    });
  };

  const handleReload = async () => {
    // An unknown outcome must never be reset or given a fresh key; the
    // authoritative reload only clears the rejection fence after the
    // projection refetch succeeds.
    if (submit.isPending) return;
    if (issuedRef !== null) return;
    setRequiresReload(true);
    try {
      await projection.refetch({ throwOnError: true });
    } catch {
      return;
    }
    setRequiresReload(false);
    setRejectionReason(null);
  };

  if (requestId === null) {
    return (
      <section
        className="panel"
        data-testid="integrator-panel"
        aria-labelledby="integrator-title"
      >
        <h2 id="integrator-title" tabIndex={-1} ref={headingRef}>
          附件版本提交
        </h2>
        <p data-testid="integrator-no-request">
          未指定补充材料请求（?request=…）
        </p>
      </section>
    );
  }

  if (projection.isPending) {
    return (
      <section
        className="panel"
        data-testid="integrator-panel"
        aria-labelledby="integrator-title"
      >
        <h2 id="integrator-title" tabIndex={-1} ref={headingRef}>
          附件版本提交
        </h2>
        <p data-testid="integrator-projection-loading">请求投影加载中…</p>
      </section>
    );
  }

  if (projection.isError || projection.data === undefined) {
    const notFound =
      projection.error instanceof HttpError && projection.error.status === 404;
    return (
      <section
        className="panel"
        data-testid="integrator-panel"
        aria-labelledby="integrator-title"
      >
        <h2 id="integrator-title" tabIndex={-1} ref={headingRef}>
          附件版本提交
        </h2>
        <p data-testid="integrator-projection-error">
          {notFound ? "请求未找到或无权访问" : "请求投影不可用"}
        </p>
      </section>
    );
  }

  const data = projection.data;
  return (
    <section
      className="panel"
      data-testid="integrator-panel"
      aria-labelledby="integrator-title"
    >
      <h2 id="integrator-title" tabIndex={-1} ref={headingRef}>
        附件版本提交
      </h2>
      <section
        className="panel"
        data-testid="integrator-projection"
        aria-labelledby="integrator-projection-title"
      >
        <h3 id="integrator-projection-title">请求投影（服务端权威）</h3>
        <dl className="facts">
          <div>
            <dt>请求编号</dt>
            <dd data-testid="integrator-projection-request-id">
              {data.request_id}
            </dd>
          </div>
          <div>
            <dt>状态</dt>
            <dd data-testid="integrator-projection-status">{data.status}</dd>
          </div>
          <div>
            <dt>当前</dt>
            <dd data-testid="integrator-projection-current">
              {data.current === true ? "是" : "否"}
            </dd>
          </div>
          <div>
            <dt>到期（epoch）</dt>
            <dd data-testid="integrator-projection-due">{data.due_at}</dd>
          </div>
          <div>
            <dt>上游应用</dt>
            <dd data-testid="integrator-projection-upstream">
              {data.upstream_application_ref}
            </dd>
          </div>
          <div>
            <dt>请求上下文摘要</dt>
            <dd data-testid="integrator-projection-digest">
              {data.context_digest}
            </dd>
          </div>
          <div>
            <dt>材料要求</dt>
            <dd data-testid="integrator-projection-material">
              {data.material_requirement.material_requirement_id}
              {" · "}
              {data.material_requirement.document_role}
              {" · "}
              {data.material_requirement.operation}
            </dd>
          </div>
          <div>
            <dt>允许的工作负载</dt>
            <dd data-testid="integrator-projection-workload">
              {data.material_requirement.allowed_workload_identity_ids.join(
                ", ",
              )}
            </dd>
          </div>
          <div>
            <dt>批次项数 / 闭包 / 完整性 / 溯源 / 证据资格</dt>
            <dd data-testid="integrator-projection-flags">
              {data.material_requirement.batch_item_count}
              {" · "}
              {String(data.material_requirement.batch_closure_required)}
              {" · "}
              {String(data.material_requirement.integrity_required)}
              {" · "}
              {String(data.material_requirement.provenance_required)}
              {" · "}
              {String(data.material_requirement.evidence_eligibility_required)}
            </dd>
          </div>
          <div>
            <dt>前驱附件</dt>
            <dd data-testid="integrator-projection-predecessor">
              {data.expected_predecessor_attachment_id}
              {" · v"}
              {data.expected_predecessor_attachment_version}
            </dd>
          </div>
          <div>
            <dt>下一附件版本</dt>
            <dd data-testid="integrator-projection-next-attachment">
              {data.next_attachment_version}
            </dd>
          </div>
          <div>
            <dt>下一请求进度 / 源修订 / 前驱修订 / 批次序号</dt>
            <dd data-testid="integrator-projection-next-revision">
              {data.next_request_progress_revision}
              {" · "}
              {data.next_source_revision}
              {" · "}
              {data.expected_predecessor_revision === null
                ? "None"
                : data.expected_predecessor_revision}
              {" · "}
              {data.next_batch_item_sequence}
            </dd>
          </div>
          <div>
            <dt>批次绑定</dt>
            <dd data-testid="integrator-projection-batch">
              {data.batch.batch_id === null ? "None" : data.batch.batch_id}
              {" · "}
              {data.batch.manifest_digest === null
                ? "None"
                : data.batch.manifest_digest}
              {" · "}
              {data.batch.stream_id === null ? "None" : data.batch.stream_id}
            </dd>
          </div>
        </dl>
      </section>
      {requestEnded && (
        <p className="text-sm text-muted-foreground" data-testid="integrator-terminal-note">
          该请求已结束，不能再提交附件版本
        </p>
      )}
        <label className="block text-sm">
          注册信封（JSON）
          <textarea
            className="envelope-field"
            data-testid="integrator-envelope-input"
            rows={10}
            placeholder='{"envelope_id": "...", "command_type": "submit_attachment_version", …}'
            value={envelopeText}
            onChange={(event) => {
              setEnvelopeText(event.target.value);
              setLastReceipt(null);
            }}
            disabled={issuedRef !== null || submit.isPending || requiresReload}
            aria-label="注册信封 JSON"
          />
        </label>
      {parsed === undefined && envelopeText.trim() !== "" && (
        <p className="text-sm text-destructive" data-testid="integrator-envelope-error">
          补充材料载荷不是有效 JSON
        </p>
      )}
      {lastReceipt !== null && (
        <section className="panel" data-testid="integrator-receipt">
          <h3>提交回执（服务端权威）</h3>
          <p
            role="status"
            aria-live="polite"
            data-testid="integrator-disposition-announcement"
          >
            {dispositionAnnouncement(lastReceipt)}
          </p>
          <dl className="facts">
            <div>
              <dt>处置</dt>
              <dd data-testid="integrator-receipt-disposition">
                {lastReceipt.disposition}
              </dd>
            </div>
            <div>
              <dt>原因码</dt>
              <dd data-testid="integrator-receipt-reason">
                {lastReceipt.reason_code ?? "None"}
              </dd>
            </div>
            <div>
              <dt>请求状态</dt>
              <dd data-testid="integrator-receipt-request-status">
                {lastReceipt.request_status ?? "None"}
              </dd>
            </div>
            <div>
              <dt>阶段</dt>
              <dd data-testid="integrator-receipt-phase">
                {lastReceipt.phase ?? "None"}
              </dd>
            </div>
            <div>
              <dt>重放</dt>
              <dd data-testid="integrator-receipt-replayed">
                {String(lastReceipt.replayed)}
              </dd>
            </div>
          </dl>
        </section>
      )}
      {transportUnknown && (
        <p role="status" aria-live="polite" data-testid="integrator-unknown">
          结果未知：网络未确认，重试将使用同一幂等键
        </p>
      )}
      {requiresReload && rejectionReason !== null && (
        <p className="text-sm text-muted-foreground" data-testid="integrator-reload-note">
          提交未接受（{rejectionReason}）：请重新加载权威投影后再试
        </p>
      )}
      <div className="recovery-actions" data-testid="integrator-actions">
        <Button
          variant="secondary"
          onClick={handleReload}
          disabled={submit.isPending || issuedRef !== null}
          data-testid="integrator-reload-button"
        >
          重新加载投影
        </Button>
        <Button
          onClick={handleSubmit}
          disabled={submitBlocked}
          data-testid="integrator-submit-button"
        >
          提交附件版本
        </Button>
        {transportUnknown && (
          <Button
            variant="outline"
            onClick={handleRetry}
            data-testid="integrator-retry-button"
          >
            重试
          </Button>
        )}
      </div>
      <p role="status" aria-live="polite" data-testid="integrator-command-status">
        {submit.isPending
          ? "附件版本提交中…"
          : transportUnknown
            ? "结果未知"
            : requiresReload
              ? "提交未接受"
              : lastReceipt !== null
                ? dispositionAnnouncement(lastReceipt)
                : "等待操作"}
      </p>
    </section>
  );
}
