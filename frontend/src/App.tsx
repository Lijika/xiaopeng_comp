import { useCallback, useState } from "react";
import { useExhibitCase } from "./api/hooks";
import ApprovalDecisionPanel from "./components/ApprovalDecisionPanel";
import {
  CancelApplicationPanel,
  DeletionStatusPanel,
  DeliveryStatusPanel,
  ExportStatusPanel,
  SettlementStatusPanel,
} from "./components/AfterApprovalPanels";
import CaseFilePanel from "./components/CaseFilePanel";
import DemoCheckPanel from "./components/DemoCheckPanel";
import PolicyReleasePanel, {
  GovernanceWorkspacePanel,
} from "./components/PolicyReleasePanel";
import ReviewDecisionPanel from "./components/ReviewDecisionPanel";
import StepPager from "./components/StepPager";
import SupplementUploadPanel from "./components/SupplementUploadPanel";
import S12EvaluationOperator from "./components/S12EvaluationOperator";

/** Left rail workspaces. Top bar only shows the current workspace's pages. */
const FLOW_GROUPS = [
  {
    id: "check",
    title: "核验",
    short: "核验",
    hint: "同一申请：核验 → 人工复核 → 补材料 → 批准",
    steps: [
      {
        n: "1",
        label: "单笔核验",
        href: "/",
        desc: "上传申请 JSON，立刻看到一致 / 不一致 / 存疑",
      },
      {
        n: "2",
        label: "人工复核",
        href: "/controlled/s01",
        desc: "对系统标红或存疑的申请做人工判断",
      },
      {
        n: "3",
        label: "补材料",
        href: "/controlled/s02",
        desc: "缺页、串页、拍糊了，补一版附件再核",
      },
      {
        n: "4",
        label: "批准",
        href: "/controlled/s05/react",
        desc: "批准、特批或拒绝本笔申请",
      },
    ],
  },
  {
    id: "after",
    title: "批准之后 · 放款后治理",
    short: "治理",
    hint: "批准完成后的投递、取消、删除与导出",
    steps: [
      {
        n: "5",
        label: "结果投递",
        href: "/controlled/s13",
        desc: "把核验结论交给下游系统",
      },
      {
        n: "6",
        label: "取消申请",
        href: "/controlled/s14",
        desc: "中途取消一笔尚未放款的申请",
      },
      {
        n: "7",
        label: "终止清算",
        href: "/controlled/s14/settlement",
        desc: "已终止申请的收尾与通知",
      },
      {
        n: "8",
        label: "删除数据",
        href: "/controlled/s16",
        desc: "按合规要求删除这份申请的副本",
      },
      {
        n: "9",
        label: "导出报告",
        href: "/controlled/s17",
        desc: "经独立授权后导出脱敏结果",
      },
    ],
  },
  {
    id: "rules",
    title: "规则",
    short: "规则",
    hint: "发布规则、看影响、对照覆盖率误报漏报",
    steps: [
      {
        n: "R1",
        label: "发布规则",
        href: "/controlled/s08/react",
        desc: "把校验规则做成可发布的版本",
      },
      {
        n: "R2",
        label: "规则影响",
        href: "/controlled/s09/react",
        desc: "改规则前，先看会影响哪些在途申请",
      },
      {
        n: "R3",
        label: "成绩看板",
        href: "/controlled/s12",
        desc: "覆盖率、误报、漏报",
      },
    ],
  },
] as const;

function isFlowLinkActive(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/" || pathname === "/demo/react";
  return pathname === href || pathname === `${href}/react`;
}

function PageGuide({
  step,
  title,
  what,
  how,
}: {
  step: string;
  audience?: string;
  title: string;
  what: string;
  how: string;
}) {
  return (
    <aside className="page-guide" data-testid="page-guide">
      <p className="page-guide-kicker">
        <span className="page-guide-step">第 {step} 步</span>
        <span className="page-guide-audience">审核员</span>
      </p>
      <p className="page-guide-title">{title}</p>
      <p className="page-guide-what">{what}</p>
      <p className="page-guide-how">{how}</p>
    </aside>
  );
}

