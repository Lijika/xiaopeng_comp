import { useEffect, useState, type ReactElement } from "react";

import { HttpError } from "../api/client";
import {
  useDemoBatchCheck,
  useDemoEvaluationSummary,
  type DemoBatchCheckResponse,
  type DemoBatchItem,
  type DemoEvaluationSummaryResponse,
} from "../api/hooks";
import { humanEvalWarning, humanHonestyNote, ruleTitle } from "../lib/demoCopy";
import {
  readExhibitUploads,
  type ExhibitUpload,
} from "../lib/exhibitSession";
import { Button } from "./ui/button";

const OUTCOME_ZH: Record<string, string> = {
  completed: "全部完成",
  partial: "部分完成",
  failed: "全部失败",
};

const ITEM_OUTCOME_ZH: Record<string, string> = {
  completed: "已完成",
  failed: "失败",
};

const VERDICT_ZH: Record<string, string> = {
  consistent: "一致",
  inconsistent: "不一致",
  uncertain: "存疑",
  skipped: "跳过",
};

/** The failure states render one fixed generic message only: the server
 * error body is never copied into the UI, so no caller or internal detail
 * (codes, paths, exception text) can surface to the user. */
const BATCH_FAILURE_TEXT = "批量校验失败，请稍后重试";
const BATCH_ITEM_FAILURE_TEXT = "条目校验失败，请稍后重试";
/** The only code-mapped bound copy: the registered cap rejection gets fixed
 * cap-specific copy; the bound number itself stays on the separate
 * server-owned cap label. */
const CAP_FAILURE_TEXT = "所选样例数量超过服务端上限，请减少选择";
const EVAL_FAILURE_TEXT = "评估摘要不可用";
const EVAL_EMPTY_TEXT = "当前语料无可用评估数据";

/** The closed demo batch item renderer: explicit terminal outcome, server
 * summary, issues, or the fixed generic per-item failure.  Absence never
 * implies success. */
