import { useState, type ReactElement } from "react";

import { HttpError } from "../api/client";
import {
  useDemoBatchCheck,
  useDemoEvaluationSummary,
  useDemoFixtures,
  type DemoBatchCheckResponse,
  type DemoBatchItem,
  type DemoEvaluationSummaryResponse,
} from "../api/hooks";

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
              {issue.rule_id} · {VERDICT_ZH[issue.verdict] ?? issue.verdict} ·{" "}
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
function DemoEvalSummary({ data }: { data: DemoEvaluationSummaryResponse }) {
  const counts = data.counts ?? null;
  const rates = data.rates ?? null;
  const warnings = data.warnings ?? [];
  return (
    <div className="demo-eval">
      <div className="demo-check-head">
        <span className="boundary" data-testid="demo-eval-claim">
          {data.claim}
        </span>
        <span className="boundary" data-testid="demo-eval-gap">
          {data.performance_gap}
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
          <dl className="facts" data-testid="demo-eval-counts">
            <div>
              <dt>n_apps_loaded</dt>
              <dd>{counts.n_apps_loaded}</dd>
            </div>
            <div>
              <dt>n_check_ok</dt>
              <dd>{counts.n_check_ok}</dd>
            </div>
            <div>
              <dt>n_check_fail</dt>
              <dd>{counts.n_check_fail}</dd>
            </div>
            <div>
              <dt>total_pairs</dt>
              <dd>{counts.total_pairs}</dd>
            </div>
            <div>
              <dt>decisive_pairs</dt>
              <dd>{counts.decisive_pairs}</dd>
            </div>
            <div>
              <dt>true_positive</dt>
              <dd>{counts.true_positive}</dd>
            </div>
            <div>
              <dt>true_negative</dt>
              <dd>{counts.true_negative}</dd>
            </div>
            <div>
              <dt>false_positive</dt>
              <dd>{counts.false_positive}</dd>
            </div>
            <div>
              <dt>false_negative</dt>
              <dd>{counts.false_negative}</dd>
            </div>
            <div>
              <dt>n_inconsistent_labeled_decisive</dt>
              <dd>{counts.n_inconsistent_labeled_decisive}</dd>
            </div>
          </dl>
          <dl className="facts" data-testid="demo-eval-rates">
            <div>
              <dt>coverage</dt>
              <dd>{rates.coverage}</dd>
            </div>
            <div>
              <dt>false_positive_rate</dt>
              <dd>{rates.false_positive_rate}</dd>
            </div>
            <div>
              <dt>false_negative_rate</dt>
              <dd>{rates.false_negative_rate}</dd>
            </div>
            <div>
              <dt>accuracy</dt>
              <dd>{rates.accuracy}</dd>
            </div>
            <div>
              <dt>miss_rate</dt>
              <dd>{rates.miss_rate}</dd>
            </div>
            <div>
              <dt>uncertain_rate</dt>
              <dd>{rates.uncertain_rate}</dd>
            </div>
          </dl>
        </>
      )}
      {warnings.length > 0 && (
        <ul className="demo-eval-warnings" data-testid="demo-eval-warnings">
          {warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      )}
      <p className="demo-limitation" data-testid="demo-eval-note">
        {data.honesty_note}
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
  const fixtures = useDemoFixtures();
  const batch = useDemoBatchCheck();
  const summary = useDemoEvaluationSummary();
  const [selected, setSelected] = useState<string[]>([]);
  const [batchResult, setBatchResult] =
    useState<DemoBatchCheckResponse | null>(null);

  const toggle = (fixtureId: string) => {
    // A live synchronous mutation owns one request until a terminal result:
    // selection can never change (or reset the mutation) while it is
    // pending, so no overlapping POST or callback loss is possible.
    if (batch.isPending) return;
    setSelected((prev) =>
      prev.includes(fixtureId)
        ? prev.filter((id) => id !== fixtureId)
        : [...prev, fixtureId],
    );
    // A selection change only clears the previous run's presentation; the
    // mutation state stays terminal until the explicit next run.
    setBatchResult(null);
  };

  const run = () => {
    if (selected.length === 0 || batch.isPending) return;
    setBatchResult(null);
    batch.mutate(
      { fixture_ids: selected },
      { onSuccess: (data) => setBatchResult(data) },
    );
  };

  const batchMaxN = fixtures.data?.batch_max_n;

  let batchSection: ReactElement;
  if (fixtures.isLoading) {
    batchSection = (
      <p data-testid="demo-batch-fixtures-loading" role="status">
        正在加载演示样例…
      </p>
    );
  } else if (fixtures.isError) {
    batchSection = (
      <p className="demo-error" data-testid="demo-batch-fixtures-error" role="alert">
        演示样例列表不可用
      </p>
    );
  } else if ((fixtures.data?.fixtures ?? []).length === 0) {
    batchSection = (
      <p data-testid="demo-batch-fixtures-empty">暂无可用的演示样例</p>
    );
  } else {
    const options = fixtures.data?.fixtures ?? [];
    batchSection = (
      <>
        <fieldset className="demo-batch-controls">
          <legend>选择演示样例（每次校验最多 {batchMaxN ?? "?"} 条）</legend>
          {options.map((option) => (
            <label
              key={option.fixture_id}
              className="demo-batch-option"
              htmlFor={`demo-batch-fixture-${option.fixture_id}`}
            >
              <input
                id={`demo-batch-fixture-${option.fixture_id}`}
                type="checkbox"
                data-testid={`demo-batch-fixture-${option.fixture_id}`}
                checked={selected.includes(option.fixture_id)}
                disabled={batch.isPending}
                onChange={() => toggle(option.fixture_id)}
              />
              {option.title}
            </label>
          ))}
        </fieldset>
        <div className="demo-controls">
          <button
            type="button"
            data-testid="demo-batch-run-button"
            disabled={selected.length === 0 || batch.isPending}
            onClick={run}
          >
            运行批量校验
          </button>
          <span className="demo-limitation" data-testid="demo-batch-cap">
            {batchMaxN !== undefined ? `服务端上限：${batchMaxN} 条` : ""}
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
        <h2>批量校验（同步·受上限约束）</h2>
        {batchSection}
      </section>
      <section className="panel" data-testid="demo-eval-panel">
        <h2>评估摘要（只读）</h2>
        <div className="demo-controls">
          <button
            type="button"
            data-testid="demo-eval-load-button"
            disabled={summary.isFetching}
            onClick={() => summary.refetch()}
          >
            加载摘要
          </button>
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