function workspaceForPath(pathname: string) {
  return (
    FLOW_GROUPS.find((group) =>
      group.steps.some((step) => isFlowLinkActive(pathname, step.href)),
    ) ?? FLOW_GROUPS[0]
  );
}

function FlowNavigation() {
  const pathname = window.location.pathname;
  const currentGroup = workspaceForPath(pathname);
  return (
    <div className="app-shell" data-testid="app-shell">
      <aside className="workspace-rail" aria-label="工作区">
        <p className="workspace-rail-brand">放款审核</p>
        {FLOW_GROUPS.map((group) => {
          const active = group.id === currentGroup.id;
          const home = group.steps[0]?.href ?? "/";
          return (
            <a
              key={group.id}
              href={home}
              className="workspace-rail-item"
              aria-current={active ? "page" : undefined}
              data-testid={`workspace-${group.id}`}
              title={group.hint}
            >
              {group.short}
            </a>
          );
        })}
      </aside>
      <nav className="flow-nav" aria-label="当前工作区页面" data-testid="flow-nav">
        <div className="flow-nav-inner">
          <div className="flow-nav-brand">
            <strong>{currentGroup.title}</strong>
            <span>{currentGroup.hint}</span>
          </div>
          <div className="flow-group">
            <div className="flow-steps">
              {currentGroup.steps.map((step) => {
                const active = isFlowLinkActive(pathname, step.href);
                return (
                  <a
                    key={step.href}
                    href={step.href}
                    className="flow-step"
                    aria-current={active ? "page" : undefined}
                  >
                    <span className="flow-step-label">
                      <span className="flow-step-n">{step.n}</span>
                      {step.label}
                    </span>
                    <span className="flow-step-desc">{step.desc}</span>
                  </a>
                );
              })}
            </div>
          </div>
        </div>
      </nav>
    </div>
  );
}

function readParam(name: string): string | null {
  const value = new URLSearchParams(window.location.search).get(name);
  if (value === null || value.trim() === "") return null;
  return value;
}

function CurrentApplicationBanner() {
  const { data } = useExhibitCase();
  if (!data?.application_id) return null;
  const report = (data.report ?? {}) as {
    consistent?: number;
    inconsistent?: number;
    uncertain?: number;
  };
  return (
    <p className="demo-limitation" data-testid="current-application-banner">
      当前申请：{data.application_id}
      {` · 一致 ${report.consistent ?? 0} · 不一致 ${report.inconsistent ?? 0} · 存疑 ${report.uncertain ?? 0}`}
      {data.file_name ? `。来自第 1 步上传的 ${data.file_name}。` : "。"}
    </p>
  );
}

/** The one built artifact serves every shell; the pathname owns which role UI
 * mounts.  ``/`` is the canonical demo shell and ``/demo/react`` its alias;
 * ``/controlled/s01`` is the canonical Reviewer workbench (alias
 * ``/controlled/s01/react``), ``/controlled/s02`` the canonical Integrator
 * shell (alias ``/controlled/s02/react``), ``/controlled/s05`` the Exception
 * Approver shell, ``/controlled/s08`` the policy-release shell and
 * ``/controlled/s09`` the governance workspace shell and
 * ``/controlled/s12`` (alias ``/controlled/s12/react``) the Evaluation
 * Operator shell. */
function isDemoShell(): boolean {
  const pathname = window.location.pathname;
  return pathname === "/" || pathname === "/demo/react";
}
function isIntegratorShell(): boolean {
  return window.location.pathname.startsWith("/controlled/s02");
}
function isExceptionApproverShell(): boolean {
  return window.location.pathname.startsWith("/controlled/s05");
}
function isS08Shell(): boolean {
  return window.location.pathname.startsWith("/controlled/s08");
}
function isS09Shell(): boolean {
  return window.location.pathname.startsWith("/controlled/s09");
}
function isS12Shell(): boolean {
  return window.location.pathname.startsWith("/controlled/s12");
}

