import { HttpError } from "../api/client";
import { useCurrentRoute } from "../api/hooks";

/** The Lifecycle-owned route as a server authority, shared by every panel
 * that must never derive phase/route client-side.  The error branch runs
 * before the data check so an initial 404/503 never degrades into an endless
 * loading state, and cached route data is never shown after a failed read. */
export default function GateSection({
  applicationId,
}: {
  applicationId: string;
}) {
  const gate = useCurrentRoute(applicationId);
  if (gate.isPending) {
    return <p data-testid="gate-loading">路由加载中…</p>;
  }
  if (gate.isError || gate.data === undefined) {
    const notFound = gate.error instanceof HttpError && gate.error.status === 404;
    return (
      <p data-testid="gate-error">
        {notFound ? "当前路由未找到或无权访问" : "当前路由不可用"}
      </p>
    );
  }
  return (
    <section className="panel" data-testid="gate-panel" aria-labelledby="gate-title">
      <h3 id="gate-title">当前路由（服务端权威）</h3>
      <dl className="facts">
        <div>
          <dt>阶段</dt>
          <dd data-testid="gate-phase">{gate.data.phase}</dd>
        </div>
        <div>
          <dt>路由</dt>
          <dd data-testid="gate-route">{gate.data.route}</dd>
        </div>
        <div>
          <dt>当前性</dt>
          <dd data-testid="gate-currentness">{gate.data.currentness_reason}</dd>
        </div>
      </dl>
    </section>
  );
}
