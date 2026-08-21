import { useState } from "react";

import {
  HttpError,
  type S12BundleResponse,
} from "../api/client";
import {
  S12_TERMINAL_JOB_STATUSES,
  useS12Bundle,
  useS12Job,
  useS12JobPoll,
  useS12Plans,
  useS12StartProcess,
} from "../api/hooks";

/** The closed S12 error-state mapping: the exact registered envelope code
 * renders beside one stable label.  Authorization-denial content carries no
 * plan, job or bundle identifier; the unavailable/not-found states keep
 * prior server identifiers only as stale context. */
function s12ErrorState(error: Error): {
  code: string;
  label: string;
  testId: string;
} | null {
  if (!(error instanceof HttpError)) return null;
  const code = error.errorCode ?? `S12_HTTP_${error.status}`;
  if (error.status === 403) {
    return { code, label: "Authorization denied", testId: "s12-error-forbidden" };
  }
  if (error.status === 404) {
    return { code, label: "Not found", testId: "s12-error-not-found" };
  }
  if (error.status === 422) {
    return { code, label: "Invalid command", testId: "s12-error-invalid" };
  }
  if (error.status === 503) {
    return { code, label: "Unavailable", testId: "s12-error-unavailable" };
  }
  return { code, label: "Request failed", testId: "s12-error-unavailable" };
}

function S12ErrorState({ error }: { error: Error }) {
  const state = s12ErrorState(error);
  if (state === null) {
    return (
      <p role="status" aria-live="polite" data-testid="s12-unknown-outcome">
        结果未知：网络未确认，请重新加载权威状态
      </p>
    );
  }
  return (
    <section className="panel" data-testid={state.testId} role="alert">
      <p>{state.label}</p>
      <p data-testid="s12-error-code">{state.code}</p>
    </section>
  );
}

/** One leaf value rendered verbatim; no formatting, translation or client
 * computation is applied to any server value. */
function SealedLeaf({ value }: { value: unknown }) {
  return (
    <span className="break-all" data-testid="s12-value">
      {value === null || value === undefined ? "—" : String(value)}
    </span>
  );
}

function SealedValue({ value }: { value: unknown }) {
  if (value === null || value === undefined) {
    return <SealedLeaf value="—" />;
  }
  if (Array.isArray(value)) {
    if (value.length === 0) {
      return <SealedLeaf value="[]" />;
    }
    return (
      <ol className="history-list max-h-64 overflow-y-auto" data-testid="s12-report-list">
        {value.map((item, index) => (
          <li key={index}>
            {item !== null && typeof item === "object" ? (
              <div className="panel-inline">
                <SealedValue value={item} />
              </div>
            ) : (
              <SealedLeaf value={item} />
            )}
          </li>
        ))}
      </ol>
    );
  }
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    return (
      <dl className="facts max-h-96 overflow-y-auto">
        {entries.map(([key, nested]) => (
          <div key={key}>
            <dt>{key}</dt>
            <dd>
              {nested !== null && typeof nested === "object" ? (
                <SealedValue value={nested} />
              ) : (
                <SealedLeaf value={nested} />
              )}
            </dd>
          </div>
        ))}
      </dl>
    );
  }
  return <SealedLeaf value={value} />;
}

const REPORT_EXCLUDED_KEYS = new Set(["replay_package"]);

/** The complete sealed report: every top-level field of the server-provided
 * bundle renders exactly once in server (DTO insertion) order, with values
 * verbatim.  ``replay_package`` renders as its digest pair only -- the
 * browser reads it as lineage identity, never as a re-executed payload.
 * No metric calculation, digest derivation, denominator filtering or status
 * inference happens here. */
function S12SealedReport({ bundle }: { bundle: S12BundleResponse }) {
  return (
    <section
      className="panel"
      data-testid="s12-sealed-report"
      aria-labelledby="s12-sealed-title"
    >
      <h3 id="s12-sealed-title">评价封存报告（服务端权威，逐字段原样）</h3>
      <p className="text-sm text-muted-foreground">
        封存包不可变且按内容寻址；浏览器只读展示，不做任何计算或改写。
      </p>
      {Object.entries(bundle)
        .filter(([key]) => !REPORT_EXCLUDED_KEYS.has(key))
        .map(([key, value]) => (
          <section key={key} data-testid="s12-report-section">
            <h4 data-testid="s12-report-section-name">{key}</h4>
            <SealedValue value={value} />
          </section>
        ))}
      <section data-testid="s12-report-section">
        <h4 data-testid="s12-report-section-name">replay_package_digest</h4>
        <SealedLeaf value={bundle.replay_package_digest} />
      </section>
    </section>
  );
}

/**
 * The T14 Evaluation Operator surface mounted only for ``/controlled/s12``
 * (and its alias).  Sequence: read the frozen-plan catalog, start exactly
 * one job per explicit action (start body carries only ``plan_id``), invoke
 * the released process trigger once for that original job id, poll the job
 * at one GET per second for at most 120 cycles, and on a terminal job with
 * a bundle id read the immutable sealed bundle once for rendering.  Every
 * state is explicit; cancel/rerun/plan-freeze/business commands do not
 * exist here.
 */
