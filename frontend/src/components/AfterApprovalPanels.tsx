import { useExhibitCase, useExhibitCaseGovernance } from "../api/hooks";
import { Button } from "./ui/button";

function asGov(value: unknown) {
  const gov = (value ?? {}) as Record<string, unknown>;
  return {
    delivered: Boolean(gov.delivered),
    cancelled: Boolean(gov.cancelled),
    settled: Boolean(gov.settled),
    deleted: Boolean(gov.deleted),
    exportNote: typeof gov.export_note === "string" ? gov.export_note : null,
  };
}

export function DeliveryStatusPanel() {
  const { data } = useExhibitCase();
  const act = useExhibitCaseGovernance();
  if (!data?.application_id) {
    return (
      <section className="panel" data-testid="s13-delivery-panel">
        <p data-testid="s13-no-application">请先在核验页上传并核验一笔申请。</p>
      </section>
    );
  }
  const approval = (data.approval ?? {}) as { action?: string };
  const gov = asGov(data.governance);
  const blocked = approval.action === "reject";
  const report = (data.report ?? {}) as Record<string, number>;
  return (
    <section className="panel" data-testid="s13-delivery-panel">
      <h2>把核验结论交给下游</h2>
      <p className="demo-limitation">本页不打款。投递命令发到服务端本笔案件。</p>
      <dl className="facts">
        <div><dt>申请</dt><dd>{data.application_id}</dd></div>
        <div>
          <dt>核验</dt>
          <dd>一致 {report.consistent ?? 0} · 不一致 {report.inconsistent ?? 0} · 存疑 {report.uncertain ?? 0}</dd>
        </div>
        <div>
          <dt>批准结果</dt>
          <dd>{approval.action === "approve" ? "批准" : approval.action === "exception" ? "特批" : approval.action === "reject" ? "拒绝" : "尚未批准"}</dd>
        </div>
        <div>
          <dt>投递状态</dt>
          <dd data-testid="s13-delivery-status">{gov.delivered ? "已投递" : "未投递"}</dd>
        </div>
      </dl>
      {blocked ? (
        <p className="demo-limitation">本笔已拒绝，服务端不允许投递。</p>
      ) : (
        <Button type="button" disabled={gov.delivered || act.isPending} onClick={() => act.mutate("deliver")}>
          {gov.delivered ? "已交给下游" : "投递本笔结论"}
        </Button>
      )}
    </section>
  );
}

export function CancelApplicationPanel() {
  const { data } = useExhibitCase();
  const act = useExhibitCaseGovernance();
  if (!data?.application_id) {
    return <section className="panel"><p>请先在核验页上传并核验一笔申请。</p></section>;
  }
  const gov = asGov(data.governance);
  return (
    <section className="panel" data-testid="t16-lifecycle-panel">
      <h2>取消本笔申请</h2>
      <p className="demo-limitation">取消命令发到服务端。取消后不再投递、不再放款。</p>
      <p>申请：{data.application_id}</p>
      <p>状态：{gov.cancelled ? "已取消" : "进行中"}</p>
      <Button type="button" variant="outline" disabled={gov.cancelled || act.isPending} onClick={() => act.mutate("cancel")}>
        {gov.cancelled ? "已取消" : "取消本笔申请"}
      </Button>
    </section>
  );
}

export function SettlementStatusPanel() {
  const { data } = useExhibitCase();
  const act = useExhibitCaseGovernance();
  if (!data?.application_id) {
    return <section className="panel"><p>请先在核验页上传并核验一笔申请。</p></section>;
  }
  const gov = asGov(data.governance);
  const approval = (data.approval ?? {}) as { action?: string };
  const ready = gov.cancelled || approval.action === "reject";
  return (
    <section className="panel">
      <h2>终止后的收尾</h2>
      <p className="demo-limitation">申请取消或拒绝后，通知下游不要再处理这笔。</p>
      <p>申请：{data.application_id}</p>
      <p>清算：{gov.settled ? "已收尾" : ready ? "待收尾" : "申请仍在进行中"}</p>
      <Button type="button" disabled={gov.settled || !ready || act.isPending} onClick={() => act.mutate("settle")}>
        {gov.settled ? "已完成收尾" : "完成终止收尾"}
      </Button>
    </section>
  );
}

export function DeletionStatusPanel() {
  const { data } = useExhibitCase();
  const act = useExhibitCaseGovernance();
  if (!data?.application_id) {
    return <section className="panel"><p>请先在核验页上传并核验一笔申请。</p></section>;
  }
  const gov = asGov(data.governance);
  return (
    <section className="panel">
      <h2>按合规删除本笔副本</h2>
      <p className="demo-limitation">删除标记写在服务端本笔案件上。</p>
      <p>申请：{data.application_id}</p>
      <p>删除：{gov.deleted ? "已标记删除" : "仍保留"}</p>
      <Button type="button" variant="outline" disabled={gov.deleted || act.isPending} onClick={() => act.mutate("delete")}>
        {gov.deleted ? "已删除" : "删除本笔数据"}
      </Button>
    </section>
  );
}

export function ExportStatusPanel() {
  const { data } = useExhibitCase();
  const act = useExhibitCaseGovernance();
  if (!data?.application_id) {
    return <section className="panel"><p>请先在核验页上传并核验一笔申请。</p></section>;
  }
  const gov = asGov(data.governance);
  return (
    <section className="panel">
      <h2>导出本笔核验报告</h2>
      <p className="demo-limitation">导出记录写在服务端。这里导出的是本笔摘要，不是一次性密文包。</p>
      <p>申请：{data.application_id}</p>
      <p>{gov.exportNote || "尚未导出"}</p>
      <Button type="button" disabled={Boolean(gov.exportNote) || act.isPending} onClick={() => act.mutate("export")}>
        {gov.exportNote ? "已导出" : "导出本笔报告"}
      </Button>
    </section>
  );
}
