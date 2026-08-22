import { useState } from "react";

import { HttpError, isDefinitiveS13Rejection } from "../api/client";
import {
  useS13Compensate,
  useS13Delivery,
  useS13ProcessNextDelivery,
  useS13Reconcile,
  type S13QueryResponse,
} from "../api/hooks";

/**
 * The S13 error-state mapping: the exact registered envelope code renders
 * beside one stable label.  Authorization-denial content carries no
 * obligation or delivery identifiers; stale/duplicate/unauthorized/
 * out-of-order map to visible structured codes.  A previous authoritative
 * presentation is cleared before the error renders (data is not shown when
 * isError is true).
 */
function s13ErrorState(error: Error): {
  code: string;
  label: string;
  testId: string;
} | null {
  if (!(error instanceof HttpError)) return null;
  const code = error.errorCode ?? `S13_HTTP_${error.status}`;
  if (error.status === 403) {
    return { code, label: "Authorization denied", testId: "s13-error-forbidden" };
  }
  if (error.status === 404) {
    return { code, label: "Not found", testId: "s13-error-not-found" };
  }
  if (error.status === 422) {
    return { code, label: "Invalid command", testId: "s13-error-invalid" };
  }
  if (error.status === 503) {
    return { code, label: "Unavailable", testId: "s13-error-unavailable" };
  }
  return { code, label: "Request failed", testId: "s13-error-unavailable" };
}

function S13ErrorState({ error }: { error: Error }) {
  const state = s13ErrorState(error);
  if (state === null) {
    return (
      <p role="status" aria-live="polite" data-testid="s13-unknown-outcome">
        结果未知：网络未确认，请保留操作标识并通过对账确认后再重试
      </p>
    );
  }
  return (
    <section className="panel" data-testid={state.testId} role="alert">
      <p>{state.label}</p>
      <p data-testid="s13-error-code">{state.code}</p>
    </section>
  );
}

function leafText(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}

/** Display text for delivery_status: server value preserved; unknown future values get neutral fallback. */
function deliveryStatusText(status: string): string {
  const known: Record<string, string> = {
    pending: "pending",
    sent: "sent",
    received: "received",
    failed: "failed",
    unknown: "unknown",
    unavailable: "unavailable",
    blocked: "blocked",
    retry_scheduled: "retry_scheduled",
    compensated: "compensated",
    none: "none",
  };
  return known[status] ?? status;
}

function Section({
  labelledBy,
  title,
  testId,
  children,
}: {
  labelledBy: string;
  title: string;
  testId: string;
  children: React.ReactNode;
}) {
  return (
    <section className="panel" data-testid={testId} aria-labelledby={labelledBy}>
      <h3 id={labelledBy}>{title}</h3>
      {children}
    </section>
  );
}

/**
 * T15 delivery console panel mounted only for /controlled/s13.  It shows four
 * visibly separate regions: Verification Completed gate, Verification Routing,
 * Delivery Obligation, and Delivery Receipt.  The UI never describes
 * Verification Routing as a disbursement decision and never equates obligation
 * creation with downstream delivery receipt.  Every command is an explicit
 * authorized action; mount/refetch emits zero business POSTs.  Unknown
 * transport outcomes retain the original operation_id and require
 * same-operation reconciliation before retry.
 */
