import { useState } from "react";

import { useExhibitCase, useExhibitCaseApproval } from "../api/hooks";
import { Button } from "./ui/button";

const ACTIONS = [
  { action: "approve" as const, label: "批准", hint: "按核验结论放行，进入后续投递。" },
  { action: "exception" as const, label: "特批", hint: "明知有差异，业务上仍允许放行。" },
  { action: "reject" as const, label: "拒绝", hint: "不放行，本笔到此结束。" },
];

export default function ApprovalDecisionPanel() {
  const { data } = useExhibitCase();
  const approvalMut = useExhibitCaseApproval();
  const review = data?.review as { action?: string; note?: string } | null | undefined;
  const saved = data?.approval as { action?: string; note?: string } | null | undefined;
  const [note, setNote] = useState(saved?.note ?? "");
  if (!data?.application_id) {
    return (
      <section className="panel" data-testid="approval-decision-panel">
        <h2>批准</h2>
        <p className="demo-limitation">还没有从第 1 步带过来的申请。请先上传 JSON 并完成核验。</p>
      </section>
    );
  }
  return (
    <section className="panel" data-testid="approval-decision-panel">
      <h2>批准</h2>
      <p className="demo-limitation">决议提交到服务端本笔案件，投递页会按批准结果拦截已拒绝申请。</p>
      {review?.action && (
        <p data-testid="approval-review-carry">
          上一岗判断：
          {review.action === "confirm"
            ? "确认结论"
            : review.action === "need_material"
              ? "需要补材料"
              : "提交特批"}
          {review.note ? ` · ${review.note}` : ""}
        </p>
      )}
      <label htmlFor="approval-note">审批意见</label>
      <textarea
        id="approval-note"
        data-testid="approval-note"
        rows={3}
        value={note}
        onChange={(event) => setNote(event.target.value)}
      />
      <div className="decision-actions">
        {ACTIONS.map((item) => (
          <Button
            key={item.action}
            type="button"
            variant={item.action === "reject" ? "outline" : item.action === "approve" ? "default" : "secondary"}
            data-testid={`approval-action-${item.action}`}
            disabled={approvalMut.isPending}
            onClick={() => approvalMut.mutate({ action: item.action, note })}
          >
            {item.label}
          </Button>
        ))}
      </div>
      {saved?.action && (
        <p className="demo-status" data-testid="approval-decision-saved">
          服务端已记录：
          {saved.action === "approve" ? "批准" : saved.action === "exception" ? "特批" : "拒绝"}
          {saved.note ? ` · ${saved.note}` : ""}
        </p>
      )}
    </section>
  );
}