function BatchItem({ item }: { item: DemoBatchItem }) {
  const summary = item.summary ?? null;
  const issues = item.issues ?? [];
  if (item.outcome === "failed" || summary === null) {
    // The fixed generic per-item failure only: the server error body is
    // never copied into the UI, so no code or internal detail can surface.
    return (
      <p className="demo-error" data-testid="demo-batch-item-error">
        {BATCH_ITEM_FAILURE_TEXT}
      </p>
    );
  }
  return (
    <>
      <p>申请单：{item.application_id ?? "—"}</p>
      <p>
        一致 {summary.consistent} · 不一致 {summary.inconsistent} ·
        存疑 {summary.uncertain} · 跳过 {summary.skipped}
      </p>
      {issues.length > 0 && (
        <ul className="demo-batch-issues">
          {issues.map((issue) => (
            <li
              key={issue.rule_id}
              data-testid={`demo-batch-issue-${issue.rule_id}`}
            >
              {ruleTitle(issue.rule_id)} · {VERDICT_ZH[issue.verdict] ?? issue.verdict} ·{" "}
              {issue.message}
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

/** The closed read-only evaluation-summary renderer.  It renders only the
 * server DTO: claims, scope, counts/rates (or the explicit empty state),
 * warnings, and the honesty note.  It never renders or derives PASS. */
function Metric({
  code,
  label,
  hint,
  value,
}: {
  code: string;
  label: string;
  hint: string;
  value: string | number;
}) {
  return (
    <div>
      <dt>
        <span className="metric-label">{label}</span>
        <span className="metric-key">{code}</span>
      </dt>
      <dd>{value}</dd>
      <p className="metric-hint">{hint}</p>
    </div>
  );
}

function DemoEvalSummary({ data }: { data: DemoEvaluationSummaryResponse }) {
  const counts = data.counts ?? null;
  const rates = data.rates ?? null;
  const warnings = data.warnings ?? [];
  const claimZh =
    data.claim === "C-DEV-REG" ? "开发/回归集成绩（非正式生产）" : data.claim;
  const gapZh =
    data.performance_gap === "UNVERIFIED"
      ? "尚未用真实 OCR 全量复核"
      : data.performance_gap;
  return (
    <div className="demo-eval">
      <div className="demo-check-head">
        <span className="sr-only" data-testid="demo-eval-claim">
          {data.claim}
        </span>
        <span className="sr-only" data-testid="demo-eval-gap">
          {data.performance_gap}
        </span>
        <span className="demo-limitation">
          {claimZh} · {gapZh}
        </span>
      </div>
      <p className="demo-limitation" data-testid="demo-eval-scope">
        {data.scope}
      </p>
      {data.summary_state === "empty" || counts === null || rates === null ? (
        <p data-testid="demo-eval-empty" role="status">
          {EVAL_EMPTY_TEXT}
        </p>
      ) : (
        <>
          <p className="demo-eval-lead">
            对照三项指标：自动化覆盖率 ≥ 80%，误报（把对的判成错的）≤
            5%，漏报（把错的判成对的）≤ 3%。下面的数来自固定评测集，不是这一次点按钮算出来的。
          </p>
          <h3 className="demo-eval-h">核验三项指标</h3>
          <dl className="facts metric-highlight" data-testid="demo-eval-rates">
            <Metric
              code="coverage"
              label="自动化覆盖率"
              hint="有多少核对项系统给出了明确结论（不是跳过）。目标 ≥ 80%。"
              value={rates.coverage}
            />
            <Metric
              code="false_positive_rate"
              label="误报率"
              hint="本来一致，却被判成不一致。目标 ≤ 5%。越低越好。"
              value={rates.false_positive_rate}
            />
            <Metric
              code="false_negative_rate"
              label="漏报率"
              hint="本来不一致，却被判成一致。目标 ≤ 3%。这是放款风险，必须压住。"
              value={rates.false_negative_rate}
            />
            <Metric
              code="accuracy"
              label="明确结论的准确率"
              hint="在「说得清对错」的核对项里，判对了多少。"
              value={rates.accuracy}
            />
            <Metric
              code="miss_rate"
              label="漏检率"
              hint="真有问题，却被说成一致或存疑。用来防止用「存疑」把漏报藏起来。"
              value={rates.miss_rate}
            />
            <Metric
              code="uncertain_rate"
              label="存疑比例"
              hint="系统不敢自动下结论、要人看一眼的比例。不是失败，是诚实。"
              value={rates.uncertain_rate}
            />
          </dl>
          <h3 className="demo-eval-h">评测规模</h3>
          <dl className="facts" data-testid="demo-eval-counts">
            <Metric
              code="n_apps_loaded"
              label="申请数"
              hint="纳入本次评测的申请笔数。"
              value={counts.n_apps_loaded}
            />
            <Metric
              code="n_check_ok"
              label="完成核验"
              hint="成功产出报告的申请数。"
              value={counts.n_check_ok}
            />
            <Metric
              code="n_check_fail"
              label="核验失败"
              hint="未能产出报告的申请数，应为 0。"
              value={counts.n_check_fail}
            />
            <Metric
              code="total_pairs"
              label="核对项总数"
              hint="跨单据字段比对的总次数。"
              value={counts.total_pairs}
            />
            <Metric
              code="decisive_pairs"
              label="有效结论数"
              hint="给出一致或不一致、未被跳过的核对项。"
              value={counts.decisive_pairs}
            />
            <Metric
              code="true_positive"
              label="正确拦截"
              hint="标注为不一致，系统同样判定为不一致。"
              value={counts.true_positive}
            />
            <Metric
              code="true_negative"
              label="正确放行"
              hint="标注为一致，系统同样判定为一致。"
              value={counts.true_negative}
            />
            <Metric
              code="false_positive"
              label="误报次数"
              hint="标注为一致，系统判定为不一致。"
              value={counts.false_positive}
            />
            <Metric
              code="false_negative"
              label="漏报次数"
              hint="标注为不一致，系统判定为一致。应为 0。"
              value={counts.false_negative}
            />
            <Metric
              code="n_inconsistent_labeled_decisive"
              label="不一致样本（有效结论）"
              hint="标注为不一致且系统给出明确结论的项，用作漏报率分母。"
              value={counts.n_inconsistent_labeled_decisive}
            />
          </dl>
        </>
      )}
      {warnings.length > 0 && (
        <ul className="demo-eval-warnings" data-testid="demo-eval-warnings">
          {warnings.map((warning) => (
            <li key={warning}>{humanEvalWarning(warning)}</li>
          ))}
        </ul>
      )}
      <p className="demo-limitation" data-testid="demo-eval-note">
        {humanHonestyNote()}
      </p>
    </div>
  );
}

/** The T07 demo panels: a bounded synchronous batch check over
 * server-resident synthetic fixtures (native controls, server-owned cap,
 * explicit per-item and enclosing outcomes) plus the read-only fixed-main
 * evaluation summary with its server-owned C-DEV-REG / UNVERIFIED claim
 * labels.  The browser sends fixture ids only; it never derives or displays
 * formal PASS. */
export default function DemoBatchSummaryPanel() {
  const batch = useDemoBatchCheck();
  const summary = useDemoEvaluationSummary();
  const [uploads, setUploads] = useState<ExhibitUpload[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [batchResult, setBatchResult] =
    useState<DemoBatchCheckResponse | null>(null);

  useEffect(() => {
    setUploads(readExhibitUploads());
  }, []);

  const toggle = (uploadId: string) => {
    if (batch.isPending) return;
    setSelected((prev) =>
      prev.includes(uploadId)
        ? prev.filter((id) => id !== uploadId)
        : [...prev, uploadId],
    );
    setBatchResult(null);
  };

  const run = () => {
    if (selected.length === 0 || batch.isPending) return;
    const applications = uploads
      .filter((item) => selected.includes(item.id))
      .map((item) => item.application);
    if (applications.length === 0) return;
    setBatchResult(null);
    batch.mutate(
      { applications, fixture_ids: [] },
      { onSuccess: (data) => setBatchResult(data) },
    );
  };

  const batchMaxN = 50;

  let batchSection: ReactElement;
  if (uploads.length === 0) {
    batchSection = (
      <p data-testid="demo-batch-fixtures-empty">
        还没有已上传的申请。请先在上面核验一笔 JSON。
      </p>
    );
  } else {
    batchSection = (
      <>
        <fieldset className="demo-batch-controls">
          <legend>选择已上传的申请（每次最多 {batchMaxN} 条）</legend>
          {uploads.map((option) => (
            <label
              key={option.id}
              className="demo-batch-option"
              htmlFor={`demo-batch-fixture-${option.id}`}
            >
              <input
                id={`demo-batch-fixture-${option.id}`}
                type="checkbox"
                data-testid={`demo-batch-fixture-${option.id}`}
                checked={selected.includes(option.id)}
                disabled={batch.isPending}
                onChange={() => toggle(option.id)}
              />
              {option.fileName || option.id}
            </label>
          ))}
        </fieldset>
        <div className="demo-controls">
          <Button
            type="button"
            data-testid="demo-batch-run-button"
            disabled={selected.length === 0 || batch.isPending}
            onClick={run}
          >
            {batch.isPending ? "正在核验…" : "开始批量核验"}
          </Button>
          <span className="demo-limitation" data-testid="demo-batch-cap">
            服务端上限：{batchMaxN} 条
          </span>
        </div>
        <p
          className="demo-status"
          data-testid="demo-batch-status"
          role="status"
          aria-live="polite"
        >
          {batch.isPending
            ? "批量校验中…"
            : batch.isError
              ? "批量校验失败"
              : batchResult !== null
                ? "批量校验完成"
                : "等待批量校验"}
        </p>
        {batch.isError && (
          <p className="demo-error" data-testid="demo-batch-error" role="alert">
            {batch.error instanceof HttpError &&
            batch.error.errorCode === "DEMO_BATCH_TOO_LARGE"
              ? CAP_FAILURE_TEXT
              : BATCH_FAILURE_TEXT}
          </p>
        )}
        {batchResult !== null && (
          <div className="demo-batch-results" data-testid="demo-batch-results">
            <div className="demo-check-head">
              <span data-testid="demo-batch-outcome">
                结果：{OUTCOME_ZH[batchResult.outcome] ?? batchResult.outcome}
              </span>
              <span className="demo-limitation">
                完成 {batchResult.completed} · 失败 {batchResult.failed}
              </span>
            </div>
            <p data-testid="demo-batch-totals">
              一致 {batchResult.totals.consistent} · 不一致{" "}
              {batchResult.totals.inconsistent} · 存疑{" "}
              {batchResult.totals.uncertain} · 跳过 {batchResult.totals.skipped}
            </p>
            <ul className="demo-batch-items">
              {batchResult.results.map((item) => (
                <li
                  key={item.fixture_id}
                  data-testid={`demo-batch-item-${item.fixture_id}`}
                >
                  <div className="demo-check-head">
                    <span className="demo-rule-id">{item.fixture_id}</span>
                    <span
                      className="verdict"
                      data-testid="demo-batch-item-outcome"
                    >
                      {ITEM_OUTCOME_ZH[item.outcome] ?? item.outcome}
                    </span>
                  </div>
                  <BatchItem item={item} />
                </li>
              ))}
            </ul>
          </div>
        )}
      </>
    );
  }

  return (
    <>
      <section className="panel" data-testid="demo-batch-panel">
        <h2>一次核好几笔（可选）</h2>
        <p className="demo-limitation">
          对比刚才上传并核验过的申请。列表来自本页已上传的 JSON，不再使用内置演示样例。
        </p>
        {batchSection}
      </section>
      <section className="panel" data-testid="demo-eval-panel">
        <h2>核验指标（只读）</h2>
        <p className="demo-limitation">
          覆盖率、误报、漏报来自固定评测集。点下面的按钮读取官方数字，页面自己不算分。
        </p>
        <div className="demo-controls">
          <Button
            type="button"
            data-testid="demo-eval-load-button"
            disabled={summary.isFetching}
            onClick={() => summary.refetch()}
          >
            {summary.isFetching ? "正在读取…" : "查看覆盖率、误报与漏报"}
          </Button>
          <span className="demo-limitation">固定语料 suite=main · 只读</span>
        </div>
        <p
          className="demo-status"
          data-testid="demo-eval-status"
          role="status"
          aria-live="polite"
        >
          {summary.isFetching
            ? "加载中…"
            : summary.isError
              ? EVAL_FAILURE_TEXT
              : summary.data !== undefined
                ? "已加载"
                : "未加载"}
        </p>
        {summary.isError && (
          <p className="demo-error" data-testid="demo-eval-error" role="alert">
            {EVAL_FAILURE_TEXT}
          </p>
        )}
        {/* Cached metrics are never rendered while a reload is in flight or
            has failed: unavailable and loading stay distinct terminal/
            transient states, so no prior work appears current. */}
        {summary.data !== undefined &&
          !summary.isFetching &&
          !summary.isError && <DemoEvalSummary data={summary.data} />}
      </section>
    </>
  );
}
