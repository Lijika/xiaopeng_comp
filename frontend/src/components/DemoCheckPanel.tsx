import { useState } from "react";

import {
  useDemoCheck,
  useDemoFixtures,
  type DemoCheckResponse,
} from "../api/hooks";

const VERDICT_ZH: Record<string, string> = {
  consistent: "一致",
  inconsistent: "不一致",
  uncertain: "存疑",
  skipped: "跳过",
};

const SEVERITY_ZH: Record<string, string> = {
  critical: "严重",
  major: "主要",
  minor: "次要",
  info: "提示",
};

/** The failure state renders one fixed generic message only: the server
 * error body is never copied into the UI, so no caller or internal detail
 * (paths, identifiers, exception text) can surface to the user. */
const CHECK_FAILURE_TEXT = "校验失败，请稍后重试";

/** The closed demo report renderer.  It renders only the server DTO and
 * never reproduces matching, grading, or expected-verdict comparison. */
function DemoReport({ report }: { report: DemoCheckResponse }) {
  return (
    <div className="demo-report" data-testid="demo-report">
      <div className="app-header">
        <h2>校验报告</h2>
        <span className="boundary track" data-testid="demo-report-track">
          {report.track}
        </span>
        <span className="boundary" data-testid="demo-report-scope">
          {report.data_scope}
        </span>
      </div>
      <p data-testid="demo-summary">
        一致 {report.summary.consistent} · 不一致 {report.summary.inconsistent} ·
        存疑 {report.summary.uncertain} · 跳过 {report.summary.skipped}
      </p>
      <p data-testid="demo-config-version">
        规则包版本：{report.config.rule_config_version ?? "未知"}
      </p>
      <ul className="demo-checks">
        {report.checks.map((check) => (
          <li
            key={check.rule_id}
            data-testid={`demo-check-item-${check.rule_id}`}
          >
            <div className="demo-check-head">
              <span className="demo-rule-id">{check.rule_id}</span>
              <span className="verdict">{VERDICT_ZH[check.verdict] ?? check.verdict}</span>
              <span className="severity">{SEVERITY_ZH[check.severity] ?? check.severity}</span>
            </div>
            <p className="demo-check-message">{check.message}</p>
            {(check.snapshots ?? []).length > 0 && (
              <table className="demo-snapshots">
                <thead>
                  <tr>
                    <th>单据</th>
                    <th>字段</th>
                    <th>原文</th>
                    <th>标准化</th>
                  </tr>
                </thead>
                <tbody>
                  {check.snapshots?.map((snap, index) => (
                    <tr
                      key={`${snap.doc_id}-${snap.field}-${index}`}
                      data-testid={`demo-snapshot-${check.rule_id}`}
                    >
                      <td>{snap.doc_type}</td>
                      <td>{snap.field}</td>
                      <td>{snap.raw ?? "—"}</td>
                      <td>{snap.normalized ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {check.diff_highlight !== null && check.diff_highlight !== undefined && (
              <p
                className="demo-diff"
                data-testid={`demo-diff-${check.rule_id}`}
              >
                差异：{check.diff_highlight.left ?? "—"} →{" "}
                {check.diff_highlight.right ?? "—"}
                {check.diff_highlight.detail
                  ? `（${check.diff_highlight.detail}）`
                  : ""}
              </p>
            )}
          </li>
        ))}
      </ul>
      <div className="demo-evidence">
        <h3>证据元数据</h3>
        {report.evidence_links.map((link) => (
          <p key={link.href} className="demo-evidence-link">
            <a href={link.href} data-testid="demo-evidence-link">
              {link.label}
            </a>
            <span className="demo-limitation"> {link.limitation}</span>
          </p>
        ))}
      </div>
    </div>
  );
}

/** The T06 demo check panel: server-owned fixture selection, one explicit
 * click-driven mutation, and the closed report.  The browser never sends a
 * filename, path, application, rules, knowledge, or label value. */
export default function DemoCheckPanel() {
  const fixtures = useDemoFixtures();
  const check = useDemoCheck();
  const [fixtureId, setFixtureId] = useState("");
  const [report, setReport] = useState<DemoCheckResponse | null>(null);

  const run = () => {
    if (fixtureId === "" || check.isPending) return;
    setReport(null);
    check.mutate(fixtureId, {
      onSuccess: (data) => setReport(data),
    });
  };

  // Selecting a different fixture resets the previous report; presentation
  // state never survives a selection change.
  const select = (next: string) => {
    setFixtureId(next);
    setReport(null);
    check.reset();
  };

  if (fixtures.isLoading) {
    return (
      <section className="panel" data-testid="demo-panel">
        <p data-testid="demo-fixtures-loading" role="status">
          正在加载演示样例…
        </p>
      </section>
    );
  }
  if (fixtures.isError) {
    return (
      <section className="panel" data-testid="demo-panel">
        <p data-testid="demo-fixtures-error" role="alert">
          演示样例列表不可用
        </p>
      </section>
    );
  }
  const options = fixtures.data?.fixtures ?? [];
  if (options.length === 0) {
    return (
      <section className="panel" data-testid="demo-panel">
        <p data-testid="demo-fixtures-empty">暂无可用的演示样例</p>
      </section>
    );
  }

  return (
    <section className="panel" data-testid="demo-panel">
      <div className="demo-controls">
        <label htmlFor="demo-fixture-select">演示样例</label>
        <select
          id="demo-fixture-select"
          data-testid="demo-fixture-select"
          value={fixtureId}
          onChange={(event) => select(event.target.value)}
        >
          <option value="">请选择…</option>
          {options.map((option) => (
            <option key={option.fixture_id} value={option.fixture_id}>
              {option.title}
            </option>
          ))}
        </select>
        <button
          type="button"
          data-testid="demo-run-button"
          disabled={fixtureId === "" || check.isPending}
          onClick={run}
        >
          运行校验
        </button>
      </div>
      <p
        className="demo-status"
        data-testid="demo-check-status"
        role="status"
        aria-live="polite"
      >
        {check.isPending ? "校验中…" : report !== null ? "校验完成" : "等待运行"}
      </p>
      {check.isError && (
        <p className="demo-error" data-testid="demo-check-error" role="alert">
          {CHECK_FAILURE_TEXT}
        </p>
      )}
      {report !== null && <DemoReport report={report} />}
    </section>
  );
}
