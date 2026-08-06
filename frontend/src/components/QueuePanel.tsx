import type { QueueManualItem, RecoveryQueueItem } from "../api/client";
import { useQueue } from "../api/hooks";

function RecoveryItemLink({
  item,
  onOpen,
}: {
  item: RecoveryQueueItem;
  onOpen: (workId: string) => void;
}) {
  return (
    <a
      href={`?work=${encodeURIComponent(item.recovery_work_id)}`}
      onClick={(event) => {
        event.preventDefault();
        onOpen(item.recovery_work_id);
      }}
      data-testid="queue-work-link"
      className="block rounded-md border border-border px-3 py-2 [overflow-wrap:anywhere] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
    >
      <span className="flex min-w-0 items-baseline justify-between gap-2">
        <strong>{item.recovery_work_id}</strong>
        <span data-testid="queue-recovery-phase">{item.phase}</span>
      </span>
      <span className="block text-sm text-muted-foreground">
        {item.primary_reason_code} · {item.responsible_party}
      </span>
    </a>
  );
}

function ManualItemLink({
  item,
  onOpen,
}: {
  item: QueueManualItem;
  onOpen: (workId: string) => void;
}) {
  return (
    <a
      href={`?review=${encodeURIComponent(item.work_item_id)}`}
      onClick={(event) => {
        event.preventDefault();
        onOpen(item.work_item_id);
      }}
      data-testid="queue-manual-link"
      className="block rounded-md border border-border px-3 py-2 [overflow-wrap:anywhere] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
    >
      <span className="flex min-w-0 items-baseline justify-between gap-2">
        <strong>{item.application_id}</strong>
        <span data-testid="queue-manual-phase">{item.phase}</span>
      </span>
      <span className="block text-sm text-muted-foreground">
        {item.work_item_id} · {item.route} · {item.mandatory_blockers.length} 个阻断
      </span>
    </a>
  );
}

export default function QueuePanel({
  onOpenWork,
  onOpenReview,
}: {
  onOpenWork: (workId: string) => void;
  onOpenReview: (workId: string) => void;
}) {
  const query = useQueue();
  const accessEnded = query.data?.access_ended === true;

  return (
    <section
      className="panel"
      data-testid="queue-panel"
      aria-labelledby="queue-title"
    >
      <header className="flex items-baseline justify-between gap-2">
        <h2 id="queue-title">受控审核队列</h2>
        <span className="text-sm text-muted-foreground">
          投影水位{" "}
          <span data-testid="queue-watermark">
            {query.data?.projection_watermark ?? "—"}
          </span>
        </span>
      </header>
      <p className="sr-only" role="status" data-testid="queue-status">
        {query.isPending
          ? "队列加载中"
          : query.isError
            ? "队列不可用"
            : accessEnded
              ? "会话已过期"
              : (query.data?.items?.length ?? 0) > 0 ||
                  (query.data?.recovery_items?.length ?? 0) > 0
                ? "队列已同步"
                : "队列为空"}
      </p>

      {query.isPending ? (
        <p data-testid="queue-loading">队列加载中…</p>
      ) : query.isError ? (
        <p data-testid="queue-error">队列不可用</p>
      ) : accessEnded ? (
        <p
          className="text-sm text-muted-foreground"
          role="alert"
          data-testid="queue-access-ended"
        >
          会话已过期：请重新打开受控审核工作台以恢复访问
        </p>
      ) : (
        <>
          <section aria-labelledby="queue-manual-title">
            <h3 id="queue-manual-title">人工复核队列</h3>
            <ul data-testid="queue-items" className="[overflow-wrap:anywhere]">
              {(query.data?.items ?? []).map((item) => (
                <li key={item.work_item_id} data-testid="queue-item">
                  <ManualItemLink item={item} onOpen={onOpenReview} />
                </li>
              ))}
            </ul>
            {(query.data?.items?.length ?? 0) === 0 && (
              <p data-testid="queue-empty" className="text-sm text-muted-foreground">
                队列为空
              </p>
            )}
          </section>
          <section aria-labelledby="queue-recovery-title">
            <h3 id="queue-recovery-title">恢复工作</h3>
            <ul data-testid="queue-recovery-items">
              {(query.data?.recovery_items ?? []).map((item) => (
                <li key={item.recovery_work_id}>
                  <RecoveryItemLink item={item} onOpen={onOpenWork} />
                </li>
              ))}
            </ul>
            {(query.data?.recovery_items?.length ?? 0) === 0 && (
              <p data-testid="queue-recovery-empty" className="text-sm text-muted-foreground">
                无恢复工作
              </p>
            )}
          </section>
        </>
      )}
    </section>
  );
}