export default function T15DeliveryPanel({
  applicationId,
}: {
  applicationId: string | null;
}) {
  const delivery = useS13Delivery(applicationId);
  const reconcile = useS13Reconcile();
  const compensate = useS13Compensate();
  const processNext = useS13ProcessNextDelivery();
  const [commandOutcome, setCommandOutcome] = useState<{
    kind: "reconcile" | "compensate" | "process";
    status: string;
    reasonCode?: string | null;
    operationId?: string | null;
  } | null>(null);
  const [unknownOutcome, setUnknownOutcome] = useState<{
    operationId: string | null;
  } | null>(null);

  // No application selected: navigation state before any S13 query.
  if (applicationId === null || applicationId === "") {
    return (
      <section className="panel" data-testid="s13-delivery-panel">
        <p data-testid="s13-no-application" role="status" aria-live="polite">
          请选择应用以查看投递视图
        </p>
        <Section
          labelledBy="s13-gate-title"
          title="Verification Completed"
          testId="s13-gate-section"
        >
          <p data-testid="s13-gate-empty">—</p>
        </Section>
        <Section
          labelledBy="s13-routing-title"
          title="Verification Routing"
          testId="s13-routing-section"
        >
          <p data-testid="s13-routing-empty">—</p>
        </Section>
        <Section
          labelledBy="s13-obligation-title"
          title="Delivery Obligation"
          testId="s13-obligation-section"
        >
          <p data-testid="s13-obligation-empty">—</p>
        </Section>
        <Section
          labelledBy="s13-receipt-title"
          title="Delivery Receipt"
          testId="s13-receipt-section"
        >
          <p data-testid="s13-receipt-empty">—</p>
        </Section>
      </section>
    );
  }

  if (delivery.isPending) {
    return (
      <section className="panel" data-testid="s13-delivery-panel">
        <p data-testid="s13-delivery-loading" role="status" aria-live="polite">
          正在加载投递视图…
        </p>
      </section>
    );
  }

  if (delivery.isError) {
    return (
      <section className="panel" data-testid="s13-delivery-panel">
        <S13ErrorState error={delivery.error} />
      </section>
    );
  }

  const data: S13QueryResponse = delivery.data;
  const obligation = data.obligation;
  const deliveryStatus = deliveryStatusText(data.delivery_status);
  const verificationCompleted = data.verification_completed;
  // Obligation presence is a distinct server fact; it never implies receipt.
  const obligationPresent = obligation !== null;

  const handleReconcile = () => {
    if (obligation === null || reconcile.isPending) return;
    reconcile.mutate(
      { obligation_id: obligation.obligation_id },
      {
        onSuccess: (res) => {
          setCommandOutcome({
            kind: "reconcile",
            status: res.delivery_status ?? res.status,
            reasonCode: res.reason_code ?? null,
            operationId: res.operation_id ?? obligation.operation_id,
          });
          setUnknownOutcome(null);
        },
        onError: (error) => {
          if (!isDefinitiveS13Rejection(error)) {
            setUnknownOutcome({ operationId: obligation.operation_id });
          } else {
            setCommandOutcome({
              kind: "reconcile",
              status: "rejected",
              reasonCode: error instanceof HttpError ? error.errorCode ?? null : null,
              operationId: obligation.operation_id,
            });
          }
        },
      },
    );
  };

  const handleCompensate = () => {
    if (obligation === null || compensate.isPending) return;
    compensate.mutate(
      { obligation_id: obligation.obligation_id },
      {
        onSuccess: (res) => {
          setCommandOutcome({
            kind: "compensate",
            status: res.status,
            reasonCode: res.reason_code ?? null,
            operationId: res.operation_id ?? obligation.operation_id,
          });
          setUnknownOutcome(null);
        },
        onError: (error) => {
          if (!isDefinitiveS13Rejection(error)) {
            setUnknownOutcome({ operationId: obligation.operation_id });
          } else {
            setCommandOutcome({
              kind: "compensate",
              status: "rejected",
              reasonCode: error instanceof HttpError ? error.errorCode ?? null : null,
              operationId: obligation.operation_id,
            });
          }
        },
      },
    );
  };

  const handleProcessNext = () => {
    if (processNext.isPending) return;
    processNext.mutate(
      {},
      {
        onSuccess: (res) => {
          setCommandOutcome({
            kind: "process",
            status: res.status,
            reasonCode: res.reason_code ?? null,
            operationId: res.operation_id ?? null,
          });
          setUnknownOutcome(null);
        },
        onError: (error) => {
          if (!isDefinitiveS13Rejection(error)) {
            setUnknownOutcome({ operationId: obligation?.operation_id ?? null });
          } else {
            setCommandOutcome({
              kind: "process",
              status: "rejected",
              reasonCode: error instanceof HttpError ? error.errorCode ?? null : null,
              operationId: null,
            });
          }
        },
      },
    );
  };

  return (
    <section className="panel" data-testid="s13-delivery-panel">
      {/* 1. Verification Completed gate — a Lifecycle fact, never a disbursement decision */}
      <Section
        labelledBy="s13-gate-title"
        title="Verification Completed"
        testId="s13-gate-section"
      >
        <dl className="facts">
          <div>
            <dt>Verification Completed</dt>
            <dd data-testid="s13-verification-completed">
              {verificationCompleted ? "completed" : "not completed"}
            </dd>
          </div>
          <div>
            <dt>Phase</dt>
            <dd data-testid="s13-phase">{leafText(data.phase)}</dd>
          </div>
          <div>
            <dt>Cycle</dt>
            <dd data-testid="s13-cycle">{leafText(data.cycle)}</dd>
          </div>
          <div>
            <dt>Lifecycle Revision</dt>
            <dd data-testid="s13-lifecycle-revision">
              {leafText(data.lifecycle_revision)}
            </dd>
          </div>
        </dl>
        <p className="text-sm text-muted-foreground" data-testid="s13-gate-note">
          Verification Completed is the positive completion gate owned by the
          Lifecycle authority.
        </p>
      </Section>

      {/* 2. Verification Routing — immutable route with attribution provenance */}
      <Section
        labelledBy="s13-routing-title"
        title="Verification Routing"
        testId="s13-routing-section"
      >
        <dl className="facts">
          <div>
            <dt>Route</dt>
            <dd data-testid="s13-route">{leafText(data.route)}</dd>
          </div>
          <div>
            <dt>Attribution Kind</dt>
            <dd data-testid="s13-attribution-kind">
              {obligation ? leafText(obligation.attribution_kind) : "—"}
            </dd>
          </div>
          <div>
            <dt>Cycle</dt>
            <dd data-testid="s13-routing-cycle">{leafText(data.cycle)}</dd>
          </div>
          <div>
            <dt>Schema Version</dt>
            <dd data-testid="s13-schema-version">
              {leafText(data.schema_version)}
            </dd>
          </div>
        </dl>
        <p className="text-sm text-muted-foreground" data-testid="s13-routing-note">
          Verification Routing is the immutable route and its attribution
          provenance.
        </p>
      </Section>

      {/* 3. Delivery Obligation — an established responsibility, distinct from receipt */}
      <Section
        labelledBy="s13-obligation-title"
        title="Delivery Obligation"
        testId="s13-obligation-section"
      >
        {!obligationPresent ? (
          <p data-testid="s13-obligation-none" role="status">
            No delivery obligation — downstream delivery is not yet established
          </p>
        ) : (
          <dl className="facts">
            <div>
              <dt>Obligation ID</dt>
              <dd className="break-all" data-testid="s13-obligation-id">
                {leafText(obligation!.obligation_id)}
              </dd>
            </div>
            <div>
              <dt>Operation ID</dt>
              <dd className="break-all" data-testid="s13-operation-id">
                {leafText(obligation!.operation_id)}
              </dd>
            </div>
            <div>
              <dt>Recipient</dt>
              <dd data-testid="s13-recipient-id">
                {leafText(obligation!.recipient_id)}
              </dd>
            </div>
            <div>
              <dt>Adapter</dt>
              <dd data-testid="s13-adapter">
                {leafText(obligation!.adapter_id)} /{" "}
                {leafText(obligation!.adapter_version)}
              </dd>
            </div>
            <div>
              <dt>Payload Ref</dt>
              <dd className="break-all" data-testid="s13-payload-ref">
                {leafText(obligation!.payload_ref)}
              </dd>
            </div>
            <div>
              <dt>Payload Digest</dt>
              <dd className="break-all" data-testid="s13-payload-digest">
                {leafText(obligation!.payload_digest)}
              </dd>
            </div>
            <div>
              <dt>Payload Schema</dt>
              <dd data-testid="s13-payload-schema">
                {leafText(obligation!.payload_schema)}
              </dd>
            </div>
            <div>
              <dt>Obligation Status</dt>
              <dd data-testid="s13-obligation-status">
                {leafText(obligation!.status)}
              </dd>
            </div>
          </dl>
        )}
        <p className="text-sm text-muted-foreground" data-testid="s13-obligation-note">
          Delivery Obligation records an established downstream responsibility;
          creation carries no receipt claim.
        </p>
      </Section>

      {/* 4. Delivery Receipt — server-owned progress, separate from obligation creation */}
      <Section
        labelledBy="s13-receipt-title"
        title="Delivery Receipt"
        testId="s13-receipt-section"
      >
        <dl className="facts">
          <div>
            <dt>Delivery Status</dt>
            <dd data-testid="s13-delivery-status">{deliveryStatus}</dd>
          </div>
          <div>
            <dt>Attempt Count</dt>
            <dd data-testid="s13-attempt-count">
              {leafText(data.attempt_count)}
            </dd>
          </div>
          <div>
            <dt>Projection Watermark</dt>
            <dd data-testid="s13-projection-watermark">
              {leafText(data.projection_watermark)}
            </dd>
          </div>
          <div>
            <dt>Store Revision</dt>
            <dd data-testid="s13-store-revision">
              {leafText(data.store_revision)}
            </dd>
          </div>
        </dl>
        <p className="text-sm text-muted-foreground" data-testid="s13-receipt-note">
          Delivery Receipt reports the server-owned delivery progress;
          obligation existence does not imply receipt.
        </p>

        {/* Explicit authorized command controls; visible only when DTO authorizes them */}
        <div className="flex flex-wrap gap-2" data-testid="s13-command-bar">
          <button
            type="button"
            data-testid="s13-reconcile-button"
            disabled={
              obligation === null ||
              reconcile.isPending ||
              // Retry is unavailable until same-operation reconciliation proves not_executed
              unknownOutcome !== null
            }
            aria-disabled={
              obligation === null ||
              reconcile.isPending ||
              unknownOutcome !== null
            }
            onClick={handleReconcile}
          >
            Reconcile
          </button>
          <button
            type="button"
            data-testid="s13-compensate-button"
            disabled={obligation === null || compensate.isPending}
            aria-disabled={obligation === null || compensate.isPending}
            onClick={handleCompensate}
          >
            Compensate
          </button>
          <button
            type="button"
            data-testid="s13-process-next-button"
            disabled={processNext.isPending || unknownOutcome !== null}
            aria-disabled={processNext.isPending || unknownOutcome !== null}
            onClick={handleProcessNext}
          >
            Process Next Delivery
          </button>
        </div>

        {unknownOutcome !== null && (
          <p
            role="status"
            aria-live="polite"
            data-testid="s13-unknown-outcome"
          >
            Unknown outcome — retain operation{" "}
            <span data-testid="s13-unknown-operation-id">
              {leafText(unknownOutcome.operationId)}
            </span>{" "}
            and reconcile before retry
          </p>
        )}

        {commandOutcome !== null && (
          <p role="status" aria-live="polite" data-testid="s13-command-outcome">
            Command {commandOutcome.kind}: {commandOutcome.status}
            {commandOutcome.reasonCode
              ? ` (${commandOutcome.reasonCode})`
              : ""}
            {commandOutcome.operationId
              ? ` — operation ${commandOutcome.operationId}`
              : ""}
          </p>
        )}

        {(reconcile.isPending ||
          compensate.isPending ||
          processNext.isPending) && (
          <p role="status" aria-live="polite" data-testid="s13-command-pending">
            Processing command…
          </p>
        )}

        {reconcile.isError && isDefinitiveS13Rejection(reconcile.error) && (
          <section role="alert" data-testid="s13-reconcile-error">
            <S13ErrorState error={reconcile.error} />
          </section>
        )}
        {compensate.isError && isDefinitiveS13Rejection(compensate.error) && (
          <section role="alert" data-testid="s13-compensate-error">
            <S13ErrorState error={compensate.error} />
          </section>
        )}
        {reconcile.isError && !isDefinitiveS13Rejection(reconcile.error) && (
          <p role="status" aria-live="polite" data-testid="s13-reconcile-unknown">
            Reconcile result unknown — operation {obligation?.operation_id} retained
          </p>
        )}
        {compensate.isError && !isDefinitiveS13Rejection(compensate.error) && (
          <p role="status" aria-live="polite" data-testid="s13-compensate-unknown">
            Compensate result unknown — operation {obligation?.operation_id} retained
          </p>
        )}
      </Section>
    </section>
  );
}
