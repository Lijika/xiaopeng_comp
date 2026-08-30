import { useRef, useState } from "react";

import {
  useMutation,
  type UseMutationResult,
} from "@tanstack/react-query";

import { request } from "../api/client";
import { type DemoCheckResponse } from "../api/hooks";
import { ruleTitle } from "../lib/demoCopy";
import {
  documentsFromApplication,
  readExhibitCurrent,
  readExhibitUploads,
  upsertExhibitUpload,
  writeExhibitCurrent,
  writeExhibitUploads,
} from "../lib/exhibitSession";
import { Button } from "./ui/button";

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

const CHECK_FAILURE_TEXT = "校验失败，请稍后重试";
const INVALID_JSON_TEXT = "请上传任务4申请 JSON（需包含 documents）";

function useUploadedCheck(): UseMutationResult<
  DemoCheckResponse,
  Error,
  { application: Record<string, unknown>; fileName: string }
> {
  return useMutation({
    mutationFn: ({
      application,
    }: {
      application: Record<string, unknown>;
      fileName: string;
    }) =>
      request<DemoCheckResponse>("/api/demo/check", {
        method: "POST",
        body: JSON.stringify({ application }),
      }),
    retry: false,
  });
}

function DemoReport({ report }: { report: DemoCheckResponse }) {
  return (
    <div className="demo-report" data-testid="demo-report">
      <div className="app-header">
        <h2>校验报告</h2>
      </div>
      <span className="sr-only" data-testid="demo-report-track">
        {report.track}
      </span>
      <span className="sr-only" data-testid="demo-report-scope">
        {report.data_scope}
      </span>
      <p data-testid="demo-summary">
        一致 {report.summary.consistent} 条 · 不一致 {report.summary.inconsistent}{" "}
        条 · 存疑 {report.summary.uncertain} 条 · 跳过 {report.summary.skipped} 条
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
              <span className="demo-rule-id">
                {ruleTitle(check.rule_id, check.name)}
              </span>
              <span className={`verdict verdict-${check.verdict}`}>
                {VERDICT_ZH[check.verdict] ?? check.verdict}
              </span>
              <span className="severity">
                {SEVERITY_ZH[check.severity] ?? check.severity}
              </span>
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
            {check.diff_highlight !== null &&
              check.diff_highlight !== undefined && (
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
      {(report.evidence_links ?? []).length > 0 && (
        <div className="demo-evidence">
          <h3>对照材料</h3>
          {(report.evidence_links ?? []).map((link) => (
            <p key={link.href} className="demo-evidence-link">
              <a href={link.href} data-testid="demo-evidence-link">
                {link.label}
              </a>
              <span className="demo-limitation"> {link.limitation}</span>
            </p>
          ))}
        </div>
      )}
    </div>
  );
}

export default function DemoCheckPanel() {
  const check = useUploadedCheck();
  const fileRef = useRef<HTMLInputElement>(null);
  const saved = readExhibitCurrent();
  const [fileName, setFileName] = useState(saved?.fileName ?? "");
  const [application, setApplication] = useState<Record<string, unknown> | null>(
    saved?.application ?? null,
  );
  const [parseError, setParseError] = useState<string | null>(null);
  const [report, setReport] = useState<DemoCheckResponse | null>(
    saved?.fullReport ? (saved.fullReport as DemoCheckResponse) : null,
  );

  const onFile = async (file: File | undefined) => {
    setReport(null);
    check.reset();
    if (file === undefined) {
      setFileName("");
      setApplication(null);
      setParseError(null);
      return;
    }
    setFileName(file.name);
    try {
      const text = await file.text();
      const parsed: unknown = JSON.parse(text);
      if (
        parsed === null ||
        typeof parsed !== "object" ||
        Array.isArray(parsed) ||
        !Array.isArray((parsed as { documents?: unknown }).documents)
      ) {
        setApplication(null);
        setParseError(INVALID_JSON_TEXT);
        return;
      }
      setApplication(parsed as Record<string, unknown>);
      setParseError(null);
    } catch {
      setApplication(null);
      setParseError(INVALID_JSON_TEXT);
    }
  };

  const run = () => {
    if (application === null || check.isPending) return;
    setReport(null);
    check.mutate(
      { application, fileName },
      {
        onSuccess: (data) => {
        setReport(data);
        const id =
          typeof application.application_id === "string" &&
          application.application_id !== ""
            ? application.application_id
            : data.application_id;
        const record = {
          id,
          fileName,
          application,
          documents: documentsFromApplication(application),
          report: {
            application_id: data.application_id,
            consistent: data.summary.consistent,
            inconsistent: data.summary.inconsistent,
            uncertain: data.summary.uncertain,
            skipped: data.summary.skipped,
          },
          checks: data.checks.map((item) => ({
            rule_id: item.rule_id,
            name: item.name,
            verdict: item.verdict,
            severity: item.severity,
            message: item.message,
            snapshots: (item.snapshots ?? []).map((snap) => ({
              doc_type: snap.doc_type,
              field: snap.field,
              raw: snap.raw ?? null,
              normalized: snap.normalized ?? null,
            })),
            diff_left: item.diff_highlight?.left ?? null,
            diff_right: item.diff_highlight?.right ?? null,
            diff_detail: item.diff_highlight?.detail ?? null,
          })),
          fullReport: data as unknown as Record<string, unknown>,
        };
        writeExhibitCurrent(record);
        writeExhibitUploads(upsertExhibitUpload(readExhibitUploads(), record));
      },
    });
  };

  return (
    <section className="panel" data-testid="demo-panel">
      <h2>上传申请 JSON，看跨单据是否对得上</h2>
      <p className="demo-limitation">
        只接收任务4申请 JSON（含 documents）。展会文件在
        材料/task4_applications/：登记证字段来自抽取结果，保单/合同/发票/身份证按同一辆车补齐。不要上传图片。
      </p>
      <div className="demo-controls">
        <input
          id="demo-application-file"
          ref={fileRef}
          type="file"
          accept="application/json,.json"
          data-testid="demo-application-file"
          className="sr-only"
          onChange={(event) => {
            void onFile(event.target.files?.[0]);
          }}
        />
        <Button
          type="button"
          variant="outline"
          onClick={() => fileRef.current?.click()}
        >
          {fileName ? `已选 ${fileName}` : "选择申请 JSON"}
        </Button>
        <Button
          type="button"
          data-testid="demo-run-button"
          disabled={application === null || check.isPending}
          onClick={run}
        >
          {check.isPending ? "正在核验…" : "开始核验"}
        </Button>
      </div>
      <p
        className="demo-status"
        data-testid="demo-check-status"
        role="status"
        aria-live="polite"
      >
        {check.isPending
          ? "校验中…"
          : report !== null
            ? "校验完成"
            : fileName !== ""
              ? `已选择 ${fileName}`
              : "等待上传"}
      </p>
      {parseError !== null && (
        <p className="demo-error" data-testid="demo-parse-error" role="alert">
          {parseError}
        </p>
      )}
      {check.isError && (
        <p className="demo-error" data-testid="demo-check-error" role="alert">
          {CHECK_FAILURE_TEXT}
        </p>
      )}
      {report !== null && <DemoReport report={report} />}
    </section>
  );
}
