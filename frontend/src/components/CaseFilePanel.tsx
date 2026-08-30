import {
  documentTypeLabel,
  fieldLabel,
  ruleTitle,
  verdictLabel,
} from "../lib/demoCopy";
import { useExhibitCase } from "../api/hooks";

export default function CaseFilePanel({
  title = "本笔申请（承接第 1 步核验）",
}: {
  title?: string;
}) {
  const { data } = useExhibitCase();
  if (!data?.application_id) {
    return (
      <section className="panel" data-testid="case-file-empty">
        <h2>{title}</h2>
        <p className="demo-limitation">
          还没有从第 1 步带过来的申请。请先上传 JSON 并完成核验，再进入后续步骤。
        </p>
      </section>
    );
  }
  const report = data.report as
    | {
        consistent?: number;
        inconsistent?: number;
        uncertain?: number;
      }
    | undefined;
  const checks = data.checks ?? [];
  const issues = checks.filter(
    (check) => check.verdict === "inconsistent" || check.verdict === "uncertain",
  );
  const documents = (data.documents ?? []) as Array<{
    doc_id?: string;
    doc_type?: string;
    fields?: Array<{ field?: string; raw?: string }>;
  }>;
  return (
    <section className="panel" data-testid="case-file-panel">
      <h2>{title}</h2>
      <p data-testid="case-file-id">
        申请编号：{data.application_id}
        {data.file_name ? ` · 文件 ${data.file_name}` : ""}
      </p>
      {report !== undefined && (
        <p data-testid="case-file-summary">
          核验结果：一致 {report.consistent ?? 0} · 不一致 {report.inconsistent ?? 0} ·
          存疑 {report.uncertain ?? 0}
          {issues.length === 0
            ? "。本笔没有需要人工处理的差异。"
            : "。下面是需要人看的差异。"}
        </p>
      )}
      {issues.length > 0 && (
        <ul className="demo-checks" data-testid="case-file-issues">
          {issues.map((check) => (
            <li key={check.rule_id}>
              <div className="demo-check-head">
                <span className="demo-rule-id">
                  {ruleTitle(check.rule_id, check.name)}
                </span>
                <span className={`verdict verdict-${check.verdict}`}>
                  {verdictLabel(check.verdict)}
                </span>
              </div>
              <p className="demo-check-message">{check.message}</p>
            </li>
          ))}
        </ul>
      )}
      <h3>本笔单据字段</h3>
      <div className="case-docs" data-testid="case-file-docs">
        {documents.map((doc) => (
          <section key={doc.doc_id || doc.doc_type} className="case-doc">
            <h4>{documentTypeLabel(String(doc.doc_type ?? "")) || doc.doc_id}</h4>
            <dl className="facts">
              {(doc.fields ?? []).map((field) => (
                <div key={field.field}>
                  <dt>{fieldLabel(String(field.field ?? ""))}</dt>
                  <dd>{field.raw || "—"}</dd>
                </div>
              ))}
            </dl>
          </section>
        ))}
      </div>
    </section>
  );
}