function isS13Shell(): boolean {
  return window.location.pathname.startsWith("/controlled/s13");
}

/** The S16 governed-deletion console mounted only for the canonical route
 * and its alias: a data-governance boundary whose panel runs the complete
 * dry-run -> approvals -> commit -> worker -> receipt flow. */
function isS16Shell(): boolean {
  const pathname = window.location.pathname;
  return pathname === "/controlled/s16" || pathname === "/controlled/s16/react";
}
function isS17Shell(): boolean {
  const pathname = window.location.pathname;
  return pathname === "/controlled/s17" || pathname === "/controlled/s17/react";
}

/** The T16 lifecycle cancellation workbench mounted only for the S14
 * canonical route and its alias: an integrator session boundary whose panel
 * reads the authoritative current-route/history and issues explicit cancel
 * commands. */
function isS14LifecycleShell(): boolean {
  const pathname = window.location.pathname;
  return pathname === "/controlled/s14" || pathname === "/controlled/s14/react";
}

/** The T16 termination-settlement console mounted only for its canonical
 * route and alias: a registered-operator boundary whose panel reads the S13
 * delivery view and issues settle/notification/grant/reopen commands. */
function isS14SettlementShell(): boolean {
  const pathname = window.location.pathname;
  return (
    pathname === "/controlled/s14/settlement" ||
    pathname === "/controlled/s14/settlement/react"
  );
}

/** The T06/T07 competition demo shell: only the closed synthetic facade
 * mounts here; no S01/S02/S05 read can fire. */
function DemoShell() {
  return (
    <div className="mx-auto max-w-4xl px-4 py-6">
      <header className="app-header">
        <h1>第 1 步 · 看一笔申请过不过关</h1>
        <span className="sr-only" data-testid="demo-boundary-track">
          C-DEMO
        </span>
        <span className="sr-only" data-testid="demo-boundary-scope">
          synthetic
        </span>
      </header>
      <PageGuide
        step="1"
        title="同一申请人名下，登记证、保单、合同、发票、身份证上的关键字段是否对得上？"
        what="字段先标准化（日期、金额、地址写法可以不同），再交叉比对。结论只有三种：一致、不一致、存疑。"
        how="上传 材料/task4_applications 里的申请 JSON，点「开始核验」。先看 *_ok.json，再看 *_vin_mismatch.json。离开本页再回来，核验结果仍在。"
      />
      <main>
        <DemoCheckPanel />
        <StepPager current="1" />
      </main>
    </div>
  );
}
function IntegratorShell() {
  return (
    <div className="mx-auto max-w-4xl px-4 py-6">
      <header className="app-header">
        <h1>第 3 步 · 补材料</h1>
        <span className="sr-only" data-testid="integrator-boundary-track">
          R-OBSERVED
        </span>
        <span className="sr-only" data-testid="integrator-boundary-gate">
          S02
        </span>
      </header>
      <PageGuide
        step="3"
        title="缺页、串页或拍糊了，补一版附件再核验。"
        what="本页承接第 1 步上传的同一笔申请。补的是这一笔的材料，不是另一套演示样例。"
        how="选择缺页或更清晰的附件上传。补完后可回到人工复核，或进入批准。"
      />
      <CurrentApplicationBanner />
      <main>
        <CaseFilePanel title="本笔申请（承接第 1 步核验）" />
        <SupplementUploadPanel />
        <StepPager current="3" />
      </main>
    </div>
  );
}

/** The Exception Approver shell mounted only for ``/controlled/s05/react``;
 * the ``request`` query value is presentation/navigation only and the S05 API
 * remains the sole authority.  The Approver never mounts any S01/S02 read. */
