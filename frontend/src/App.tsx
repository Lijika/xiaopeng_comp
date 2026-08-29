import { useCallback, useEffect, useState } from "react";

import { useQueue } from "./api/hooks";
import AttachmentVersionPanel from "./components/AttachmentVersionPanel";
import BusinessExceptionApproverPanel from "./components/BusinessExceptionApproverPanel";
import DemoBatchSummaryPanel from "./components/DemoBatchSummaryPanel";
import DemoCheckPanel from "./components/DemoCheckPanel";
import PolicyReleasePanel, {
  GovernanceWorkspacePanel,
} from "./components/PolicyReleasePanel";
import QueuePanel from "./components/QueuePanel";
import RecoveryWorkPanel from "./components/RecoveryWorkPanel";
import S12EvaluationOperator from "./components/S12EvaluationOperator";
import ReviewWorkPanel from "./components/ReviewWorkPanel";
import T16LifecyclePanel, {
  T16SettlementPanel,
} from "./components/T16LifecyclePanel";
import T15DeliveryPanel from "./components/T15DeliveryPanel";
import S16GovernedDeletionPanel from "./components/S16GovernedDeletionPanel";
import S17ExportPanel from "./components/S17ExportPanel";

function readParam(name: string): string | null {
  return new URLSearchParams(window.location.search).get(name);
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
        <h1>凭证一致性校验演示</h1>
        <span className="boundary track" data-testid="demo-boundary-track">
          C-DEMO
        </span>
        <span className="boundary" data-testid="demo-boundary-scope">
          synthetic
        </span>
      </header>
      <main>
        <DemoCheckPanel />
        <DemoBatchSummaryPanel />
      </main>
    </div>
  );
}
function IntegratorShell() {
  return (
    <div className="mx-auto max-w-4xl px-4 py-6">
      <header className="app-header">
        <h1>受控材料补充工作台</h1>
        <span className="boundary track" data-testid="integrator-boundary-track">
          R-OBSERVED
        </span>
        <span className="boundary" data-testid="integrator-boundary-gate">
          S02
        </span>
      </header>
      <main>
        <AttachmentVersionPanel />
      </main>
    </div>
  );
}

/** The Exception Approver shell mounted only for ``/controlled/s05/react``;
 * the ``request`` query value is presentation/navigation only and the S05 API
 * remains the sole authority.  The Approver never mounts any S01/S02 read. */
