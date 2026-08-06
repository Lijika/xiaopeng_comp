import { useCallback, useEffect, useState } from "react";

import { useQueue } from "./api/hooks";
import QueuePanel from "./components/QueuePanel";
import RecoveryWorkPanel from "./components/RecoveryWorkPanel";
import ReviewWorkPanel from "./components/ReviewWorkPanel";

function readParam(name: string): string | null {
  return new URLSearchParams(window.location.search).get(name);
}

export default function App() {
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
