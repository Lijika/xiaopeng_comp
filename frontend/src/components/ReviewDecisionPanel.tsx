import { useState } from "react";

import { useExhibitCase, useExhibitCaseReview } from "../api/hooks";
import { Button } from "./ui/button";

const ACTIONS = [
  { action: "confirm" as const, label: "确认结论", hint: "核验结果可以采信，进入批准。" },
  { action: "need_material" as const, label: "需要补材料", hint: "缺页、串页或拍糊了，转到补材料。" },
  { action: "exception" as const, label: "提交特批", hint: "业务上允许例外，转到批准页签字。" },
];

export default function ReviewDecisionPanel() {
  const { data } = useExhibitCase();
  const review = useExhibitCaseReview();
  const saved = (data?.review ?? null) as { action?: string; note?: string } | null;
  const [note, setNote] = useState(saved?.note ?? "");
  if (!data?.application_id) {
    return (
      <section className="panel" data-testid="review-decision-panel">
        <h2>人工判断</h2>
        <p className="demo-limitation">还没有从第 1 步带过来的申请。请先上传 JSON 并完成核验。</p>
      </section>
    );
  }
  return (
    <section className="panel" data-testid="review-decision-panel">
      <h2>人工判断</h2>
      <p className="demo-limitation">判断会提交到服务端本笔案件，后续步骤读取同一份记录。</p>
      <label htmlFor="review-note">判断说明</label>
      <textarea
        id="review-note"
        data-testid="review-note"
        rows={3}
        value={note}
        onChange={(event) => setNote(event.target.value)}
      />
      <div className="decision-actions">
        {ACTIONS.map((item) => (
          <Button
            key={item.action}
            type="button"
            variant={item.action === "confirm" ? "default" : "outline"}
            data-testid={`review-action-${item.action}`}
            disabled={review.isPending}
            onClick={() => review.mutate({ action: item.action, note })}
          >
            {item.label}
          </Button>
        ))}
      </div>
      {saved?.action && (
        <p className="demo-status" data-testid="review-decision-saved">
          服务端已记录：
          {saved.action === "confirm"
            ? "确认结论"
            : saved.action === "need_material"
              ? "需要补材料"
              : "提交特批"}
          {saved.note ? ` · ${saved.note}` : ""}
        </p>
      )}
    </section>
  );
}