function ExceptionApproverShell() {
  return (
    <div className="mx-auto max-w-4xl px-4 py-6">
      <header className="app-header">
        <h1>第 4 步 · 批准</h1>
        <span className="sr-only" data-testid="approver-boundary-gate">
          S05
        </span>
      </header>
      <PageGuide
        step="4"
        title="对本笔核验结论做批准、特批或拒绝。"
        what="批准：按系统结论放行。特批：明知有差异仍放行。拒绝：不放行。"
        how="先看本笔差异快照和上一岗判断，再选一项并填写意见。"
      />
      <CurrentApplicationBanner />
      <main>
        <CaseFilePanel title="本笔申请（承接第 1 步核验）" />
        <ApprovalDecisionPanel />
        <StepPager current="4" />
      </main>
    </div>
  );
}

/** The governed policy-release shell mounted only for
 * ``/controlled/s08/react``; the ``candidate`` query value is non-sensitive
 * navigation state and the exact candidate query remains the sole
 * authorization/existence authority.  The shell never mounts any S01/S02/S05
 * read and no demo read can fire here. */
function PolicyReleaseShell() {
  const [candidateId, setCandidateId] = useState<string | null>(() =>
    readParam("candidate"),
  );
  const selectCandidate = useCallback((id: string) => {
    setCandidateId(id);
    window.history.pushState(null, "", `?candidate=${encodeURIComponent(id)}`);
  }, []);

  return (
    <div className="mx-auto max-w-4xl px-4 py-6">
      <header className="app-header">
        <h1>规则 · 发布规则</h1>
        <span className="sr-only" data-testid="s08-boundary-track">
          C-DEMO
        </span>
        <span className="sr-only" data-testid="s08-boundary-gate">
          S08
        </span>
      </header>
      <PageGuide
        step="R1"
        title="业务人员能通过界面维护校验规则，不必改代码。"
        what="规则包括完全一致、金额容差、列表包含、条件必填等。发布前要冻结候选版本，审批通过才生效。"
        how="选一个规则候选 → 提交审核 → 批准发布。不要直接改线上规则文件。"
      />
      <CurrentApplicationBanner />
      <main>
        <CaseFilePanel title="本笔申请（承接第 1 步核验）" />
        <PolicyReleasePanel
          candidateId={candidateId}
          onCandidateSelected={selectCandidate}
        />
      </main>
    </div>
  );
}

/** The T09 governance workspace shell mounted only for
 * ``/controlled/s09/react``: the four governance roles (admin, approver,
 * operator, auditor) read the atomic workspace under their own identity;
 * the workspace query remains the sole authorization/existence authority
 * and no S01/S02/S05 read beyond the auditor reconciliation can fire here. */
function GovernanceWorkspaceShell() {
  return (
    <div className="mx-auto max-w-4xl px-4 py-6">
      <header className="app-header">
        <h1>规则 · 规则影响</h1>
        <span className="sr-only" data-testid="s09-boundary-track">
          C-DEMO
        </span>
        <span className="sr-only" data-testid="s09-boundary-gate">
          S09
        </span>
      </header>
      <PageGuide
        step="R2"
        title="改规则前先看：有多少在途申请会被新规则拦住或放行。"
        what="如果影响面过大，可以先冻结，避免一边改规则一边放款。回滚也走同一套审批，不能悄悄撤。"
        how="打开工作区看影响清单。需要暂停时点冻结；需要撤回时走回滚候选，回到第 5 步审批。"
      />
      <CurrentApplicationBanner />
      <main>
        <CaseFilePanel title="本笔申请（承接第 1 步核验）" />
        <GovernanceWorkspacePanel />
      </main>
    </div>
  );
}