export default function S12EvaluationOperator() {
  const plans = useS12Plans();
  const [selectedPlanId, setSelectedPlanId] = useState("");
  const start = useS12StartProcess();
  const [jobId, setJobId] = useState<string | null>(null);
  const job = useS12Job(jobId);
  const poll = useS12JobPoll(jobId, jobId !== null);
  const terminal =
    job.data !== undefined && S12_TERMINAL_JOB_STATUSES.includes(job.data.status);
  const bundleId = terminal ? job.data?.result?.bundle_id ?? null : null;
  const bundle = useS12Bundle(bundleId);

  const handleStart = () => {
    if (selectedPlanId === "" || start.isPending || jobId !== null) return;
    start.mutate(
      { plan_id: selectedPlanId },
      {
        onSuccess: ({ job: started }) => setJobId(started.job_id),
      },
    );
  };

  if (plans.isPending) {
    return (
      <section className="panel" data-testid="s12-operator">
        <p data-testid="s12-catalog-loading" role="status">
          正在加载冻结计划目录…
        </p>
      </section>
    );
  }

  if (plans.isError) {
    return (
      <section className="panel" data-testid="s12-operator">
        <S12ErrorState error={plans.error} />
      </section>
    );
  }

  const catalog = plans.data.plans;
  if (catalog.length === 0) {
    return (
      <section className="panel" data-testid="s12-operator">
        <p data-testid="s12-catalog-empty">暂无已冻结的评价计划</p>
      </section>
    );
  }

  const startError = start.error ?? null;

  return (
    <section className="panel" data-testid="s12-operator" aria-labelledby="s12-operator-title">
      <h2 id="s12-operator-title">评价操作台（服务端权威）</h2>

      <div className="demo-controls">
        <label htmlFor="s12-plan-select">已冻结评价计划</label>
        <select
          id="s12-plan-select"
          data-testid="s12-plan-select"
          value={selectedPlanId}
          onChange={(event) => setSelectedPlanId(event.target.value)}
          disabled={start.isPending || jobId !== null}
        >
          <option value="">请选择…</option>
          {catalog.map((plan) => (
            <option key={plan.plan_id} value={plan.plan_id}>
              {`${plan.plan_id} · ${plan.scope} · ${plan.opportunity_count} opportunities`}
            </option>
          ))}
        </select>
        <button
          type="button"
          data-testid="s12-start-button"
          disabled={
            selectedPlanId === "" || start.isPending || jobId !== null
          }
          onClick={handleStart}
        >
          {start.isPending ? "执行中…" : "启动一次评价"}
        </button>
      </div>

      {selectedPlanId !== "" &&
        (() => {
          const selected = catalog.find((plan) => plan.plan_id === selectedPlanId);
          if (selected === undefined) return null;
          return (
            <dl className="facts" data-testid="s12-selected-plan">
              <div>
                <dt>计划标识</dt>
                <dd data-testid="s12-selected-plan-id">{selected.plan_id}</dd>
              </div>
              <div>
                <dt>计划摘要</dt>
                <dd data-testid="s12-selected-plan-digest" className="break-all">
                  {selected.plan_digest}
                </dd>
              </div>
              <div>
                <dt>范围</dt>
                <dd data-testid="s12-selected-plan-scope">{selected.scope}</dd>
              </div>
            </dl>
          );
        })()}

      {jobId === null && startError !== null && (
        <S12ErrorState error={startError} />
      )}

      {jobId !== null && (
        <>
          <p
            role="status"
            aria-live="polite"
            data-testid="s12-job-live"
          >
            {job.data === undefined
              ? "任务查询中…"
              : `任务 ${jobId} · 状态 ${job.data.status}`}
          </p>
          {job.data !== undefined && (
            <dl className="facts" data-testid="s12-job-facts">
              <div>
                <dt>任务状态</dt>
                <dd data-testid="s12-job-status">{job.data.status}</dd>
              </div>
              <div>
                <dt>围栏</dt>
                <dd data-testid="s12-job-fence">{job.data.fence}</dd>
              </div>
              <div>
                <dt>尝试序号</dt>
                <dd data-testid="s12-job-attempt">{job.data.attempt_no}</dd>
              </div>
              <div>
                <dt>租约到期（epoch）</dt>
                <dd data-testid="s12-job-lease">
                  {job.data.lease_until ?? "—"}
                </dd>
              </div>
              <div>
                <dt>工作身份</dt>
                <dd data-testid="s12-job-worker">{job.data.worker_id}</dd>
              </div>
              <div>
                <dt>结果状态（服务端原样）</dt>
                <dd data-testid="s12-result-status">
                  {job.data.result?.status ?? "—"}
                </dd>
              </div>
              <div>
                <dt>结果原因码</dt>
                <dd data-testid="s12-result-reasons">
                  {(job.data.result?.reason_codes ?? []).join("，") || "—"}
                </dd>
              </div>
            </dl>
          )}
          {poll === "waiting" && (
            <p data-testid="s12-polling" role="status">
              每秒轮询一次服务端任务状态（至多 120 次）…
            </p>
          )}
          {poll === "timed_out" && (
            <section className="panel" data-testid="s12-poll-bounded" role="alert">
              <p>
                已达到轮询上限（120 秒）：任务仍在服务端进行，结果未知。
                本页面不会创建新任务或再次触发执行；原任务编号：
                <span className="break-all">{jobId}</span>
              </p>
            </section>
          )}
          {terminal && job.data?.result?.bundle_id == null && (
            <p className="text-sm text-muted-foreground" data-testid="s12-job-terminal-note">
              任务已结束：没有可读取的封存报告（无 bundle_id）
            </p>
          )}
          {bundle.error !== undefined && bundle.error !== null && (
            <>
              <S12ErrorState error={bundle.error} />
              <p className="text-sm text-muted-foreground">
                任务上下文保留：<span className="break-all">{jobId}</span>
              </p>
            </>
          )}
          {bundle.data !== undefined && <S12SealedReport bundle={bundle.data} />}
        </>
      )}
    </section>
  );
}
