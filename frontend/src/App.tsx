import { useCallback, useState } from "react";

import QueuePanel from "./components/QueuePanel";
import RecoveryWorkPanel from "./components/RecoveryWorkPanel";

function readWorkParam(): string | null {
  return new URLSearchParams(window.location.search).get("work");
}

export default function App() {
  const [workId, setWorkId] = useState<string | null>(readWorkParam);

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
        {workId !== null && <RecoveryWorkPanel workId={workId} />}
      </main>
    </div>
  );
}
