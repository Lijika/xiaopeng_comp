import { useCallback, useState } from "react";

import { useQueue } from "./api/hooks";
import QueuePanel from "./components/QueuePanel";
import RecoveryWorkPanel from "./components/RecoveryWorkPanel";

function readWorkParam(): string | null {
  return new URLSearchParams(window.location.search).get("work");
}

export default function App() {
  const [workId, setWorkId] = useState<string | null>(readWorkParam);
  const queue = useQueue();
  const accessEnded = queue.data?.access_ended === true;

  const openWork = useCallback((id: string) => {
    setWorkId(id);
    window.history.pushState(null, "", `?work=${encodeURIComponent(id)}`);
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
        <QueuePanel onOpenWork={openWork} />
        {workId !== null && !accessEnded && (
          <RecoveryWorkPanel key={workId} workId={workId} />
        )}
      </main>
    </div>
  );
}
