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
import ReviewWorkPanel from "./components/ReviewWorkPanel";

function readParam(name: string): string | null {
  return new URLSearchParams(window.location.search).get(name);
}

/** The one built artifact serves every shell; the pathname owns which role UI
 * mounts.  ``/`` is the canonical demo shell and ``/demo/react`` its alias;
 * ``/controlled/s01`` is the canonical Reviewer workbench (alias
 * ``/controlled/s01/react``), ``/controlled/s02`` the canonical Integrator
 * shell (alias ``/controlled/s02/react``), ``/controlled/s05`` the Exception
 * Approver shell, ``/controlled/s08`` the policy-release shell and
 * ``/controlled/s09`` the governance workspace shell. */
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
  return <ReviewerWorkbench />;
}
