const UPLOADS_KEY = "task4_exhibit_uploads";
const CURRENT_KEY = "task4_exhibit_current";
const REVIEW_KEY = "task4_exhibit_review";
const APPROVAL_KEY = "task4_exhibit_approval";
const ATTACHMENTS_KEY = "task4_exhibit_attachments";

export type ExhibitSnapshot = {
  doc_type: string;
  field: string;
  raw: string | null;
  normalized: string | null;
};

export type ExhibitCheck = {
  rule_id: string;
  name: string;
  verdict: string;
  severity: string;
  message: string;
  snapshots: ExhibitSnapshot[];
  diff_left?: string | null;
  diff_right?: string | null;
  diff_detail?: string | null;
};

export type ExhibitDocument = {
  doc_id: string;
  doc_type: string;
  fields: { field: string; raw: string }[];
};

export type ExhibitUpload = {
  id: string;
  fileName: string;
  application: Record<string, unknown>;
  documents: ExhibitDocument[];
  report?: {
    application_id: string;
    consistent: number;
    inconsistent: number;
    uncertain: number;
    skipped: number;
  };
  checks: ExhibitCheck[];
  fullReport?: Record<string, unknown>;
};

export type ExhibitReviewDecision = {
  action: "confirm" | "need_material" | "exception";
  note: string;
  at: string;
};

export type ExhibitApprovalDecision = {
  action: "approve" | "exception" | "reject";
  note: string;
  at: string;
};

export type ExhibitAttachment = {
  name: string;
  size: number;
  kind: string;
  at: string;
};

export type ExhibitGovernance = {
  delivered: boolean;
  deliveredAt: string | null;
  cancelled: boolean;
  cancelledAt: string | null;
  settled: boolean;
  settledAt: string | null;
  deleted: boolean;
  deletedAt: string | null;
  exportNote: string | null;
};

const GOVERNANCE_KEY = "task4_exhibit_governance";

const EMPTY_GOVERNANCE: ExhibitGovernance = {
  delivered: false,
  deliveredAt: null,
  cancelled: false,
  cancelledAt: null,
  settled: false,
  settledAt: null,
  deleted: false,
  deletedAt: null,
  exportNote: null,
};

export function readGovernance(): ExhibitGovernance {
  return { ...EMPTY_GOVERNANCE, ...readJson<Partial<ExhibitGovernance>>(GOVERNANCE_KEY, {}) };
}

export function writeGovernance(value: ExhibitGovernance): void {
  writeJson(GOVERNANCE_KEY, value);
}

function readJson<T>(key: string, fallback: T): T {
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return fallback;
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

function writeJson(key: string, value: unknown): void {
  window.localStorage.setItem(key, JSON.stringify(value));
}

export function readExhibitUploads(): ExhibitUpload[] {
  const parsed = readJson<unknown>(UPLOADS_KEY, []);
  return Array.isArray(parsed) ? (parsed as ExhibitUpload[]) : [];
}

export function writeExhibitUploads(uploads: ExhibitUpload[]): void {
  writeJson(UPLOADS_KEY, uploads);
}

export function readExhibitCurrent(): ExhibitUpload | null {
  return readJson<ExhibitUpload | null>(CURRENT_KEY, null);
}

export function writeExhibitCurrent(upload: ExhibitUpload): void {
  writeJson(CURRENT_KEY, upload);
}

export function upsertExhibitUpload(
  uploads: ExhibitUpload[],
  next: ExhibitUpload,
): ExhibitUpload[] {
  const without = uploads.filter((item) => item.id !== next.id);
  return [...without, next];
}

export function readReviewDecision(): ExhibitReviewDecision | null {
  return readJson<ExhibitReviewDecision | null>(REVIEW_KEY, null);
}

export function writeReviewDecision(decision: ExhibitReviewDecision): void {
  writeJson(REVIEW_KEY, decision);
}

export function readApprovalDecision(): ExhibitApprovalDecision | null {
  return readJson<ExhibitApprovalDecision | null>(APPROVAL_KEY, null);
}

export function writeApprovalDecision(decision: ExhibitApprovalDecision): void {
  writeJson(APPROVAL_KEY, decision);
}

export function readAttachments(): ExhibitAttachment[] {
  const parsed = readJson<unknown>(ATTACHMENTS_KEY, []);
  return Array.isArray(parsed) ? (parsed as ExhibitAttachment[]) : [];
}

export function writeAttachments(items: ExhibitAttachment[]): void {
  writeJson(ATTACHMENTS_KEY, items);
}

export function documentsFromApplication(
  application: Record<string, unknown>,
): ExhibitDocument[] {
  const rawDocs = application.documents;
  if (!Array.isArray(rawDocs)) return [];
  return rawDocs.flatMap((doc) => {
    if (doc === null || typeof doc !== "object") return [];
    const record = doc as Record<string, unknown>;
    const fieldsRaw =
      record.fields !== null && typeof record.fields === "object"
        ? (record.fields as Record<string, unknown>)
        : {};
    const fields = Object.entries(fieldsRaw).map(([field, value]) => {
      let raw = "";
      if (typeof value === "string") raw = value;
      else if (value !== null && typeof value === "object") {
        const inner = (value as { raw?: unknown }).raw;
        raw = inner === null || inner === undefined ? "" : String(inner);
      } else if (value !== null && value !== undefined) {
        raw = String(value);
      }
      return { field, raw };
    });
    return [
      {
        doc_id: String(record.doc_id ?? ""),
        doc_type: String(record.doc_type ?? ""),
        fields,
      },
    ];
  });
}
