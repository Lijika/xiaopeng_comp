import { HttpError } from "../api/client";
import { useS13Delivery, type S13QueryResponse } from "../api/hooks";

function S13ErrorState({
  error,
  onReload,
  isReloading,
}: {
  error: Error;
  onReload: () => void;
  isReloading: boolean;
}) {
  if (!(error instanceof HttpError)) {
    return (
      <ErrorStateContent
        label="结果未知"
        code="S13_HTTP_UNKNOWN"
        onReload={onReload}
        isReloading={isReloading}
        testId="s13-error-unknown"
      />
    );
  }
  const code = error.errorCode ?? `S13_HTTP_${error.status}`;
  const state =
    error.status === 403
      ? { label: "Authorization denied", testId: "s13-error-forbidden" }
      : error.status === 404
        ? { label: "Not found", testId: "s13-error-not-found" }
        : error.status === 503
          ? { label: "Unavailable", testId: "s13-error-unavailable" }
          : { label: "Request failed", testId: "s13-error-unavailable" };
  return (
    <ErrorStateContent
      label={state.label}
      code={code}
      onReload={onReload}
      isReloading={isReloading}
      testId={state.testId}
    />
  );
}

function ErrorStateContent({
  label,
  code,
  onReload,
  isReloading,
  testId,
}: {
  label: string;
  code: string;
  onReload: () => void;
  isReloading: boolean;
  testId: string;
}) {
  return (
    <div data-testid={testId} role="alert">
      <p>{label}</p>
      <p data-testid="s13-error-code">{code}</p>
      <button
        type="button"
        data-testid="s13-reload"
        onClick={onReload}
        disabled={isReloading}
      >
        {isReloading ? "Reloading" : "Reload authoritative view"}
      </button>
    </div>
  );
}

function leafText(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}

function Section({
  id,
  title,
  testId,
  children,
}: {
  id: string;
  title: string;
  testId: string;
  children: React.ReactNode;
}) {
  return (
    <section className="panel" data-testid={testId} aria-labelledby={id}>
      <h2 id={id}>{title}</h2>
      {children}
    </section>
  );
}

function EmptyProjection() {
  return (
    <div data-testid="s13-delivery-panel">
      <p data-testid="s13-no-application" role="status" aria-live="polite">
        请选择应用以查看投递视图
      </p>
      <Section id="s13-gate-title" title="Verification Completed" testId="s13-gate-section">
        <p data-testid="s13-gate-empty">—</p>
      </Section>
      <Section id="s13-routing-title" title="Verification Routing" testId="s13-routing-section">
        <p data-testid="s13-routing-empty">—</p>
      </Section>
      <Section id="s13-obligation-title" title="Delivery Obligation" testId="s13-obligation-section">
        <p data-testid="s13-obligation-empty">—</p>
      </Section>
      <Section id="s13-receipt-title" title="Delivery Receipt" testId="s13-receipt-section">
        <p data-testid="s13-receipt-empty">—</p>
      </Section>
    </div>
  );
}

function RoutingHistory({ data }: { data: S13QueryResponse }) {
  if (data.routing_history.length === 0) {
    return <p data-testid="s13-routing-history-empty">No sealed routing history</p>;
  }
  return (
    <ol className="history-list" data-testid="s13-routing-history">
      {data.routing_history.map((entry) => (
        <li
          key={`${entry.cycle}:${entry.completion_event_id}`}
          data-testid="s13-routing-history-entry"
        >
          <dl className="facts">
            <div>
              <dt>Cycle</dt>
              <dd>{leafText(entry.cycle)}</dd>
            </div>
            <div>
              <dt>Route</dt>
              <dd>{leafText(entry.route)}</dd>
            </div>
            <div>
              <dt>Attribution Kind</dt>
              <dd>{leafText(entry.attribution_kind)}</dd>
            </div>
            <div>
              <dt>Decision ID</dt>
              <dd>{leafText(entry.attribution.decision_id)}</dd>
            </div>
            <div>
              <dt>Work Item ID</dt>
              <dd>{leafText(entry.attribution.work_item_id)}</dd>
            </div>
            <div>
              <dt>Exception Request ID</dt>
              <dd>{leafText(entry.attribution.request_id)}</dd>
            </div>
            <div>
              <dt>Batch ID</dt>
              <dd>{leafText(entry.attribution.batch_id)}</dd>
            </div>
            <div>
              <dt>Batch Work Item IDs</dt>
              <dd>{entry.attribution.work_item_ids.map(leafText).join(", ") || "—"}</dd>
            </div>
            <div>
              <dt>Completion Event ID</dt>
              <dd>{leafText(entry.completion_event_id)}</dd>
            </div>
            <div>
              <dt>Completion Lifecycle Revision</dt>
              <dd>{leafText(entry.completion_lifecycle_revision)}</dd>
            </div>
            <div>
              <dt>Run ID</dt>
              <dd>{leafText(entry.run_id)}</dd>
            </div>
            <div>
              <dt>Evidence Snapshot ID</dt>
              <dd>{leafText(entry.evidence_snapshot_id)}</dd>
            </div>
            <div>
              <dt>Evidence Snapshot Digest</dt>
              <dd>{leafText(entry.evidence_snapshot_digest)}</dd>
            </div>
            <div>
              <dt>Release ID</dt>
              <dd>{leafText(entry.release_id)}</dd>
            </div>
            <div>
              <dt>Release Digest</dt>
              <dd>{leafText(entry.release_digest)}</dd>
            </div>
            <div>
              <dt>Checker Build</dt>
              <dd>{leafText(entry.checker_build)}</dd>
            </div>
            <div>
              <dt>Route Basis Digest</dt>
              <dd>{leafText(entry.route_basis_digest)}</dd>
            </div>
            <div>
              <dt>Obligation ID</dt>
              <dd>{leafText(entry.obligation_id)}</dd>
            </div>
            <div>
              <dt>Operation ID</dt>
              <dd>{leafText(entry.operation_id)}</dd>
            </div>
          </dl>
        </li>
      ))}
    </ol>
  );
}

