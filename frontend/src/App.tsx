import { useCallback, useEffect, useState } from "react";

import { useQueue } from "./api/hooks";
import AttachmentVersionPanel from "./components/AttachmentVersionPanel";
import BusinessExceptionApproverPanel from "./components/BusinessExceptionApproverPanel";
import QueuePanel from "./components/QueuePanel";
import RecoveryWorkPanel from "./components/RecoveryWorkPanel";
import ReviewWorkPanel from "./components/ReviewWorkPanel";

function readParam(name: string): string | null {
  return new URLSearchParams(window.location.search).get(name);
}

/** The one built artifact serves both controlled shells; the pathname owns
 * which role UI mounts.  ``/controlled/s01/react`` is the Reviewer shell and
 * ``/controlled/s02/react`` is the Integrator shell; every other path keeps
 * the Reviewer workbench behind the legacy URLs. */
function isIntegratorShell(): boolean {
  return window.location.pathname.startsWith("/controlled/s02");
}
function isExceptionApproverShell(): boolean {
  return window.location.pathname.startsWith("/controlled/s05");
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
  if (isIntegratorShell()) {
    return <IntegratorShell />;
  }
  if (isExceptionApproverShell()) {
    return <ExceptionApproverShell />;
  }
  return <ReviewerWorkbench />;
}
