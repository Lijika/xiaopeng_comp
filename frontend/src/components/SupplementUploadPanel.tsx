import { useRef, useState } from "react";

import { useExhibitCase, useExhibitCaseSupplement } from "../api/hooks";
import { Button } from "./ui/button";

export default function SupplementUploadPanel() {
  const { data } = useExhibitCase();
  const supplement = useExhibitCaseSupplement();
  const fileRef = useRef<HTMLInputElement>(null);
  const [note, setNote] = useState("");
  const items = (data?.attachments ?? []) as Array<{ name?: string; size?: number; at?: string }>;

  if (!data?.application_id) {
    return (
      <section className="panel" data-testid="supplement-empty">
        <h2>补材料</h2>
        <p className="demo-limitation">还没有从第 1 步带过来的申请。请先上传 JSON 并完成核验。</p>
      </section>
    );
  }

  const onFiles = (files: FileList | null) => {
    if (files === null) return;
    Array.from(files).forEach((file) => {
      supplement.mutate({
        name: file.name,
        size: file.size,
        kind: file.type || "file",
        note,
      });
    });
  };

  return (
    <section className="panel" data-testid="supplement-panel">
      <h2>补一版材料</h2>
      <p className="demo-limitation">
        针对本笔 {data.file_name || data.application_id}。附件记录提交到服务端本笔案件。
      </p>
      <div className="demo-controls">
        <input
          id="supplement-files"
          ref={fileRef}
          data-testid="supplement-files"
          type="file"
          multiple
          className="sr-only"
          onChange={(event) => onFiles(event.target.files)}
        />
        <Button type="button" onClick={() => fileRef.current?.click()}>
          选择附件
        </Button>
      </div>
      <label htmlFor="supplement-note">补件说明</label>
      <textarea id="supplement-note" rows={2} value={note} onChange={(event) => setNote(event.target.value)} />
      {items.length === 0 ? (
        <p className="demo-limitation" data-testid="supplement-empty-list">尚未上传补件。</p>
      ) : (
        <ul data-testid="supplement-list">
          {items.map((item) => (
            <li key={`${item.name}-${item.at}`}>{item.name} · {item.size} 字节</li>
          ))}
        </ul>
      )}
    </section>
  );
}