function ExceptionApproverShell() {
  const requestId = readParam("request");
  return (
    <div className="mx-auto max-w-4xl px-4 py-6">
      <header className="app-header">
        <h1>业务例外审批工作台</h1>
        <span className="boundary" data-testid="approver-boundary-gate">
          S05
        </span>
      </header>
      <main>
        <BusinessExceptionApproverPanel requestId={requestId} />
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
        <h1>治理策略发布工作台</h1>
        <span className="boundary track" data-testid="s08-boundary-track">
          C-DEMO
        </span>
        <span className="boundary" data-testid="s08-boundary-gate">
          S08
        </span>
      </header>
      <main>
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
        <h1>治理影响与安全冻结工作台</h1>
        <span className="boundary track" data-testid="s09-boundary-track">
          C-DEMO
        </span>
        <span className="boundary" data-testid="s09-boundary-gate">
          S09
        </span>
      </header>
      <main>
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
        <h1>评价操作台</h1>
        <span className="boundary" data-testid="s12-boundary-gate">
          S12
        </span>
      </header>
      <main>
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
  const [applicationId, setApplicationId] = useState<string | null>(() =>
    readParam("application"),
  );
  useEffect(() => {
    const sync = () => setApplicationId(readParam("application"));
    window.addEventListener("popstate", sync);
    return () => window.removeEventListener("popstate", sync);
  }, []);
  return (
    <div className="mx-auto max-w-4xl px-4 py-6">
      <header className="app-header">
        <h1>投递控制台</h1>
        <span className="boundary" data-testid="s13-boundary-gate">
          S13
        </span>
      </header>
      <main>
        <T15DeliveryPanel applicationId={applicationId} />
      </main>
    </div>
  );
}

/** The T16 integrator workbench shell: the ``application``/``cycle`` query
 * values are presentation/navigation state only — selecting an old cycle in
 * history never issues any command, least of all reopen. */
function LifecycleWorkbenchShell() {
  const [applicationId, setApplicationId] = useState<string | null>(() =>
    readParam("application"),
  );
  const [cycle, setCycle] = useState<number | null>(() => {
    const raw = readParam("cycle");
    return raw === null ? null : Number.parseInt(raw, 10);
  });
  useEffect(() => {
    const sync = () => {
      setApplicationId(readParam("application"));
      const raw = readParam("cycle");
      setCycle(raw === null ? null : Number.parseInt(raw, 10));
    };
    window.addEventListener("popstate", sync);
    return () => window.removeEventListener("popstate", sync);
  }, []);
  const selectCycle = useCallback((selected: number) => {
    setCycle(selected);
    const params = new URLSearchParams(window.location.search);
    params.set("cycle", String(selected));
    window.history.pushState(null, "", `?${params.toString()}`);
  }, []);
  return (
    <div className="mx-auto max-w-4xl px-4 py-6">
      <header className="app-header">
        <h1>生命周期取消工作台</h1>
        <span className="boundary track" data-testid="s14-boundary-track">
          C-DEMO
        </span>
        <span className="boundary" data-testid="s14-boundary-gate">
          S14
        </span>
      </header>
      <main>
        <T16LifecyclePanel
          applicationId={applicationId}
          selectedCycle={cycle}
          onCycleSelected={selectCycle}
        />
      </main>
    </div>
  );
}

/** The T16 operator settlement console shell: navigation values are never
 * command inputs; reopen happens only from the panel's explicit button. */
function SettlementConsoleShell() {
  const [applicationId, setApplicationId] = useState<string | null>(() =>
    readParam("application"),
  );
  useEffect(() => {
    const sync = () => setApplicationId(readParam("application"));
    window.addEventListener("popstate", sync);
    return () => window.removeEventListener("popstate", sync);
  }, []);
  return (
    <div className="mx-auto max-w-4xl px-4 py-6">
      <header className="app-header">
        <h1>终止结算控制台</h1>
        <span className="boundary" data-testid="s14-settlement-boundary-gate">
          S14
        </span>
      </header>
      <main>
        <T16SettlementPanel applicationId={applicationId} />
      </main>
    </div>
  );
}

/** The S16 governed-deletion shell: the ``application`` query value is
 * presentation/navigation state only; every preflight/approve/commit/repair
 * command stays guarded by the registered governance/approver identities.
 * No S01/S02/S05/S08/S09/S12/S13/S14 read can fire here. */
function GovernedDeletionShell() {
  return (
    <div className="mx-auto max-w-4xl px-4 py-6">
      <header className="app-header">
        <h1>合规删除工作台</h1>
        <span className="boundary track" data-testid="s16-boundary-track">
          S16
        </span>
        <span className="boundary" data-testid="s16-boundary-gate">
          S16
        </span>
      </header>
      <main>
        <S16GovernedDeletionPanel />
      </main>
    </div>
  );
}

function GovernedExportShell() {
  return <div className="mx-auto max-w-4xl px-4 py-6"><header className="app-header"><h1>受控导出工作台</h1><span className="boundary">S17</span></header><main><S17ExportPanel /></main></div>;
}

/** The Reviewer workbench owns every S01 read; it is never mounted on the
 * Integrator shell, so no S01 query can fire there. */
function ReviewerWorkbench() {
  const [workId, setWorkId] = useState<string | null>(() => readParam("work"));
  const [reviewId, setReviewId] = useState<string | null>(() =>
    readParam("review"),
  );
  const queue = useQueue();
  const accessEnded = queue.data?.access_ended === true;

  useEffect(() => {
    const syncSelection = () => {
      const nextReviewId = readParam("review");
      setReviewId(nextReviewId);
      setWorkId(nextReviewId === null ? readParam("work") : null);
    };
    window.addEventListener("popstate", syncSelection);
    return () => window.removeEventListener("popstate", syncSelection);
  }, []);

  // The two panels are mutually exclusive: opening one clears the other, so
  // the URL and the mounted panel always agree.
  const openWork = useCallback((id: string) => {
    setWorkId(id);
    setReviewId(null);
    window.history.pushState(null, "", `?work=${encodeURIComponent(id)}`);
  }, []);

  const openReview = useCallback((id: string) => {
    setReviewId(id);
    setWorkId(null);
    window.history.pushState(null, "", `?review=${encodeURIComponent(id)}`);
  }, []);

  return (
    <div className="mx-auto max-w-4xl px-4 py-6">
      <header className="app-header">
        <h1>受控审核工作台</h1>
        <span className="boundary track" data-testid="boundary-track">
          C-DEMO
        </span>
        <span className="boundary" data-testid="boundary-gate">
          G2
        </span>
      </header>
      <main>
        <QueuePanel onOpenWork={openWork} onOpenReview={openReview} />
        {workId !== null && !accessEnded && (
          <RecoveryWorkPanel key={workId} workId={workId} />
        )}
        {reviewId !== null && !accessEnded && (
          <ReviewWorkPanel key={reviewId} workId={reviewId} />
        )}
      </main>
    </div>
  );
}

export default function App() {
  if (isDemoShell()) {
    return <DemoShell />;
  }
  if (isIntegratorShell()) {
    return <IntegratorShell />;
  }
  if (isExceptionApproverShell()) {
    return <ExceptionApproverShell />;
  }
  if (isS08Shell()) {
    return <PolicyReleaseShell />;
  }
  if (isS09Shell()) {
    return <GovernanceWorkspaceShell />;
  }
  if (isS12Shell()) {
    return <EvaluationOperatorShell />;
  }
  if (isS13Shell()) {
    return <DeliveryConsoleShell />;
  }
  if (isS16Shell()) {
    return <GovernedDeletionShell />;
  }
  if (isS17Shell()) {
    return <GovernedExportShell />;
  }
  if (isS14SettlementShell()) {
    return <SettlementConsoleShell />;
  }
  if (isS14LifecycleShell()) {
    return <LifecycleWorkbenchShell />;
  }
  return <ReviewerWorkbench />;
}