export default function T15DeliveryPanel({
  applicationId,
}: {
  applicationId: string | null;
}) {
  const delivery = useS13Delivery(applicationId);

  if (!applicationId) return <EmptyProjection />;
  if (delivery.isPending) {
    return (
      <div data-testid="s13-delivery-panel">
        <p data-testid="s13-delivery-loading" role="status" aria-live="polite">
          正在加载投递视图…
        </p>
      </div>
    );
  }
  if (delivery.isError) {
    return (
      <div data-testid="s13-delivery-panel">
        <S13ErrorState
          error={delivery.error}
          onReload={() => void delivery.refetch()}
          isReloading={delivery.isFetching}
        />
      </div>
    );
  }

  const data = delivery.data;
  const obligation = data.obligation;
  return (
    <div data-testid="s13-delivery-panel">
      <Section id="s13-gate-title" title="Verification Completed" testId="s13-gate-section">
        <dl className="facts">
          <div>
            <dt>Verification Completed</dt>
            <dd data-testid="s13-verification-completed">
              {data.verification_completed ? "completed" : "not completed"}
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
            <dd data-testid="s13-lifecycle-revision">{leafText(data.lifecycle_revision)}</dd>
          </div>
        </dl>
      </Section>

      <Section id="s13-routing-title" title="Verification Routing" testId="s13-routing-section">
        <dl className="facts">
          <div>
            <dt>Current Route</dt>
            <dd data-testid="s13-route">{leafText(data.route)}</dd>
          </div>
          <div>
            <dt>Current Attribution Kind</dt>
            <dd data-testid="s13-attribution-kind">
              {leafText(obligation?.attribution_kind)}
            </dd>
          </div>
          <div>
            <dt>Schema Version</dt>
            <dd data-testid="s13-schema-version">{leafText(data.schema_version)}</dd>
          </div>
        </dl>
        <RoutingHistory data={data} />
      </Section>

      <Section id="s13-obligation-title" title="Delivery Obligation" testId="s13-obligation-section">
        {obligation === null ? (
          <p data-testid="s13-obligation-none" role="status">
            No delivery obligation
          </p>
        ) : (
          <dl className="facts">
            <div>
              <dt>Obligation ID</dt>
              <dd data-testid="s13-obligation-id">{leafText(obligation.obligation_id)}</dd>
            </div>
            <div>
              <dt>Operation ID</dt>
              <dd data-testid="s13-operation-id">{leafText(obligation.operation_id)}</dd>
            </div>
            <div>
              <dt>Recipient</dt>
              <dd data-testid="s13-recipient-id">{leafText(obligation.recipient_id)}</dd>
            </div>
            <div>
              <dt>Adapter</dt>
              <dd data-testid="s13-adapter">
                {leafText(obligation.adapter_id)} / {leafText(obligation.adapter_version)}
              </dd>
            </div>
            <div>
              <dt>Payload Ref</dt>
              <dd data-testid="s13-payload-ref">{leafText(obligation.payload_ref)}</dd>
            </div>
            <div>
              <dt>Payload Digest</dt>
              <dd data-testid="s13-payload-digest">{leafText(obligation.payload_digest)}</dd>
            </div>
            <div>
              <dt>Payload Schema</dt>
              <dd data-testid="s13-payload-schema">{leafText(obligation.payload_schema)}</dd>
            </div>
            <div>
              <dt>Obligation Status</dt>
              <dd data-testid="s13-obligation-status">{leafText(obligation.status)}</dd>
            </div>
          </dl>
        )}
      </Section>

      <Section id="s13-receipt-title" title="Delivery Receipt" testId="s13-receipt-section">
        <dl className="facts">
          <div>
            <dt>Delivery Status</dt>
            <dd data-testid="s13-delivery-status">{leafText(data.delivery_status)}</dd>
          </div>
          <div>
            <dt>Attempt Count</dt>
            <dd data-testid="s13-attempt-count">{leafText(data.attempt_count)}</dd>
          </div>
          <div>
            <dt>Projection Watermark</dt>
            <dd data-testid="s13-projection-watermark">{leafText(data.projection_watermark)}</dd>
          </div>
          <div>
            <dt>Store Revision</dt>
            <dd data-testid="s13-store-revision">{leafText(data.store_revision)}</dd>
          </div>
        </dl>
      </Section>
    </div>
  );
}