/** The T14 Evaluation Operator shell mounted only for ``/controlled/s12``
 * and its alias: it reads the frozen-plan catalog, starts one durable job
 * per explicit action, processes it once, polls the original job within a
 * fixed budget, and renders the sealed server bundle verbatim.  No S01/S02/
 * S05/S08/S09 read can fire here; every call carries only the closed S12
 * contract payloads. */
function EvaluationOperatorShell() {
  return (
    <div className="mx-auto max-w-4xl px-4 py-6">
      <header className="app-header">
        <h1>规则 · 成绩看板</h1>
        <span className="sr-only" data-testid="s12-boundary-gate">
          S12
        </span>
      </header>
      <PageGuide
        step="R3"
        title="对照三项指标：覆盖率 ≥ 80%，误报 ≤ 5%，漏报 ≤ 3%。"
        what="数字来自固定评测集，不是现场随手点出来的。看板只展示服务端封存的结果，页面自己不算分。"
        how="选一份已冻结的评测计划，点开始。跑完后看覆盖率、误报率、漏报率三张卡片，不要把合成数据说成真实 OCR 成绩。"
      />
      <CurrentApplicationBanner />
      <main>
        <CaseFilePanel title="本笔申请（承接第 1 步核验）" />
        <S12EvaluationOperator />
      </main>
    </div>
  );
}

/** The T15 delivery console shell mounted only for ``/controlled/s13``
 * and its alias: it reads the S13 delivery view for one application id
 * supplied via the ``application`` query value (presentation/navigation
 * only; the S13 delivery query remains the sole
 * authorization/existence/provenance authority).  No S01 queue/history or
 * demo read can fire here; every call carries only the closed S13
 * contract payloads.  The UI never describes Verification Routing as a
 * disbursement decision and never equates obligation creation with
 * downstream delivery receipt. */
function DeliveryConsoleShell() {
  return (
    <div className="mx-auto max-w-4xl px-4 py-6">
      <header className="app-header">
        <h1>批准后 · 结果投递</h1>
        <span className="sr-only" data-testid="s13-boundary-gate">
          S13
        </span>
      </header>
      <PageGuide
        step="5"
        title="把本笔核验结论交给下游，而不是在本页直接打款。"
        what="「核验完成」不等于「已经放款」。这里只记录结论是否已交给核心/放款系统。"
        how="先完成核验与批准，再点「投递本笔结论」。"
      />
      <CurrentApplicationBanner />
      <main>
        <CaseFilePanel title="本笔申请（承接第 1 步核验）" />
        <DeliveryStatusPanel />
      </main>
    </div>
  );
}

function LifecycleWorkbenchShell() {
  return (
    <div className="mx-auto max-w-4xl px-4 py-6">
      <header className="app-header">
        <h1>批准后 · 取消申请</h1>
        <span className="sr-only" data-testid="s14-boundary-track">
          C-DEMO
        </span>
        <span className="sr-only" data-testid="s14-boundary-gate">
          S14
        </span>
      </header>
      <PageGuide
        step="6"
        title="客户不要了，或材料补不齐时，明确取消本笔申请。"
        what="取消是一次显式动作，不是把页面关掉。"
        how="确认后点「取消本笔申请」。取消后不再投递、不再放款。"
      />
      <CurrentApplicationBanner />
      <main>
        <CaseFilePanel title="本笔申请（承接第 1 步核验）" />
        <CancelApplicationPanel />
      </main>
    </div>
  );
}

function SettlementConsoleShell() {
  return (
    <div className="mx-auto max-w-4xl px-4 py-6">
      <header className="app-header">
        <h1>批准后 · 终止清算</h1>
        <span className="sr-only" data-testid="s14-settlement-boundary-gate">
          S14
        </span>
      </header>
      <PageGuide
        step="7"
        title="申请取消或拒绝后，把下游收尾做完。"
        what="通知下游不要再处理这笔，收回未用权限。这不是重新放款。"
        how="本笔已取消后再点「完成终止收尾」。"
      />
      <CurrentApplicationBanner />
      <main>
        <CaseFilePanel title="本笔申请（承接第 1 步核验）" />
        <SettlementStatusPanel />
      </main>
    </div>
  );
}

function GovernedDeletionShell() {
  return (
    <div className="mx-auto max-w-4xl px-4 py-6">
      <header className="app-header">
        <h1>批准后 · 删除数据</h1>
        <span className="sr-only" data-testid="s16-boundary-track">
          S16
        </span>
        <span className="sr-only" data-testid="s16-boundary-gate">
          S16
        </span>
      </header>
      <PageGuide
        step="8"
        title="按合规要求删掉本笔申请在工作台里的副本。"
        what="展示用删除：标记本笔不再保留。正式系统还要两人批准并留回执。"
        how="确认后点「删除本笔数据」。"
      />
      <CurrentApplicationBanner />
      <main>
        <CaseFilePanel title="本笔申请（承接第 1 步核验）" />
        <DeletionStatusPanel />
      </main>
    </div>
  );
}

function GovernedExportShell() {
  return (
    <div className="mx-auto max-w-4xl px-4 py-6">
      <header className="app-header">
        <h1>批准后 · 导出报告</h1>
        <span className="sr-only" data-testid="s17-boundary-gate">
          S17
        </span>
      </header>
      <PageGuide
        step="9"
        title="把本笔核验结论导出给需要的人看。"
        what="正式系统要独立批准、一次性口令。这里导出的是本笔摘要。"
        how="点「导出本笔报告」。"
      />
      <CurrentApplicationBanner />
      <main>
        <CaseFilePanel title="本笔申请（承接第 1 步核验）" />
        <ExportStatusPanel />
      </main>
    </div>
  );
}

function ReviewerWorkbench() {
  return (
    <div className="mx-auto max-w-4xl px-4 py-6">
      <header className="app-header">
        <h1>第 2 步 · 人工复核</h1>
        <span className="sr-only" data-testid="boundary-track">
          C-DEMO
        </span>
        <span className="sr-only" data-testid="boundary-gate">
          G2
        </span>
      </header>
      <PageGuide
        step="2"
        title="系统已经给出三态结论，这里处理需要人看的申请。"
        what="不一致项必须能展开字段快照和差异高亮。审核员看的是「哪两张单据、哪个字段、原文是什么」，而不是一句「校验失败」。"
        how="对照本笔标红字段给出判断：确认结论、需要补材料，或提交特批。判断会带到下一步。"
      />
      <CurrentApplicationBanner />
      <main>
        <CaseFilePanel title="本笔申请（承接第 1 步核验）" />
        <ReviewDecisionPanel />
        <StepPager current="2" />
      </main>
    </div>
  );
}

export default function App() {
  let shell: React.ReactNode;
  if (isDemoShell()) {
    shell = <DemoShell />;
  } else if (isIntegratorShell()) {
    shell = <IntegratorShell />;
  } else if (isExceptionApproverShell()) {
    shell = <ExceptionApproverShell />;
  } else if (isS08Shell()) {
    shell = <PolicyReleaseShell />;
  } else if (isS09Shell()) {
    shell = <GovernanceWorkspaceShell />;
  } else if (isS12Shell()) {
    shell = <EvaluationOperatorShell />;
  } else if (isS13Shell()) {
    shell = <DeliveryConsoleShell />;
  } else if (isS16Shell()) {
    shell = <GovernedDeletionShell />;
  } else if (isS17Shell()) {
    shell = <GovernedExportShell />;
  } else if (isS14SettlementShell()) {
    shell = <SettlementConsoleShell />;
  } else if (isS14LifecycleShell()) {
    shell = <LifecycleWorkbenchShell />;
  } else {
    shell = <ReviewerWorkbench />;
  }
  return (
    <div className="app-frame">
      <FlowNavigation />
      <div className="app-main">{shell}</div>
    </div>
  );
}
