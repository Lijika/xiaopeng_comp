import { randomUUID } from "node:crypto";
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { describe, expect, it, vi } from "vitest";

import ReviewWorkPanel from "./ReviewWorkPanel";
import { MANUAL_WORK_KEY, WORKSPACE_KEY } from "../api/hooks";
import type { WorkspaceResponse } from "../api/client";
import {
  fetchRouter,
  renderWithQuery,
  restrictedDigest,
} from "../test-utils";
import type { RouteHandler } from "../test-utils";

const WORK_ID = "work_t02panel1234567890abcdef";
const APP_ID = "app_t02panel9876543210fedcba";
const FINDING_ID = "finding_t02panel0000000000000001";
const WORK_PATH =
  "/controlled/s01/api/queries/review-work-items/work_t02panel1234567890abcdef";
const WORKSPACE_PATH = `/controlled/s01/api/queries/applications/${APP_ID}/workspace`;
const ROUTE_PATH = `/controlled/s01/api/queries/applications/${APP_ID}/current-route`;
const HISTORY_PATH = `/controlled/s01/api/queries/applications/${APP_ID}/history`;
const CLAIM_PATH =
  "/controlled/s01/api/commands/review-work-items/work_t02panel1234567890abcdef/claim";
const RENEW_PATH =
  "/controlled/s01/api/commands/review-work-items/work_t02panel1234567890abcdef/renew";
const SUBMIT_PATH =
  "/controlled/s01/api/commands/review-work-items/work_t02panel1234567890abcdef/submit";
const RELEASE_PATH =
  "/controlled/s01/api/commands/review-work-items/work_t02panel1234567890abcdef/release";
const REVEAL_PATH =
  "/controlled/s01/api/commands/review-work-items/work_t02panel1234567890abcdef/reveal-field-observation";
const CORRECT_PATH =
  "/controlled/s01/api/commands/review-work-items/work_t02panel1234567890abcdef/correct-field-observation";
const MEMBERSHIP_PATH =
  "/controlled/s01/api/commands/review-work-items/work_t02panel1234567890abcdef/correct-page-membership";

const SOURCE_SENTINEL = `restricted-source:${randomUUID()}`;
const CORRECTION_SENTINEL = `restricted-correction:${randomUUID()}`;

function expectRestrictedEqual(actual: unknown, expected: unknown) {
  expect(restrictedDigest(actual)).toBe(restrictedDigest(expected));
}

function expectRestrictedAbsent(haystack: unknown, needle: string) {
  expect(String(haystack).includes(needle)).toBe(false);
}

function restrictedElements(testId: string): HTMLElement[] {
  return Array.from(
    document.querySelectorAll<HTMLElement>(`[data-testid="${testId}"]`),
  );
}

function expectRestrictedElementsAbsent(testId: string) {
  expect(restrictedElements(testId).length).toBe(0);
}

function restrictedElement(testId: string): HTMLElement {
  const elements = restrictedElements(testId);
  expect(elements.length).toBe(1);
  return elements[0];
}

function restrictedButtons(name: string): HTMLButtonElement[] {
  return Array.from(document.querySelectorAll<HTMLButtonElement>("button")).filter(
    (button) => button.textContent?.trim() === name,
  );
}

function restrictedButton(name: string, index = 0): HTMLButtonElement {
  const buttons = restrictedButtons(name);
  expect(buttons.length).toBeGreaterThan(index);
  return buttons[index];
}

async function findRestrictedElements(testId: string): Promise<HTMLElement[]> {
  await vi.waitFor(() => {
    expect(restrictedElements(testId).length).toBeGreaterThan(0);
  });
  return restrictedElements(testId);
}

const CONTEXT = {
  lifecycle_revision: 6,
  evidence_revision: 1,
  run_id: "run_t02panel",
  projection_watermark: 1,
  current_context: "a".repeat(64),
};

function workPayload(overrides: Record<string, unknown> = {}) {
  return {
    status: "unclaimed",
    application_id: APP_ID,
    work_item_id: WORK_ID,
    claim_subject: null,
    claim_fence: 0,
    claim_expires_at: 0,
    phase: "Manual Review",
    route: "manual_review",
    lifecycle_revision: 6,
    evidence_revision: 1,
    command_context: CONTEXT,
    automatic_findings: [
      {
        finding_id: FINDING_ID,
        rule_id: "R_ENGINE_CROSS",
        verdict: "uncertain",
        severity: "critical",
        reason_code: "ENGINE_CROSS_UNCERTAIN",
      },
    ],
    run_authority: {
      run_id: "run_t02panel",
      status: "complete",
      authority_digest: "b".repeat(64),
    },
    decision: null,
    decisions: [],
    completed_finding_ids: [],
    ...overrides,
  };
}

function workspacePayload(overrides: Record<string, unknown> = {}) {
  return {
    application_id: APP_ID,
    work_item_id: WORK_ID,
    assigned_subject: "t02-reviewer",
    claim_fence: 0,
    claim_expires_at: 0,
    track: "C-DEMO",
    phase: "Manual Review",
    route: "manual_review",
    evidence_ready: true,
    lifecycle_revision: 6,
    evidence_revision: 1,
    current_run_id: "run_t02panel",
    evidence_snapshot_id: "snapshot_t02panel",
    evidence_snapshot_digest: "c".repeat(64),
    projection_watermark: 1,
    mandatory_blockers: [
      {
        finding_id: FINDING_ID,
        run_id: "run_t02panel",
        rule_id: "R_ENGINE_CROSS",
        verdict: "uncertain",
        severity: "critical",
        reason_code: "ENGINE_CROSS_UNCERTAIN",
        mandatory: true,
        evidence_links: [
          {
            document_id: "reg",
            document_role: "机动车登记证书",
            field: "engine_no",
            value_state: "present",
            raw_masked: "[REDACTED]",
            observation_id: "observation_t02panel",
            source_sha256: "d".repeat(64),
            provenance_manifest_digest: "e".repeat(64),
            evidence_eligible: true,
            eligibility_reason: "REGISTERED_SOURCE_PROVENANCE_VERIFIED",
            source_page: 1,
            source_region: "region:1",
          },
        ],
      },
    ],
    selected_finding: {
      finding_id: FINDING_ID,
      run_id: "run_t02panel",
      rule_id: "R_ENGINE_CROSS",
      verdict: "uncertain",
      severity: "critical",
      reason_code: "ENGINE_CROSS_UNCERTAIN",
      mandatory: true,
      evidence_links: [
        {
          document_id: "reg",
          document_role: "机动车登记证书",
          field: "engine_no",
          value_state: "present",
          raw_masked: "[REDACTED]",
          observation_id: "observation_t02panel",
          source_sha256: "d".repeat(64),
          provenance_manifest_digest: "e".repeat(64),
          evidence_eligible: true,
          eligibility_reason: "REGISTERED_SOURCE_PROVENANCE_VERIFIED",
          source_page: 1,
          source_region: "region:1",
        },
      ],
    },
    actions: ["read_evidence"],
    ...overrides,
  };
}

function routePayload(overrides: Record<string, unknown> = {}) {
  return {
    schema_version: "s04-current-route/1",
    application_id: APP_ID,
    phase: "Manual Review",
    route: "manual_review",
    current_run_id: "run_t02panel",
    cycle: 1,
    lifecycle_revision: 6,
    evidence_revision: 1,
    evidence_snapshot_id: "snapshot_t02panel",
    evidence_snapshot_digest: "c".repeat(64),
    release_id: "auto_lease@1.9.0",
    release_digest: "f".repeat(64),
    checker_build: "s01-target-checker/6",
    currentness_reason: "CURRENT_CONTEXT_MATCH",
    completion_basis: null,
    exception_id: null,
    exception_decision_id: null,
    exception_expires_at: null,
    failure: null,
    ...overrides,
  };
}

function historyPayload(overrides: Record<string, unknown> = {}) {
  return {
    schema_version: "s04-application-history/1",
    application_id: APP_ID,
    current_run_id: "run_t02panel",
    runs: [
      {
        run_id: "run_t02panel",
        status: "complete",
        authority_digest: "b".repeat(64),
        current: true,
        currentness_reason: "CURRENT_CONTEXT_MATCH",
        cycle: 1,
        lifecycle_revision: 6,
        evidence_revision: 1,
        evidence_snapshot_id: "snapshot_t02panel",
        evidence_snapshot_digest: "c".repeat(64),
        release_id: "auto_lease@1.9.0",
        release_digest: "f".repeat(64),
        checker_build: "s01-target-checker/6",
        finding_ids: [FINDING_ID],
        cas_mismatches: [],
        selected_observation_ids: ["observation_t02panel"],
        decision_ids: [],
        exception_ids: [],
        applicable_decision_ids: [],
        applicable_exception_ids: [],
        invalidated_decision_ids: [],
        invalidated_exception_ids: [],
      },
    ],
    corrections: [],
    business_exceptions: [],
    attachment_versions: [],
    ...overrides,
  };
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** A live claim with a realistic relative expiry (the production claim TTL
 * is 900 seconds): the issued token must be unexpired for the restricted
 * reveal/correction flow, and the expiry timer must stay inside the
 * JavaScript timer ceiling so no overflow warning or 1ms clamp occurs. */
const LIVE_CLAIM_EXPIRES_AT = Math.floor(Date.now() / 1000) + 900;

function claimedWorkPayload(overrides: Record<string, unknown> = {}) {
  return workPayload({
    status: "claimed",
    claim_subject: "t02-reviewer",
    claim_fence: 1,
    claim_expires_at: LIVE_CLAIM_EXPIRES_AT,
    ...overrides,
  });
}

function claimedWorkspacePayload() {
  return workspacePayload({
    claim_fence: 1,
    claim_expires_at: LIVE_CLAIM_EXPIRES_AT,
  });
}

/** One S10 ledger page with two server-authorized candidate claims.  The
 * candidates are opaque to the browser: React renders them individually and
 * the Reviewer must pick exactly one claim explicitly. */
const S10_MEMBERSHIP_FINDING_ID = "finding_s10panel0000000000000002";
const S10_PREDECESSOR_DECISION_ID = "decision_s10panel_prior";
const S10_SOURCE_EVIDENCE = {
  event_id: "evidence_s10panel",
  evidence_revision: 2,
};
const S10_CANDIDATES = [
  {
    document_instance_id: "reg_cert_instance_a",
    document_role: "机动车登记证书",
    claim_id: "s10_claim_a",
    provenance: {
      adapter_id: "c-demo-s10-fixture",
      adapter_version: "1",
      source_pointer: "/pages/0",
    },
  },
  {
    document_instance_id: "reg::cert_instance_b",
    document_role: "机动车登记证书",
    claim_id: "s10::claim_b",
    provenance: {
      adapter_id: "c-demo-s10-fixture",
      adapter_version: "1",
      source_pointer: "/pages/0",
    },
  },
];

/** The page-1 membership carried by the selected finding, mirroring the
 * production workspace so the membership pane renders from server facts even
 * before any draft opens. */
const S10_FINDING_MEMBERSHIP = {
  attachment_id: "s10-attachment-1",
  page_source_sha256: "10".repeat(32),
  page_ordinal: 1,
  state: "selected",
  active_decision_ids: [S10_PREDECESSOR_DECISION_ID],
  source_evidence: S10_SOURCE_EVIDENCE,
  candidates: S10_CANDIDATES,
};

function s10MembershipWorkspacePayload(
  overrides: Record<string, unknown> = {},
): WorkspaceResponse {
  return workspacePayload({
    claim_fence: 1,
    claim_expires_at: LIVE_CLAIM_EXPIRES_AT,
    evidence_revision: 2,
    selected_finding: {
      ...workspacePayload().selected_finding,
      membership: S10_FINDING_MEMBERSHIP,
    },
    membership_ledger: [
      {
        attachment_id: "s10-attachment-1",
        page_source_sha256: "10".repeat(32),
        page_ordinal: 1,
        state: "selected",
        finding_id: S10_MEMBERSHIP_FINDING_ID,
        active_decision_ids: [S10_PREDECESSOR_DECISION_ID],
        source_evidence: S10_SOURCE_EVIDENCE,
        candidates: S10_CANDIDATES,
        decisions: [
          {
            record_kind: "accepted",
            decision_id: S10_PREDECESSOR_DECISION_ID,
            document_instance_id: "reg_cert_instance_a",
            document_role: "机动车登记证书",
            actor: "t02-reviewer",
            reason_code: "MEMBERSHIP_SOURCE_VERIFIED",
            time: 100,
            cycle: 1,
            source_evidence: {
              ...S10_SOURCE_EVIDENCE,
              candidate_claim_id: "s10_claim_a",
            },
            supersedes: [],
            status: "active",
          },
        ],
      },
      {
        attachment_id: "s10-attachment-2",
        page_source_sha256: "20".repeat(32),
        page_ordinal: 2,
        state: "unassigned",
        finding_id: "finding_s10panel0000000000000003",
        active_decision_ids: ["decision_s10panel_unassigned"],
        source_evidence: S10_SOURCE_EVIDENCE,
        candidates: [S10_CANDIDATES[0]],
        decisions: [
          {
            record_kind: "unassigned",
            decision_id: "decision_s10panel_unassigned",
            document_instance_id: null,
            document_role: null,
            actor: "t02-reviewer",
            reason_code: "MEMBERSHIP_PAGE_UNASSIGNED",
            time: 101,
            cycle: 1,
            source_evidence: {
              ...S10_SOURCE_EVIDENCE,
              candidate_claim_id: "s10_claim_a",
            },
            supersedes: [],
            status: "active",
          },
        ],
      },
    ],
    ...overrides,
  });
}

function baseRoutes() {
  return {
    [`GET ${WORK_PATH}`]: () => jsonResponse(workPayload()),
    [`GET ${WORKSPACE_PATH}`]: () => jsonResponse(workspacePayload()),
    [`GET ${ROUTE_PATH}`]: () => jsonResponse(routePayload()),
    [`GET ${HISTORY_PATH}`]: () => jsonResponse(historyPayload()),
  };
}

/** Waits until every owning read the action gate requires is loaded. */
async function waitForReviewReady() {
  await vi.waitFor(() => {
    for (const testId of [
      "review-workspace-rule",
      "gate-phase",
      "review-history-decisions",
      "review-status",
    ]) {
      expect(restrictedElements(testId).length).toBeGreaterThan(0);
    }
  });
}

describe("ReviewWorkPanel (T02)", () => {
  it("shows an explicit loading state while the work item is in flight", () => {
    fetchRouter({
      [`GET ${WORK_PATH}`]: () =>
        new Promise(() => {
          // The bounded pending promise owns the loading state.
        }),
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    expect(screen.getByTestId("review-panel")).toBeInTheDocument();
    expect(screen.getByTestId("review-loading")).toBeInTheDocument();
  });

  it("moves keyboard focus to the panel heading on open", async () => {
    fetchRouter(baseRoutes());
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitFor(() =>
      expect(screen.getByTestId("review-status")).toHaveTextContent("unclaimed"),
    );
    expect(document.activeElement).toBe(
      screen.getByRole("heading", { level: 2, name: "人工核验" }),
    );
  });

  it("never issues a command POST on mount, even under StrictMode", () => {
    const router = fetchRouter(baseRoutes());
    renderWithQuery(
      <StrictMode>
        <ReviewWorkPanel workId={WORK_ID} />
      </StrictMode>,
    );
    expect(router.calls.filter((call) => call.method === "POST")).toHaveLength(0);
  });

  it("hides existence behind an explicit not-found state for a 404 work item", async () => {
    fetchRouter({
      [`GET ${WORK_PATH}`]: () =>
        jsonResponse({ detail: { error: "S03_NOT_FOUND" } }, 404),
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitFor(() =>
      expect(screen.getByTestId("review-error")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("review-error")).toHaveTextContent(
      "未找到或无权访问",
    );
    expect(screen.getByTestId("review-error").textContent).not.toContain(
      "S03_NOT_FOUND",
    );
    expect(screen.queryByTestId("review-status")).not.toBeInTheDocument();
  });

  it("shows an explicit unavailable state for an initial 503 work read", async () => {
    fetchRouter({
      [`GET ${WORK_PATH}`]: () =>
        jsonResponse({ detail: { error: "S03_UNAVAILABLE" } }, 503),
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    // The transient 503 is retried with backoff before the query fails closed.
    await waitFor(
      () =>
        expect(screen.getByTestId("review-error")).toHaveTextContent(
          "工作项不可用",
        ),
      { timeout: 8_000 },
    );
    expect(screen.queryByTestId("review-status")).not.toBeInTheDocument();
  });

  it("renders server-owned facts, the finding-first workspace, and never derives lifecycle", async () => {
    const router = fetchRouter(baseRoutes());
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    expect(screen.getByTestId("review-phase")).toHaveTextContent("Manual Review");
    expect(screen.getByTestId("review-route")).toHaveTextContent("manual_review");
    expect(screen.getByTestId("review-claim-fence")).toHaveTextContent("0");
    expect(screen.getByTestId("review-finding-rule")).toHaveTextContent(
      "R_ENGINE_CROSS",
    );
    expect(screen.getByTestId("review-finding-verdict")).toHaveTextContent(
      "uncertain",
    );
    expect(screen.getByTestId("review-run-digest")).toHaveTextContent(
      /^[0-9a-f]{64}$/,
    );
    expect(screen.getByTestId("review-workspace-rule")).toHaveTextContent(
      "R_ENGINE_CROSS",
    );
    expect(screen.getByTestId("review-evidence-masked")).toHaveTextContent(
      "[REDACTED]",
    );
    expect(screen.getByTestId("gate-phase")).toHaveTextContent(
      "Manual Review",
    );
    expect(screen.getByTestId("review-history-decisions")).toHaveTextContent(
      "None",
    );
    const panelText = restrictedElement("review-panel").textContent ?? "";
    expect(panelText).not.toContain("Verification Completed");
    expect(panelText).not.toContain("human_complete");
    expect(router.calls.filter((call) => call.method === "GET")).toHaveLength(4);
  });

  it("renders lease freshness, revisions, watermark, and masked evidence provenance facts", async () => {
    fetchRouter(baseRoutes());
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    expect(screen.getByTestId("review-claim-expiry")).toHaveTextContent("0");
    expect(screen.getByTestId("review-lifecycle-revision")).toHaveTextContent(
      "6",
    );
    expect(screen.getByTestId("review-evidence-revision")).toHaveTextContent(
      "1",
    );
    expect(screen.getByTestId("review-workspace-expiry")).toHaveTextContent("0");
    expect(screen.getByTestId("review-workspace-lifecycle")).toHaveTextContent(
      "6",
    );
    expect(
      screen.getByTestId("review-workspace-evidence-revision"),
    ).toHaveTextContent("1");
    expect(screen.getByTestId("review-workspace-watermark")).toHaveTextContent(
      "1",
    );
    expect(screen.getByTestId("review-workspace-current-run")).toHaveTextContent(
      "run_t02panel",
    );
    expect(screen.getByTestId("review-workspace-snapshot")).toHaveTextContent(
      "snapshot_t02panel",
    );
    expect(screen.getByTestId("review-evidence-role")).toHaveTextContent(
      "机动车登记证书",
    );
    expect(screen.getByTestId("review-evidence-source-page")).toHaveTextContent(
      "1",
    );
    expect(
      screen.getByTestId("review-evidence-source-region"),
    ).toHaveTextContent("region:1");
    expect(screen.getByTestId("review-evidence-provenance")).toHaveTextContent(
      /^[0-9a-f]{64}$/,
    );
    expect(screen.getByTestId("review-evidence-eligibility")).toHaveTextContent(
      "REGISTERED_SOURCE_PROVENANCE_VERIFIED",
    );
  });

  it("selects an active ledger page and submits a superseding membership decision", async () => {
    const MEMBERSHIP_FINDING_ID = "finding_s10panel0000000000000002";
    const PREDECESSOR_DECISION_ID = "decision_s10panel_prior";
    const SUCCESSOR_CONTEXT = {
      ...CONTEXT,
      evidence_revision: 2,
      current_context: "9".repeat(64),
    };
    const SOURCE_EVIDENCE = {
      event_id: "evidence_s10panel",
      evidence_revision: 2,
    };
    const CANDIDATES = [
      {
        document_instance_id: "reg_cert_instance_a",
        document_role: "机动车登记证书",
        claim_id: "s10_claim_a",
        provenance: {
          adapter_id: "c-demo-s10-fixture",
          adapter_version: "1",
          source_pointer: "/pages/0",
        },
      },
      {
        document_instance_id: "reg::cert_instance_b",
        document_role: "机动车登记证书",
        claim_id: "s10::claim_b",
        provenance: {
          adapter_id: "c-demo-s10-fixture",
          adapter_version: "1",
          source_pointer: "/pages/0",
        },
      },
    ];
    const membershipWorkspace = workspacePayload({
      claim_fence: 1,
      claim_expires_at: LIVE_CLAIM_EXPIRES_AT,
      evidence_revision: 2,
      membership_ledger: [
        {
          attachment_id: "s10-attachment-1",
          page_source_sha256: "10".repeat(32),
          page_ordinal: 1,
          state: "selected",
          finding_id: MEMBERSHIP_FINDING_ID,
          active_decision_ids: [PREDECESSOR_DECISION_ID],
          source_evidence: SOURCE_EVIDENCE,
          candidates: CANDIDATES,
          decisions: [
            {
              record_kind: "accepted",
              decision_id: PREDECESSOR_DECISION_ID,
              document_instance_id: "reg_cert_instance_a",
              document_role: "机动车登记证书",
              actor: "t02-reviewer",
              reason_code: "MEMBERSHIP_SOURCE_VERIFIED",
              time: 100,
              cycle: 1,
              source_evidence: {
                ...SOURCE_EVIDENCE,
                candidate_claim_id: "s10_claim_a",
              },
              supersedes: [],
              status: "active",
            },
          ],
        },
        {
          attachment_id: "s10-attachment-2",
          page_source_sha256: "20".repeat(32),
          page_ordinal: 2,
          state: "unassigned",
          finding_id: "finding_s10panel0000000000000003",
          active_decision_ids: ["decision_s10panel_unassigned"],
          source_evidence: SOURCE_EVIDENCE,
          candidates: [CANDIDATES[0]],
          decisions: [
            {
              record_kind: "unassigned",
              decision_id: "decision_s10panel_unassigned",
              document_instance_id: null,
              document_role: null,
              actor: "t02-reviewer",
              reason_code: "MEMBERSHIP_PAGE_UNASSIGNED",
              time: 101,
              cycle: 1,
              source_evidence: {
                ...SOURCE_EVIDENCE,
                candidate_claim_id: "s10_claim_a",
              },
              supersedes: [],
              status: "active",
            },
          ],
        },
      ],
    });
    const router = fetchRouter({
      ...baseRoutes(),
      [`GET ${WORK_PATH}`]: () =>
        jsonResponse(
          claimedWorkPayload({
            evidence_revision: 2,
            command_context: SUCCESSOR_CONTEXT,
          }),
        ),
      [`GET ${WORKSPACE_PATH}`]: () => jsonResponse(membershipWorkspace),
      [`GET ${ROUTE_PATH}`]: () => jsonResponse(routePayload({ evidence_revision: 2 })),
      [`GET ${HISTORY_PATH}`]: () =>
        jsonResponse(
          historyPayload({
            runs: [
              {
                ...historyPayload().runs[0],
                evidence_revision: 2,
              },
            ],
            memberships: [
              {
                record_kind: "accepted",
                decision_id: PREDECESSOR_DECISION_ID,
                membership_id: "membership_s10panel_prior",
                page: {
                  attachment_id: "s10-attachment-1",
                  source_sha256: "10".repeat(32),
                  page_ordinal: 1,
                },
                actor: "t02-reviewer",
                reason_code: "MEMBERSHIP_SOURCE_VERIFIED",
                time: 100,
                cycle: 1,
                source_evidence: SOURCE_EVIDENCE,
                supersedes: [],
                status: "active",
                document_instance_id: "reg_cert_instance_a",
                document_role: "机动车登记证书",
              },
            ],
            membership_history: [
              {
                evidence_revision: 2,
                event_id: "evidence_s10panel",
                correction_id: "membership_s10panel_prior",
                decision_id: PREDECESSOR_DECISION_ID,
                candidate_claim_id: "s10_claim_a",
                attachment_id: "s10-attachment-1",
                page_source_sha256: "10".repeat(32),
                page_ordinal: 1,
                source_evidence: SOURCE_EVIDENCE,
                decision: "accept",
                document_instance_id: "reg_cert_instance_a",
                document_role: "机动车登记证书",
                reason_code: "MEMBERSHIP_SOURCE_VERIFIED",
                actor: "t02-reviewer",
                recorded_at: 100,
                cycle: 1,
                supersedes: [],
              },
            ],
          }),
        ),
      [`POST ${MEMBERSHIP_PATH}`]: () =>
        jsonResponse({
          status: "accepted",
          replayed: false,
          application_id: APP_ID,
          work_item_id: WORK_ID,
          correction_id: "membership_s10panel",
          membership_decision_id: "decision_s10panel",
          candidate_claim_id: "s10::claim_b",
          attachment_id: "s10-attachment-1",
          page_source_sha256: "10".repeat(32),
          page_ordinal: 1,
          decision: "accept",
          document_instance_id: "reg::cert_instance_b",
          document_role: "机动车登记证书",
          cycle: 1,
          invalidated_run_id: "run_t02panel",
          job_id: "job_s10panel",
          phase: "Assembly",
          route: "pending_check",
          lifecycle_revision: 7,
          evidence_revision: 3,
        }),
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    expect(screen.getByTestId("review-membership-ledger")).toHaveTextContent(
      "/pages/0",
    );
    expect(screen.getByTestId("review-membership-ledger")).toHaveTextContent(
      "周期 1",
    );
    expect(screen.getByTestId("review-history-memberships")).toHaveTextContent(
      "周期 1",
    );
    expect(
      screen.getByTestId("review-history-membership-corrections"),
    ).toHaveTextContent("周期 1");
    await userEvent.click(
      screen.getByRole("button", {
        name: "选择附件 s10-attachment-1 第 1 页",
      }),
    );
    await screen.findByTestId("review-membership-form");
    expect(screen.getByTestId("review-membership")).toHaveTextContent(
      "状态 已归属",
    );
    // An opened draft never preselects a candidate: the native select shows
    // an explicit disabled placeholder, submit stays disabled, and no
    // membership POST may leave before an explicit Reviewer choice.
    expect(
      screen.getByRole("combobox", { name: "候选实例" }),
    ).toHaveValue("");
    expect(
      within(screen.getByRole("combobox", { name: "候选实例" })).getByRole(
        "option",
        { name: "请选择候选实例" },
      ),
    ).toBeDisabled();
    expect(screen.getByTestId("review-membership-submit")).toBeDisabled();
    expect(
      router.calls.filter(
        (call) => call.method === "POST" && call.url === MEMBERSHIP_PATH,
      ),
    ).toHaveLength(0);
    await userEvent.click(
      screen.getByRole("button", {
        name: "选择附件 s10-attachment-2 第 2 页",
      }),
    );
    expect(screen.getByTestId("review-membership")).toHaveTextContent(
      "状态 显式未归集",
    );
    await userEvent.click(
      screen.getByRole("button", {
        name: "选择附件 s10-attachment-1 第 1 页",
      }),
    );
    const candidateSelect = screen.getByRole("combobox", {
      name: "候选实例",
    });
    const reasonSelect = screen.getByRole("combobox", { name: "原因" });
    expect(
      within(reasonSelect).queryByRole("option", {
        name: "MEMBERSHIP_PAGE_UNASSIGNED",
      }),
    ).not.toBeInTheDocument();
    await userEvent.selectOptions(candidateSelect, "s10::claim_b");
    await userEvent.click(screen.getByTestId("review-membership-submit"));
    await vi.waitFor(() =>
      expect(
        router.calls.some(
          (call) =>
            call.method === "POST" &&
            call.url === MEMBERSHIP_PATH,
        ),
      ).toBe(true),
    );
    const call = router.calls.find(
      (candidate) =>
        candidate.method === "POST" && candidate.url === MEMBERSHIP_PATH,
    );
    expect(call?.body).toEqual({
      application_id: APP_ID,
      expected_fence: 1,
      expected_context: SUCCESSOR_CONTEXT,
      idempotency_key: expect.any(String),
      membership: {
        schema_version: "page-membership-correction/2",
        finding_id: MEMBERSHIP_FINDING_ID,
        candidate_claim_id: "s10::claim_b",
        attachment_id: "s10-attachment-1",
        page_source_sha256: "10".repeat(32),
        page_ordinal: 1,
        source_evidence: SOURCE_EVIDENCE,
        expected_active_decision_ids: [PREDECESSOR_DECISION_ID],
        decision: "accept",
        document_instance_id: "reg::cert_instance_b",
        document_role: "机动车登记证书",
        reason_code: "MEMBERSHIP_SOURCE_VERIFIED",
      },
    });
  });

  it("claims with exactly the generated claim contract and acknowledges the accepted fence", async () => {
    let workRequests = 0;
    const routes = {
      ...baseRoutes(),
      [`GET ${WORK_PATH}`]: () =>
        jsonResponse(
          workRequests++ === 0 ? workPayload() : claimedWorkPayload(),
        ),
      [`POST ${CLAIM_PATH}`]: () =>
        jsonResponse({
          status: "claimed",
          application_id: APP_ID,
          work_item_id: WORK_ID,
          claim_subject: "t02-reviewer",
          claim_fence: 1,
          claim_expires_at: 1786000000,
        }),
    };
    const router = fetchRouter(routes);
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    await userEvent.click(screen.getByRole("button", { name: "认领" }));
    await waitFor(() =>
      expect(screen.getByTestId("review-command-status")).toHaveTextContent(
        "认领已接受",
      ),
    );
    expect(screen.getByTestId("review-claim-fence")).toHaveTextContent("1");
    const claimCall = router.calls.find((call) => call.method === "POST");
    expect(claimCall?.body).toEqual({ expected_context: CONTEXT });
    expect(Object.keys(claimCall?.body ?? {})).toEqual(["expected_context"]);
  });

  it("keeps the exact same body and idempotency key when the transport outcome is unknown", async () => {
    let posts = 0;
    const routes = {
      ...baseRoutes(),
      [`GET ${WORK_PATH}`]: () => jsonResponse(claimedWorkPayload()),
      [`GET ${WORKSPACE_PATH}`]: () => jsonResponse(claimedWorkspacePayload()),
      [`POST ${SUBMIT_PATH}`]: () => {
        posts += 1;
        if (posts === 1) {
          return Promise.reject(new TypeError("fetch failed: connection reset"));
        }
        return jsonResponse({
          status: "accepted",
          replayed: false,
          application_id: APP_ID,
          work_item_id: WORK_ID,
          decision_id: "decision_t02panel",
          claim_fence: 1,
          lifecycle_revision: 7,
          evidence_revision: 1,
          route: "human_complete",
        });
      },
    };
    const router = fetchRouter(routes);
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    await userEvent.click(screen.getByRole("button", { name: "提交人工核验" }));
    await waitFor(() =>
      expect(screen.getByTestId("review-command-status")).toHaveTextContent(
        "结果未知：网络未确认，重试将使用同一幂等键",
      ),
    );
    // While the outcome is unknown every write stays fenced, including claim.
    expect(screen.getByRole("button", { name: "认领" })).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "提交人工核验" }),
    ).toBeDisabled();
    const retry = screen.getByRole("button", { name: "重试" });
    await userEvent.click(retry);
    await waitFor(() =>
      expect(screen.getByTestId("review-command-status")).toHaveTextContent(
        "核验已接受",
      ),
    );
    expect(posts).toBe(2);
    const bodies = router.calls
      .filter((call) => call.method === "POST")
      .map((call) => call.body);
    expect(bodies).toHaveLength(2);
    expect(JSON.stringify(bodies[0])).toEqual(JSON.stringify(bodies[1]));
  });

  it("keeps the fenced key when a generic 503 leaves the submit outcome unknown", async () => {
    let posts = 0;
    const routes = {
      ...baseRoutes(),
      [`GET ${WORK_PATH}`]: () => jsonResponse(claimedWorkPayload()),
      [`GET ${WORKSPACE_PATH}`]: () => jsonResponse(claimedWorkspacePayload()),
      [`POST ${SUBMIT_PATH}`]: () => {
        posts += 1;
        if (posts === 1) {
          return jsonResponse(
            { detail: { message: "upstream gateway error" } },
            503,
          );
        }
        return jsonResponse({
          status: "accepted",
          replayed: false,
          application_id: APP_ID,
          work_item_id: WORK_ID,
          decision_id: "decision_t02panel",
          claim_fence: 1,
          lifecycle_revision: 7,
          evidence_revision: 1,
          route: "human_complete",
        });
      },
    };
    const router = fetchRouter(routes);
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    await userEvent.click(screen.getByRole("button", { name: "提交人工核验" }));
    await waitFor(() =>
      expect(screen.getByTestId("review-command-status")).toHaveTextContent(
        "结果未知：网络未确认，重试将使用同一幂等键",
      ),
    );
    await userEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() =>
      expect(screen.getByTestId("review-command-status")).toHaveTextContent(
        "核验已接受",
      ),
    );
    const bodies = router.calls
      .filter((call) => call.method === "POST")
      .map((call) => call.body as { idempotency_key: string });
    expect(bodies).toHaveLength(2);
    expect(bodies[0].idempotency_key).toBe(bodies[1].idempotency_key);
  });

  it("keeps an unknown non-restricted command when authoritative context changes", async () => {
    let workRequests = 0;
    let posts = 0;
    const changedContext = { ...CONTEXT, current_context: "c".repeat(64) };
    const router = fetchRouter({
      ...baseRoutes(),
      [`GET ${WORK_PATH}`]: () => {
        workRequests += 1;
        return jsonResponse(
          workPayload({
            status: "claimed",
            claim_subject: "t02-reviewer",
            claim_fence: 1,
            claim_expires_at: LIVE_CLAIM_EXPIRES_AT,
            command_context: workRequests >= 2 ? changedContext : CONTEXT,
          }),
        );
      },
      [`GET ${WORKSPACE_PATH}`]: () => jsonResponse(claimedWorkspacePayload()),
      [`POST ${SUBMIT_PATH}`]: () => {
        posts += 1;
        if (posts === 1) {
          return Promise.reject(new TypeError("fetch failed: connection reset"));
        }
        return jsonResponse({
          status: "accepted",
          replayed: true,
          application_id: APP_ID,
          work_item_id: WORK_ID,
          decision_id: "decision_t02panel",
          claim_fence: 1,
          lifecycle_revision: 7,
          evidence_revision: 1,
          route: "human_complete",
        });
      },
    });
    const { client } = renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    await userEvent.click(screen.getByRole("button", { name: "提交人工核验" }));
    await waitFor(() =>
      expect(screen.getByTestId("review-command-status")).toHaveTextContent(
        "结果未知：网络未确认，重试将使用同一幂等键",
      ),
    );
    const firstBody = router.calls.find(
      (call) => call.method === "POST",
    )?.body;
    await act(async () => {
      await client.refetchQueries({ queryKey: MANUAL_WORK_KEY(WORK_ID) });
    });
    const retry = screen.getByRole("button", { name: "重试" });
    await userEvent.click(retry);
    await waitFor(() =>
      expect(screen.getByTestId("review-command-status")).toHaveTextContent(
        "核验已接受",
      ),
    );
    const postBodies = router.calls
      .filter((call) => call.method === "POST")
      .map((call) => call.body);
    expect(postBodies.length).toBe(2);
    expect(JSON.stringify(postBodies[1])).toBe(JSON.stringify(firstBody));
  });

  it("locks every action after a definitive 409 and recovers only after a successful reload", async () => {
    let renewPosts = 0;
    const routes = {
      ...baseRoutes(),
      [`GET ${WORK_PATH}`]: () => jsonResponse(claimedWorkPayload()),
      [`GET ${WORKSPACE_PATH}`]: () => jsonResponse(claimedWorkspacePayload()),
      [`POST ${RENEW_PATH}`]: () => {
        renewPosts += 1;
        if (renewPosts === 1) {
          return jsonResponse(
            {
              detail: {
                error: "S03_STALE",
                reason_code: "STALE_WORK_ITEM_CLAIM",
              },
            },
            409,
          );
        }
        return jsonResponse({
          status: "renewed",
          application_id: APP_ID,
          work_item_id: WORK_ID,
          claim_subject: "t02-reviewer",
          claim_fence: 1,
          claim_expires_at: 1786000000,
          replayed: false,
        });
      },
    };
    const router = fetchRouter(routes);
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    await userEvent.click(screen.getByRole("button", { name: "续期" }));
    await waitFor(() =>
      expect(screen.getByTestId("review-command-status")).toHaveTextContent(
        "续期未接受（STALE_WORK_ITEM_CLAIM）：请重新加载权威上下文后再试",
      ),
    );
    for (const name of ["认领", "续期", "释放", "提交人工核验"]) {
      expect(screen.getByRole("button", { name })).toBeDisabled();
    }
    expect(
      screen.queryByRole("button", { name: "重试" }),
    ).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "重新加载" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "续期" })).toBeEnabled(),
    );
    expect(screen.getByTestId("review-command-status")).toHaveTextContent(
      "等待操作",
    );
    await userEvent.click(screen.getByRole("button", { name: "续期" }));
    await waitFor(() =>
      expect(screen.getByTestId("review-command-status")).toHaveTextContent(
        "续期已接受",
      ),
    );
    const keys = router.calls
      .filter((call) => call.method === "POST")
      .map(
        (call) => (call.body as { idempotency_key: string }).idempotency_key,
      );
    expect(keys).toHaveLength(2);
    // The proven-rejected key rotated exactly once, at the rejection.
    expect(keys[0]).not.toBe(keys[1]);
  });

  it("keeps every action fenced when the authoritative reload fails", async () => {
    let workRequests = 0;
    const routes = {
      ...baseRoutes(),
      [`GET ${WORK_PATH}`]: () => {
        workRequests += 1;
        if (workRequests === 1) return jsonResponse(claimedWorkPayload());
        return jsonResponse(
          { detail: { error: "S03_UNAVAILABLE" } },
          503,
        );
      },
      [`GET ${WORKSPACE_PATH}`]: () => jsonResponse(claimedWorkspacePayload()),
      [`POST ${RENEW_PATH}`]: () =>
        jsonResponse(
          {
            detail: {
              error: "S03_STALE",
              reason_code: "STALE_WORK_ITEM_CLAIM",
            },
          },
          409,
        ),
    };
    fetchRouter(routes);
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    await userEvent.click(screen.getByRole("button", { name: "续期" }));
    await waitFor(() =>
      expect(screen.getByTestId("review-command-status")).toHaveTextContent(
        "续期未接受",
      ),
    );
    await userEvent.click(screen.getByRole("button", { name: "重新加载" }));
    // The failed refetch must not clear the fence: the panel fails closed and
    // hides the previously confirmed DTO instead of re-enabling writes.  The
    // transient 503 is retried with backoff before the query fails closed.
    await waitFor(
      () =>
        expect(screen.getByTestId("review-error")).toHaveTextContent(
          "工作项不可用",
        ),
      { timeout: 8_000 },
    );
    expect(screen.queryByTestId("review-status")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "认领" })).not.toBeInTheDocument();
  });

  it("treats a structured 503 as a definitive rejection: claim is fenced until reload", async () => {
    let claimPosts = 0;
    const routes = {
      ...baseRoutes(),
      // The structured 503 proves no lease was created, so every refetch
      // keeps the work unclaimed until an actual claim succeeds.
      [`GET ${WORK_PATH}`]: () =>
        jsonResponse(claimPosts >= 2 ? claimedWorkPayload() : workPayload()),
      [`POST ${CLAIM_PATH}`]: () => {
        claimPosts += 1;
        if (claimPosts === 1) {
          return jsonResponse(
            {
              detail: {
                error: "S03_UNAVAILABLE",
                reason_code: "AUDIT_UNAVAILABLE",
              },
            },
            503,
          );
        }
        return jsonResponse({
          status: "claimed",
          application_id: APP_ID,
          work_item_id: WORK_ID,
          claim_subject: "t02-reviewer",
          claim_fence: 1,
          claim_expires_at: 1786000000,
        });
      },
    };
    fetchRouter(routes);
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    await userEvent.click(screen.getByRole("button", { name: "认领" }));
    await waitFor(() =>
      expect(screen.getByTestId("review-command-status")).toHaveTextContent(
        "认领未接受（AUDIT_UNAVAILABLE）：请重新加载权威上下文后再试",
      ),
    );
    expect(screen.getByTestId("review-reload-note")).toBeInTheDocument();
    // The structured 503 proves no lease was created: no retry, ordinary
    // claim stays fenced, and only an authoritative reload can recover.
    expect(
      screen.queryByRole("button", { name: "重试" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "认领" })).toBeDisabled();
    expect(screen.getByTestId("review-claim-fence")).toHaveTextContent("0");
    await userEvent.click(screen.getByRole("button", { name: "重新加载" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "认领" })).toBeEnabled(),
    );
    await userEvent.click(screen.getByRole("button", { name: "认领" }));
    await waitFor(() =>
      expect(screen.getByTestId("review-command-status")).toHaveTextContent(
        "认领已接受",
      ),
    );
    expect(claimPosts).toBe(2);
  });

  it("reconciles a lost claim through an authoritative refetch instead of a second POST", async () => {
    let workRequests = 0;
    const routes = {
      ...baseRoutes(),
      [`GET ${WORK_PATH}`]: () =>
        jsonResponse(
          workRequests++ === 0 ? workPayload() : claimedWorkPayload(),
        ),
      [`POST ${CLAIM_PATH}`]: () =>
        Promise.reject(new TypeError("fetch failed: connection reset")),
    };
    const router = fetchRouter(routes);
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    await userEvent.click(screen.getByRole("button", { name: "认领" }));
    await waitFor(() =>
      expect(screen.getByTestId("review-command-status")).toHaveTextContent(
        "结果未知：网络未确认，重试将使用同一幂等键",
      ),
    );
    // An unknown claim exposes only reconciliation: the ordinary claim button
    // must not be able to issue a second claim effect.
    expect(screen.getByRole("button", { name: "认领" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "重试" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() =>
      expect(screen.getByTestId("review-command-status")).toHaveTextContent(
        "认领已接受",
      ),
    );
    expect(screen.getByTestId("review-status")).toHaveTextContent("claimed");
    expect(screen.getByTestId("review-claim-fence")).toHaveTextContent("1");
    expect(router.calls.filter((call) => call.method === "POST").length).toBe(1);
  });

  it("submits the chosen explicit outcome for the overall and every finding decision", async () => {
    const routes = {
      ...baseRoutes(),
      [`GET ${WORK_PATH}`]: () => jsonResponse(claimedWorkPayload()),
      [`GET ${WORKSPACE_PATH}`]: () => jsonResponse(claimedWorkspacePayload()),
      [`POST ${SUBMIT_PATH}`]: () =>
        jsonResponse({
          status: "accepted",
          replayed: false,
          application_id: APP_ID,
          work_item_id: WORK_ID,
          decision_id: "decision_t02panel",
          claim_fence: 1,
          lifecycle_revision: 7,
          evidence_revision: 1,
          route: "human_complete",
        }),
    };
    const router = fetchRouter(routes);
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    const outcomeSelect = screen.getByTestId("review-outcome");
    expect(outcomeSelect).toHaveValue("confirmed");
    await userEvent.selectOptions(outcomeSelect, "inconclusive");
    await userEvent.click(screen.getByRole("button", { name: "提交人工核验" }));
    await waitFor(() =>
      expect(screen.getByTestId("review-command-status")).toHaveTextContent(
        "核验已接受",
      ),
    );
    const submitCall = router.calls.find((call) => call.method === "POST");
    const verification = (submitCall?.body as { verification: { outcome: string; finding_decisions: Array<{ outcome: string }> } })
      .verification;
    expect(verification.outcome).toBe("inconclusive");
    expect(verification.finding_decisions).toHaveLength(1);
    expect(verification.finding_decisions[0].outcome).toBe("inconclusive");
    // The submitted decision carries no automatic verdict/route/target.
    const serialized = JSON.stringify(verification);
    expect(serialized).not.toContain("verdict");
    expect(serialized).not.toContain("route");
    expect(serialized).not.toContain("target");
  });

  it("treats a structured 413 as a definitive pre-command rejection and fences claim", async () => {
    const routes = {
      ...baseRoutes(),
      [`POST ${CLAIM_PATH}`]: () =>
        jsonResponse(
          {
            detail: {
              error: "S03_COMMAND_TOO_LARGE",
              message: "S03 command exceeds the allowed size",
            },
          },
          413,
        ),
    };
    fetchRouter(routes);
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    await userEvent.click(screen.getByRole("button", { name: "认领" }));
    await waitFor(() =>
      expect(screen.getByTestId("review-command-status")).toHaveTextContent(
        "认领未接受（conflict）：请重新加载权威上下文后再试",
      ),
    );
    expect(screen.getByRole("button", { name: "认领" })).toBeDisabled();
    expect(
      screen.queryByRole("button", { name: "重试" }),
    ).not.toBeInTheDocument();
  });

  it("keeps the automatic finding untouched and exact before and after completion", async () => {
    const routes = {
      ...baseRoutes(),
      [`GET ${WORK_PATH}`]: () =>
        jsonResponse(
          workPayload({
            status: "completed",
            claim_subject: "t02-reviewer",
            claim_fence: 1,
            decision: { decision_id: "decision_t02panel" },
            decisions: [{ decision_id: "decision_t02panel" }],
            completed_finding_ids: [FINDING_ID],
          }),
        ),
      [`GET ${ROUTE_PATH}`]: () =>
        jsonResponse(
          routePayload({ phase: "Verification Completed", route: "human_complete" }),
        ),
      [`GET ${HISTORY_PATH}`]: () =>
        jsonResponse(
          historyPayload({
            runs: [
              {
                ...historyPayload().runs[0],
                decision_ids: ["decision_t02panel"],
              },
            ],
          }),
        ),
    };
    fetchRouter(routes);
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitFor(() =>
      expect(screen.getByTestId("review-status")).toHaveTextContent("completed"),
    );
    await screen.findByTestId("review-workspace-gone");
    // The automatic finding is the server's authority: its verdict is not
    // rewritten by the human workflow and remains visible as-is.
    expect(screen.getByTestId("review-finding-verdict")).toHaveTextContent(
      "uncertain",
    );
    await waitFor(() =>
      expect(screen.getByTestId("gate-phase")).toHaveTextContent(
        "Verification Completed",
      ),
    );
    expect(screen.getByTestId("gate-route")).toHaveTextContent(
      "human_complete",
    );
    expect(screen.getByTestId("review-history-decisions")).toHaveTextContent(
      "decision_t02panel",
    );
    expect(
      screen.queryByRole("button", { name: "认领" }),
    ).toBeDisabled();
  });

  it.each([
    ["workspace 404", WORKSPACE_PATH, 404, "S03_NOT_FOUND", "未找到或无权访问"],
    ["history 404", HISTORY_PATH, 404, "S03_NOT_FOUND", "未找到或无权访问"],
    ["route 404", ROUTE_PATH, 404, "S03_NOT_FOUND", "未找到或无权访问"],
    ["workspace 503", WORKSPACE_PATH, 503, "S03_UNAVAILABLE", "相关权威不可用"],
  ])(
    "fails closed and hides protected data when %s fails",
    async (_label, path, status, error, message) => {
      const router = fetchRouter({
        ...baseRoutes(),
        [`GET ${path}`]: () => jsonResponse({ detail: { error } }, status),
      });
      renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
      await waitFor(
        () =>
          expect(screen.getByTestId("review-error")).toHaveTextContent(message),
        { timeout: 8_000 },
      );
      expect(screen.queryByTestId("review-status")).not.toBeInTheDocument();
      expect(screen.queryByTestId("review-finding")).not.toBeInTheDocument();
      expect(screen.queryByTestId("review-claim-fence")).not.toBeInTheDocument();
      expect(
        screen.queryByTestId("review-evidence-provenance"),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByTestId("review-history-decisions"),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "认领" }),
      ).not.toBeInTheDocument();
      expect(router.calls.filter((call) => call.method === "POST")).toHaveLength(0);
    },
    10_000,
  );

  it("hides cached work data after a terminal 404 refetch", async () => {
    let workRequests = 0;
    fetchRouter({
      ...baseRoutes(),
      [`GET ${WORK_PATH}`]: () => {
        workRequests += 1;
        if (workRequests === 1) return jsonResponse(claimedWorkPayload());
        return jsonResponse({ detail: { error: "S03_NOT_FOUND" } }, 404);
      },
      [`GET ${WORKSPACE_PATH}`]: () => jsonResponse(claimedWorkspacePayload()),
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    expect(screen.getByTestId("review-status")).toHaveTextContent("claimed");
    await userEvent.click(screen.getByRole("button", { name: "重新加载" }));
    await waitFor(() =>
      expect(screen.getByTestId("review-error")).toHaveTextContent(
        "未找到或无权访问",
      ),
    );
    expect(screen.queryByTestId("review-status")).not.toBeInTheDocument();
    expect(screen.queryByTestId("review-claim-fence")).not.toBeInTheDocument();
  });
});

/** Claimed workspace with two source-backed evidence observations so the
 * reveal can be pinned to exactly one of them. */
function t03WorkspacePayload() {
  const base = workspacePayload();
  const link = base.selected_finding.evidence_links[0];
  return workspacePayload({
    claim_fence: 1,
    claim_expires_at: LIVE_CLAIM_EXPIRES_AT,
    selected_finding: {
      ...base.selected_finding,
      evidence_links: [
        link,
        {
          ...link,
          document_id: "pol",
          document_role: "行驶证",
          observation_id: "observation_t02panel2",
        },
      ],
    },
  });
}

/** Post-correction history: one superseded run and exactly one server-current
 * successor run plus the recorded correction. */
function t03HistoryPayload() {
  const first = historyPayload().runs[0];
  return historyPayload({
    current_run_id: "run_t03succ",
    runs: [
      {
        ...first,
        run_id: "run_t02panel",
        current: false,
        currentness_reason: "SUPERSEDED_BY_EVIDENCE_REVISION",
        evidence_revision: 1,
        decision_ids: [],
      },
      {
        ...first,
        run_id: "run_t03succ",
        current: true,
        currentness_reason: "CURRENT_CONTEXT_MATCH",
        evidence_revision: 2,
        decision_ids: [],
      },
    ],
    corrections: [
      {
        correction_id: "correction_t03panel",
        superseded_observation_id: "observation_t02panel",
        successor_observation_id: "observation_t02panel_succ",
        document_id: "reg",
        document_role: "机动车登记证书",
        field: "engine_no",
        reason_code: "SOURCE_VALUE_MISREAD",
        actor: "t03-reviewer",
        evidence_revision: 2,
        recorded_at: 1786000000,
        invalidated_decision_ids: [],
        invalidated_exception_ids: [],
        source_location: {
          source_sha256: "d".repeat(64),
          source_page: 1,
          source_region: "region:1",
        },
      },
    ],
  });
}

function revealResultPayload(overrides: Record<string, unknown> = {}) {
  return {
    status: "revealed",
    replayed: false,
    application_id: APP_ID,
    work_item_id: WORK_ID,
    observation_id: "observation_t02panel",
    source_location: {
      source_sha256: "d".repeat(64),
      source_page: 1,
      source_region: "region:1",
    },
    source_text: SOURCE_SENTINEL,
    revealed_at: 1786000000,
    ...overrides,
  };
}

function correctionResultPayload(overrides: Record<string, unknown> = {}) {
  return {
    status: "accepted",
    replayed: false,
    application_id: APP_ID,
    work_item_id: WORK_ID,
    correction_id: "correction_t03panel",
    // The server echoes the successor observation the rerun created, never
    // the superseded observation the command was issued against.
    observation_id: "observation_t02panel_succ",
    invalidated_run_id: "run_t02panel",
    job_id: "job_t03panel",
    phase: "Auto Complete",
    route: "auto_complete",
    lifecycle_revision: 7,
    evidence_revision: 2,
    invalidated_exception_ids: [],
    ...overrides,
  };
}

describe("ReviewWorkPanel S10 page-membership explicit choice (T10)", () => {
  const SUCCESSOR_CONTEXT = {
    ...CONTEXT,
    evidence_revision: 2,
    current_context: "9".repeat(64),
  };

  function s10MembershipRoutes(overrides: Record<string, RouteHandler> = {}) {
    return {
      [`GET ${WORK_PATH}`]: () =>
        jsonResponse(
          claimedWorkPayload({
            evidence_revision: 2,
            command_context: SUCCESSOR_CONTEXT,
          }),
        ),
      [`GET ${WORKSPACE_PATH}`]: () =>
        jsonResponse(s10MembershipWorkspacePayload()),
      [`GET ${ROUTE_PATH}`]: () =>
        jsonResponse(routePayload({ evidence_revision: 2 })),
      [`GET ${HISTORY_PATH}`]: () =>
        jsonResponse(
          historyPayload({
            runs: [
              {
                ...historyPayload().runs[0],
                evidence_revision: 2,
              },
            ],
          }),
        ),
      ...overrides,
    };
  }

  it("unassign keeps the candidate selector visible and binds the explicit claim", async () => {
    const router = fetchRouter({
      ...s10MembershipRoutes({
        [`POST ${MEMBERSHIP_PATH}`]: () =>
          jsonResponse({
            status: "accepted",
            replayed: false,
            application_id: APP_ID,
            work_item_id: WORK_ID,
            correction_id: "membership_s10panel_unassign",
            membership_decision_id: "decision_s10panel_unassign",
            candidate_claim_id: "s10::claim_b",
            attachment_id: "s10-attachment-1",
            page_source_sha256: "10".repeat(32),
            page_ordinal: 1,
            decision: "unassign",
            document_instance_id: null,
            document_role: null,
            cycle: 1,
            invalidated_run_id: "run_t02panel",
            job_id: "job_s10panel",
            phase: "Assembly",
            route: "pending_check",
            lifecycle_revision: 7,
            evidence_revision: 3,
          }),
      }),
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    await userEvent.click(
      screen.getByRole("button", {
        name: "选择附件 s10-attachment-1 第 1 页",
      }),
    );
    await screen.findByTestId("review-membership-form");
    await userEvent.click(screen.getByTestId("review-membership-unassign-radio"));
    // The unassign variant also binds candidate_claim_id, so the candidate
    // selector stays visible and starts on the empty disabled placeholder.
    const candidateSelect = screen.getByRole("combobox", {
      name: "候选实例",
    });
    expect(candidateSelect).toHaveValue("");
    expect(screen.getByTestId("review-membership-submit")).toBeDisabled();
    const reasonSelect = screen.getByRole("combobox", { name: "原因" });
    expect(
      within(reasonSelect).queryByRole("option", {
        name: "MEMBERSHIP_INSTANCE_WRONG",
      }),
    ).not.toBeInTheDocument();
    expect(
      within(reasonSelect).getByRole("option", {
        name: "MEMBERSHIP_PAGE_UNASSIGNED",
      }),
    ).toBeInTheDocument();
    await userEvent.selectOptions(candidateSelect, "s10::claim_b");
    await userEvent.selectOptions(reasonSelect, "MEMBERSHIP_PAGE_UNASSIGNED");
    await userEvent.click(screen.getByTestId("review-membership-submit"));
    await vi.waitFor(() =>
      expect(
        router.calls.some(
          (call) =>
            call.method === "POST" && call.url === MEMBERSHIP_PATH,
        ),
      ).toBe(true),
    );
    const call = router.calls.find(
      (candidate) =>
        candidate.method === "POST" && candidate.url === MEMBERSHIP_PATH,
    );
    expect(call?.body).toEqual({
      application_id: APP_ID,
      expected_fence: 1,
      expected_context: SUCCESSOR_CONTEXT,
      idempotency_key: expect.any(String),
      membership: {
        schema_version: "page-membership-correction/2",
        finding_id: S10_MEMBERSHIP_FINDING_ID,
        candidate_claim_id: "s10::claim_b",
        attachment_id: "s10-attachment-1",
        page_source_sha256: "10".repeat(32),
        page_ordinal: 1,
        source_evidence: S10_SOURCE_EVIDENCE,
        expected_active_decision_ids: [S10_PREDECESSOR_DECISION_ID],
        decision: "unassign",
        reason_code: "MEMBERSHIP_PAGE_UNASSIGNED",
      },
    });
  });

  it.each([
    [
      "command_context",
      MANUAL_WORK_KEY(WORK_ID),
      (counts: { work: number; workspace: number }) => ({
        [`GET ${WORK_PATH}`]: () => {
          counts.work += 1;
          return jsonResponse(
            counts.work >= 2
              ? claimedWorkPayload({
                  evidence_revision: 2,
                  command_context: {
                    ...SUCCESSOR_CONTEXT,
                    current_context: "8".repeat(64),
                  },
                })
              : claimedWorkPayload({
                  evidence_revision: 2,
                  command_context: SUCCESSOR_CONTEXT,
                }),
          );
        },
      }),
    ],
    [
      "claim fence",
      MANUAL_WORK_KEY(WORK_ID),
      (counts: { work: number; workspace: number }) => ({
        [`GET ${WORK_PATH}`]: () => {
          counts.work += 1;
          return jsonResponse(
            counts.work >= 2
              ? claimedWorkPayload({
                  evidence_revision: 2,
                  command_context: SUCCESSOR_CONTEXT,
                  claim_fence: 2,
                })
              : claimedWorkPayload({
                  evidence_revision: 2,
                  command_context: SUCCESSOR_CONTEXT,
                }),
          );
        },
      }),
    ],
    [
      "claim lease issuance",
      MANUAL_WORK_KEY(WORK_ID),
      (counts: { work: number; workspace: number }) => ({
        [`GET ${WORK_PATH}`]: () => {
          counts.work += 1;
          return jsonResponse(
            counts.work >= 2
              ? claimedWorkPayload({
                  evidence_revision: 2,
                  command_context: SUCCESSOR_CONTEXT,
                  claim_expires_at: Math.floor(Date.now() / 1000) + 1200,
                })
              : claimedWorkPayload({
                  evidence_revision: 2,
                  command_context: SUCCESSOR_CONTEXT,
                }),
          );
        },
      }),
    ],
    [
      "membership workspace facts",
      WORKSPACE_KEY(APP_ID),
      (counts: { work: number; workspace: number }) => ({
        [`GET ${WORKSPACE_PATH}`]: () => {
          counts.workspace += 1;
          const base = s10MembershipWorkspacePayload();
          const changedLedger = base.membership_ledger
            ? [
                {
                  ...base.membership_ledger[0],
                  candidates: [
                    {
                      ...S10_CANDIDATES[0],
                      claim_id: "s10_claim_a_refetched",
                    },
                    ...S10_CANDIDATES.slice(1),
                  ],
                },
                ...base.membership_ledger.slice(1),
              ]
            : [];
          return jsonResponse(
            counts.workspace >= 2
              ? s10MembershipWorkspacePayload({
                  membership_ledger: changedLedger,
                })
              : s10MembershipWorkspacePayload(),
          );
        },
      }),
    ],
  ])(
    "%s authoritative refetch closes the open membership draft with zero POSTs",
    async (_label, queryKey, buildOverrides) => {
      const counts = { work: 0, workspace: 0 };
      const router = fetchRouter({
        ...s10MembershipRoutes(buildOverrides(counts)),
      });
      const { client } = renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
      await waitForReviewReady();
      await userEvent.click(
        screen.getByRole("button", {
          name: "选择附件 s10-attachment-1 第 1 页",
        }),
      );
      await screen.findByTestId("review-membership-form");
      await userEvent.selectOptions(
        screen.getByRole("combobox", { name: "候选实例" }),
        "s10::claim_b",
      );
      // A successful authoritative refetch changes the issuing authority;
      // the open draft must close with the panel staying healthy and no POST
      // leaving the browser.
      await act(async () => {
        await client.refetchQueries({ queryKey });
      });
      await vi.waitFor(() =>
        expect(
          screen.queryByTestId("review-membership-form"),
        ).not.toBeInTheDocument(),
      );
      expect(screen.queryByTestId("review-error")).not.toBeInTheDocument();
      expect(
        router.calls.filter(
          (call) =>
            call.method === "POST" && call.url === MEMBERSHIP_PATH,
        ),
      ).toHaveLength(0);
      // A freshly opened draft starts from the disabled empty placeholder.
      await userEvent.click(
        screen.getByRole("button", {
          name: "选择附件 s10-attachment-1 第 1 页",
        }),
      );
      await screen.findByTestId("review-membership-form");
      expect(
        screen.getByRole("combobox", { name: "候选实例" }),
      ).toHaveValue("");
      expect(screen.getByTestId("review-membership-submit")).toBeDisabled();
      expect(
        router.calls.filter(
          (call) =>
            call.method === "POST" && call.url === MEMBERSHIP_PATH,
        ),
      ).toHaveLength(0);
    },
  );

  it("expiry closes an open membership draft and a renewed claim never reopens it", async () => {
    const nearExpiry = Math.floor(Date.now() / 1000) + 3;
    const renewedExpiry = Math.floor(Date.now() / 1000) + 1800;
    let workRequests = 0;
    const router = fetchRouter({
      ...s10MembershipRoutes({
        [`GET ${WORK_PATH}`]: () => {
          workRequests += 1;
          return jsonResponse(
            claimedWorkPayload({
              evidence_revision: 2,
              command_context: SUCCESSOR_CONTEXT,
              claim_expires_at:
                workRequests >= 2 ? renewedExpiry : nearExpiry,
            }),
          );
        },
        [`POST ${RENEW_PATH}`]: () =>
          jsonResponse({
            status: "renewed",
            replayed: false,
            application_id: APP_ID,
            work_item_id: WORK_ID,
            claim_subject: "t02-reviewer",
            claim_fence: 1,
            claim_expires_at: renewedExpiry,
          }),
      }),
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    await userEvent.click(
      screen.getByRole("button", {
        name: "选择附件 s10-attachment-1 第 1 页",
      }),
    );
    await screen.findByTestId("review-membership-form");
    // The one expiry clock fires without navigation: the open draft closes
    // and the panel enters the explicit reload-required state with the
    // membership pane, live status region, and reload note all naming the
    // expiry. No membership POST may leave under the expired authority.
    await vi.waitFor(
      () =>
        expect(
          screen.queryByTestId("review-membership-form"),
        ).not.toBeInTheDocument(),
      { timeout: 8_000 },
    );
    expect(screen.getByTestId("review-command-status")).toHaveTextContent(
      "认领已过期：请重新加载权威上下文后再继续",
    );
    expect(screen.getByTestId("review-membership-expired")).toHaveTextContent(
      "认领已过期，请重新加载权威上下文",
    );
    expect(screen.getByTestId("review-reload-note")).toBeVisible();
    // Expiry is an explicit reload-required boundary: every write control
    // stays disabled (commandBlocked includes requiresReload) until the
    // Reviewer activates the public reload action.
    for (const name of ["认领", "续期", "释放", "提交人工核验"]) {
      expect(screen.getByRole("button", { name })).toBeDisabled();
    }
    await userEvent.click(screen.getByTestId("reload-button"));
    await vi.waitFor(() =>
      expect(
        screen.queryByTestId("review-membership-form"),
      ).not.toBeInTheDocument(),
    );
    // The authoritative reload clears the latch; the expired draft stays
    // closed and a next valid draft starts from the empty placeholder.
    await vi.waitFor(() =>
      expect(screen.getByRole("button", { name: "续期" })).toBeEnabled(),
    );
    await userEvent.click(
      screen.getByRole("button", {
        name: "选择附件 s10-attachment-1 第 1 页",
      }),
    );
    await screen.findByTestId("review-membership-form");
    expect(
      screen.getByRole("combobox", { name: "候选实例" }),
    ).toHaveValue("");
    expect(
      router.calls.filter(
        (call) =>
          call.method === "POST" && call.url === MEMBERSHIP_PATH,
      ),
    ).toHaveLength(0);
  });

  it.each([
    ["404 existence-hidden", 404, "S03_NOT_FOUND", "S03_NOT_FOUND"],
    ["409 stale or conflicting", 409, "S03_STALE", "STALE_WORK_ITEM_CLAIM"],
    ["503 structured unavailable", 503, "S03_UNAVAILABLE", "AUDIT_UNAVAILABLE"],
  ])(
    "%s locks membership into reload-required without an unknown-outcome retry",
    async (_label, status, error, reason) => {
      const router = fetchRouter({
        ...s10MembershipRoutes({
          [`POST ${MEMBERSHIP_PATH}`]: () =>
            jsonResponse({ detail: { error, reason_code: reason } }, status),
        }),
      });
      renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
      await waitForReviewReady();
      await userEvent.click(
        screen.getByRole("button", {
          name: "选择附件 s10-attachment-1 第 1 页",
        }),
      );
      await screen.findByTestId("review-membership-form");
      await userEvent.selectOptions(
        screen.getByRole("combobox", { name: "候选实例" }),
        "s10::claim_b",
      );
      await userEvent.click(screen.getByTestId("review-membership-submit"));
      await waitFor(() =>
        expect(screen.getByTestId("review-command-status")).toHaveTextContent(
          `页归属未接受（${reason}）：请重新加载权威上下文后再试`,
        ),
      );
      expect(screen.getByTestId("review-reload-note")).toBeInTheDocument();
      // A definitive outcome is proof the command was never accepted: no
      // unknown-outcome retry, the rejected draft is scrubbed, and every
      // write stays fenced until an authoritative reload.
      expect(
        screen.queryByRole("button", { name: "重试" }),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByTestId("review-membership-form"),
      ).not.toBeInTheDocument();
      for (const name of ["认领", "续期", "释放", "提交人工核验"]) {
        expect(screen.getByRole("button", { name })).toBeDisabled();
      }
      await userEvent.click(screen.getByRole("button", { name: "重新加载" }));
      await waitFor(() =>
        expect(screen.getByRole("button", { name: "续期" })).toBeEnabled(),
      );
      expect(
        router.calls.filter(
          (call) =>
            call.method === "POST" && call.url === MEMBERSHIP_PATH,
        ),
      ).toHaveLength(1);
    },
  );

  it("keeps the exact membership body and idempotency key when the transport outcome is unknown", async () => {
    let posts = 0;
    const router = fetchRouter({
      ...s10MembershipRoutes({
        [`POST ${MEMBERSHIP_PATH}`]: () => {
          posts += 1;
          if (posts === 1) {
            return Promise.reject(new TypeError("network unreachable"));
          }
          return jsonResponse({
            status: "accepted",
            replayed: false,
            application_id: APP_ID,
            work_item_id: WORK_ID,
            correction_id: "membership_s10panel",
            membership_decision_id: "decision_s10panel",
            candidate_claim_id: "s10::claim_b",
            attachment_id: "s10-attachment-1",
            page_source_sha256: "10".repeat(32),
            page_ordinal: 1,
            decision: "accept",
            document_instance_id: "reg::cert_instance_b",
            document_role: "机动车登记证书",
            cycle: 1,
            invalidated_run_id: "run_t02panel",
            job_id: "job_s10panel",
            phase: "Assembly",
            route: "pending_check",
            lifecycle_revision: 7,
            evidence_revision: 3,
          });
        },
      }),
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    await userEvent.click(
      screen.getByRole("button", {
        name: "选择附件 s10-attachment-1 第 1 页",
      }),
    );
    await screen.findByTestId("review-membership-form");
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "候选实例" }),
      "s10::claim_b",
    );
    await userEvent.click(screen.getByTestId("review-membership-submit"));
    await waitFor(() =>
      expect(screen.getByTestId("review-command-status")).toHaveTextContent(
        "结果未知：网络未确认，重试将使用同一幂等键",
      ),
    );
    await userEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() =>
      expect(screen.getByTestId("review-command-status")).toHaveTextContent(
        "页归属已接受",
      ),
    );
    const bodies = router.calls
      .filter(
        (call) =>
          call.method === "POST" && call.url === MEMBERSHIP_PATH,
      )
      .map((call) => call.body);
    expect(bodies).toHaveLength(2);
    // The replay reuses the byte-identical serialized command, including the
    // retained idempotency key.
    expect(JSON.stringify(bodies[0])).toEqual(JSON.stringify(bodies[1]));
  });
});

describe("ReviewWorkPanel controlled reveal (T03)", () => {
  it("reveals the restricted source for exactly the clicked observation and nowhere else", async () => {
    const router = fetchRouter({
      ...baseRoutes(),
      [`GET ${WORK_PATH}`]: () => jsonResponse(claimedWorkPayload()),
      [`GET ${WORKSPACE_PATH}`]: () => jsonResponse(t03WorkspacePayload()),
      [`POST ${REVEAL_PATH}`]: () => jsonResponse(revealResultPayload()),
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    expect(screen.getAllByRole("button", { name: "查看来源" })).toHaveLength(2);
    // Every source-backed observation carries its own correction control;
    // the reveal stays restricted to the explicitly revealed observation.
    expect(
      screen.getAllByRole("button", { name: "更正该字段" }),
    ).toHaveLength(2);
    const maskedEvidence = screen.getAllByTestId("review-evidence-masked");
    await userEvent.click(screen.getAllByRole("button", { name: "查看来源" })[0]);
    const sources = await findRestrictedElements("review-reveal-source");
    expect(sources.length).toBe(1);
    expectRestrictedEqual(sources[0].textContent, SOURCE_SENTINEL);
    // The restricted value exists in exactly one element of the whole panel.
    const panelText = restrictedElement("review-panel").textContent ?? "";
    expect(panelText.split(SOURCE_SENTINEL).length - 1).toBe(1);
    // The evidence stays masked for both observations.
    for (const masked of maskedEvidence) {
      expect(masked).toHaveTextContent("[REDACTED]");
    }
    const revealCall = router.calls.find((call) => call.method === "POST");
    expect(revealCall?.body).toEqual({
      application_id: APP_ID,
      observation_id: "observation_t02panel",
      expected_fence: 1,
      expected_context: CONTEXT,
      idempotency_key: expect.any(String),
    });
    expect(Object.keys(revealCall?.body ?? {})).toEqual([
      "application_id",
      "observation_id",
      "expected_fence",
      "expected_context",
      "idempotency_key",
    ]);
  });

  it("keeps the source masked while a reveal is in flight and renders only on the authorized response", async () => {
    let resolveReveal: ((response: Response) => void) | undefined;
    fetchRouter({
      ...baseRoutes(),
      [`GET ${WORK_PATH}`]: () => jsonResponse(claimedWorkPayload()),
      [`GET ${WORKSPACE_PATH}`]: () => jsonResponse(t03WorkspacePayload()),
      [`POST ${REVEAL_PATH}`]: () =>
        new Promise<Response>((resolve) => {
          resolveReveal = resolve;
        }),
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    await userEvent.click(screen.getAllByRole("button", { name: "查看来源" })[0]);
    // Every evidence link shows the in-flight reveal marker; no source may
    // render for any of them until the authorized response arrives.
    const pendingMarkers = await screen.findAllByTestId("review-reveal-pending");
    expect(pendingMarkers.length).toBeGreaterThanOrEqual(2);
    expectRestrictedElementsAbsent("review-reveal-source");
    expectRestrictedAbsent(
      restrictedElement("review-panel").textContent,
      SOURCE_SENTINEL,
    );
    await act(async () => {
      resolveReveal?.(
        new Response(JSON.stringify(revealResultPayload()), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    });
    const sources = await findRestrictedElements("review-reveal-source");
    expect(sources.length).toBe(1);
    expectRestrictedEqual(sources[0].textContent, SOURCE_SENTINEL);
  });

  it("scrubs the restricted reveal at an authoritative reload and re-reveals with a fresh key", async () => {
    let revealPosts = 0;
    const router = fetchRouter({
      ...baseRoutes(),
      [`GET ${WORK_PATH}`]: () => jsonResponse(claimedWorkPayload()),
      [`GET ${WORKSPACE_PATH}`]: () => jsonResponse(t03WorkspacePayload()),
      [`POST ${REVEAL_PATH}`]: () => {
        revealPosts += 1;
        return jsonResponse(revealResultPayload());
      },
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    const reloadButton = screen.getByRole("button", { name: "重新加载" });
    await userEvent.click(screen.getAllByRole("button", { name: "查看来源" })[0]);
    await findRestrictedElements("review-reveal-source");
    await userEvent.click(reloadButton);
    await vi.waitFor(() =>
      expectRestrictedElementsAbsent("review-reveal-source"),
    );
    expectRestrictedAbsent(
      restrictedElement("review-panel").textContent,
      SOURCE_SENTINEL,
    );
    await userEvent.click(screen.getAllByRole("button", { name: "查看来源" })[0]);
    await findRestrictedElements("review-reveal-source");
    expect(revealPosts).toBe(2);
    const keys = router.calls
      .filter((call) => call.method === "POST")
      .map((call) => (call.body as { idempotency_key: string }).idempotency_key);
    expect(keys).toHaveLength(2);
    // The accepted reveal rotated its semantic key; the re-issued reveal is a
    // new logical command with a fresh key.
    expect(keys[0]).not.toBe(keys[1]);
  });

  it("scrubs a saved reveal before a release response settles", async () => {
    let workRequests = 0;
    let resolveRelease: ((response: Response) => void) | undefined;
    const router = fetchRouter({
      ...baseRoutes(),
      [`GET ${WORK_PATH}`]: () =>
        jsonResponse(
          workRequests++ === 0 ? claimedWorkPayload() : workPayload(),
        ),
      [`GET ${WORKSPACE_PATH}`]: () => jsonResponse(t03WorkspacePayload()),
      [`POST ${REVEAL_PATH}`]: () => jsonResponse(revealResultPayload()),
      [`POST ${RELEASE_PATH}`]: () =>
        new Promise<Response>((resolve) => {
          resolveRelease = resolve;
        }),
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    const releaseButton = screen.getByRole("button", { name: "释放" });
    await userEvent.click(screen.getAllByRole("button", { name: "查看来源" })[0]);
    const [revealed] = await findRestrictedElements("review-reveal-source");
    expectRestrictedEqual(
      revealed.textContent,
      SOURCE_SENTINEL,
    );
    await userEvent.click(releaseButton);
    // The command boundary itself scrubs the value; no response or refetch is
    // needed to end the restricted lifetime.
    expectRestrictedElementsAbsent("review-reveal-source");
    expectRestrictedAbsent(
      restrictedElement("review-panel").textContent,
      SOURCE_SENTINEL,
    );
    await act(async () => {
      resolveRelease?.(
        jsonResponse({
          status: "released",
          replayed: false,
          application_id: APP_ID,
          work_item_id: WORK_ID,
          claim_fence: 0,
        }),
      );
    });
    await waitFor(() =>
      expect(screen.getByTestId("review-status")).toHaveTextContent("unclaimed"),
    );
    expect(router.calls.filter((call) => call.method === "POST").length).toBe(2);
  });

  it("scrubs a correction draft before a manual-submit response settles", async () => {
    let resolveSubmit: ((response: Response) => void) | undefined;
    fetchRouter({
      ...baseRoutes(),
      [`GET ${WORK_PATH}`]: () => jsonResponse(claimedWorkPayload()),
      [`GET ${WORKSPACE_PATH}`]: () => jsonResponse(t03WorkspacePayload()),
      [`POST ${SUBMIT_PATH}`]: () =>
        new Promise<Response>((resolve) => {
          resolveSubmit = resolve;
        }),
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    const submitDecision = screen.getByRole("button", {
      name: "提交人工核验",
    });
    await userEvent.click(
      screen.getAllByRole("button", { name: "更正该字段" })[0],
    );
    const rawInput = screen.getByTestId("review-correction-raw");
    await userEvent.type(rawInput, CORRECTION_SENTINEL);
    await userEvent.click(submitDecision);
    expectRestrictedElementsAbsent("review-correction-form");
    expectRestrictedAbsent(
      restrictedElement("review-panel").textContent,
      CORRECTION_SENTINEL,
    );
    await act(async () => {
      resolveSubmit?.(
        jsonResponse({
          status: "accepted",
          replayed: false,
          application_id: APP_ID,
          work_item_id: WORK_ID,
          decision_id: "decision_t03panel",
          claim_fence: 1,
          lifecycle_revision: 7,
          evidence_revision: 1,
          route: "human_complete",
        }),
      );
    });
    await waitFor(() =>
      expect(screen.getByTestId("review-command-status")).toHaveTextContent(
        "核验已接受",
      ),
    );
  });

  it("scrubs restricted state on owning-read access loss and never restores it", async () => {
    let workspaceRequests = 0;
    const router = fetchRouter({
      ...baseRoutes(),
      [`GET ${WORK_PATH}`]: () => jsonResponse(claimedWorkPayload()),
      [`GET ${WORKSPACE_PATH}`]: () => {
        workspaceRequests += 1;
        return workspaceRequests === 2
          ? jsonResponse({ detail: { error: "S03_NOT_FOUND" } }, 404)
          : jsonResponse(t03WorkspacePayload());
      },
      [`POST ${REVEAL_PATH}`]: () => jsonResponse(revealResultPayload()),
    });
    const { client } = renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    await userEvent.click(screen.getAllByRole("button", { name: "查看来源" })[0]);
    const [revealed] = await findRestrictedElements("review-reveal-source");
    expectRestrictedEqual(revealed.textContent, SOURCE_SENTINEL);
    await act(async () => {
      await client.refetchQueries({ queryKey: WORKSPACE_KEY(APP_ID) });
    });
    await findRestrictedElements("review-error");
    await act(async () => {
      await client.refetchQueries({ queryKey: WORKSPACE_KEY(APP_ID) });
    });
    await waitForReviewReady();
    expectRestrictedElementsAbsent("review-reveal-source");
    expectRestrictedAbsent(
      restrictedElement("review-panel").textContent,
      SOURCE_SENTINEL,
    );
    const cached = client
      .getMutationCache()
      .getAll()
      .map((mutation) => JSON.stringify(mutation.state));
    expectRestrictedAbsent(cached.join("\n"), SOURCE_SENTINEL);
    expect(router.calls.filter((call) => call.method === "POST").length).toBe(1);
  });

  it("expiry scrubs the saved reveal and every restricted control without navigation", async () => {
    const nearExpiry = Math.floor(Date.now() / 1000) + 3;
    const router = fetchRouter({
      ...baseRoutes(),
      [`GET ${WORK_PATH}`]: () =>
        jsonResponse(
          workPayload({
            status: "claimed",
            claim_subject: "t02-reviewer",
            claim_fence: 1,
            claim_expires_at: nearExpiry,
          }),
        ),
      [`GET ${WORKSPACE_PATH}`]: () => jsonResponse(t03WorkspacePayload()),
      [`POST ${REVEAL_PATH}`]: () => jsonResponse(revealResultPayload()),
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    await userEvent.click(screen.getAllByRole("button", { name: "查看来源" })[0]);
    await findRestrictedElements("review-reveal-source");
    // The one expiry clock fires without navigation and drops the restricted
    // reveal, the correction draft, and the token.
    await vi.waitFor(
      () => expectRestrictedElementsAbsent("review-reveal-source"),
      { timeout: 8_000 },
    );
    expectRestrictedAbsent(
      restrictedElement("review-panel").textContent,
      SOURCE_SENTINEL,
    );
    expect(router.calls.filter((call) => call.method === "POST").length).toBe(1);
  });

  it("discards a late reveal response that resolves after the claim expires", async () => {
    const nearExpiry = Math.floor(Date.now() / 1000) + 3;
    let resolveReveal: ((response: Response) => void) | undefined;
    const router = fetchRouter({
      ...baseRoutes(),
      [`GET ${WORK_PATH}`]: () =>
        jsonResponse(
          workPayload({
            status: "claimed",
            claim_subject: "t02-reviewer",
            claim_fence: 1,
            claim_expires_at: nearExpiry,
          }),
        ),
      [`GET ${WORKSPACE_PATH}`]: () => jsonResponse(t03WorkspacePayload()),
      [`POST ${REVEAL_PATH}`]: () =>
        new Promise<Response>((resolve) => {
          resolveReveal = resolve;
        }),
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    await userEvent.click(screen.getAllByRole("button", { name: "查看来源" })[0]);
    // The reveal was issued under a live (unexpired) token.
    await waitFor(() =>
      expect(router.calls.filter((call) => call.method === "POST").length).toBe(1),
    );
    // Let the expiry clock run out with the reveal still in flight; the
    // authorization dies, the pending command is dropped, and the late
    // success must be discarded before storage.
    await new Promise((resolve) => setTimeout(resolve, 3_500));
    await act(async () => {
      resolveReveal?.(
        new Response(JSON.stringify(revealResultPayload()), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    });
    // The late success was discarded before storage: the restricted text
    // never renders and the panel holds no pending reveal.
    expectRestrictedElementsAbsent("review-reveal-source");
    expectRestrictedAbsent(
      restrictedElement("review-panel").textContent,
      SOURCE_SENTINEL,
    );
    expect(
      screen.queryByTestId("review-reveal-pending"),
    ).not.toBeInTheDocument();
    expect(router.calls.filter((call) => call.method === "POST").length).toBe(1);
  });

  it("an expired claim disables every restricted control and cannot issue a reveal", async () => {
    const router = fetchRouter({
      ...baseRoutes(),
      [`GET ${WORK_PATH}`]: () =>
        jsonResponse(
          workPayload({
            status: "claimed",
            claim_subject: "t02-reviewer",
            claim_fence: 1,
            claim_expires_at: 0,
          }),
        ),
      [`GET ${WORKSPACE_PATH}`]: () => jsonResponse(t03WorkspacePayload()),
      [`POST ${REVEAL_PATH}`]: () => jsonResponse(revealResultPayload()),
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    for (const button of screen.getAllByRole("button", { name: "查看来源" })) {
      expect(button).toBeDisabled();
    }
    for (const button of screen.getAllByRole("button", { name: "更正该字段" })) {
      expect(button).toBeDisabled();
    }
    await userEvent.click(screen.getAllByRole("button", { name: "查看来源" })[0]);
    expectRestrictedElementsAbsent("review-reveal-source");
    expect(router.calls.filter((call) => call.method === "POST").length).toBe(0);
  });

  it("a same-fence renewal with a new claim expiry scrubs the saved reveal and draft", async () => {
    let workRequests = 0;
    const renewedExpiry = Math.floor(Date.now() / 1000) + 1800;
    const router = fetchRouter({
      ...baseRoutes(),
      [`GET ${WORK_PATH}`]: () => {
        workRequests += 1;
        return jsonResponse(
          workPayload({
            status: "claimed",
            claim_subject: "t02-reviewer",
            claim_fence: 1,
            claim_expires_at:
              workRequests >= 2 ? renewedExpiry : LIVE_CLAIM_EXPIRES_AT,
          }),
        );
      },
      [`GET ${WORKSPACE_PATH}`]: () => jsonResponse(t03WorkspacePayload()),
      [`POST ${REVEAL_PATH}`]: () => jsonResponse(revealResultPayload()),
      [`POST ${RENEW_PATH}`]: () =>
        jsonResponse({
          status: "renewed",
          replayed: false,
          application_id: APP_ID,
          work_item_id: WORK_ID,
          claim_subject: "t02-reviewer",
          claim_fence: 1,
          claim_expires_at: renewedExpiry,
        }),
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    const correctButton = screen.getAllByRole("button", {
      name: "更正该字段",
    })[0];
    const renewButton = screen.getByRole("button", { name: "续期" });
    await userEvent.click(screen.getAllByRole("button", { name: "查看来源" })[0]);
    await findRestrictedElements("review-reveal-source");
    await userEvent.click(correctButton);
    expect(restrictedElements("review-correction-form").length).toBe(1);
    // The renewal extends the claim expiry without touching the fence: the
    // issued authorization (which includes the claim expiry) is no longer the
    // live one, so every restricted holder is scrubbed.
    await userEvent.click(renewButton);
    await vi.waitFor(() =>
      expectRestrictedElementsAbsent("review-reveal-source"),
    );
    expectRestrictedElementsAbsent("review-correction-form");
    expectRestrictedAbsent(
      restrictedElement("review-panel").textContent,
      SOURCE_SENTINEL,
    );
    expect(router.calls.filter((call) => call.method === "POST").length).toBe(2);
  });

  it("a command_context-only change ends an unknown replay, removes retry, and clears its raw", async () => {
    let workRequests = 0;
    const changedContext = { ...CONTEXT, current_context: "b".repeat(64) };
    const router = fetchRouter({
      ...baseRoutes(),
      [`GET ${WORK_PATH}`]: () => {
        workRequests += 1;
        return jsonResponse(
          workPayload({
            status: "claimed",
            claim_subject: "t02-reviewer",
            claim_fence: 1,
            claim_expires_at: LIVE_CLAIM_EXPIRES_AT,
            command_context:
              workRequests >= 2 ? changedContext : CONTEXT,
          }),
        );
      },
      [`GET ${WORKSPACE_PATH}`]: () => jsonResponse(t03WorkspacePayload()),
      [`POST ${CORRECT_PATH}`]: () =>
        Promise.reject(new TypeError("fetch failed: connection reset")),
    });
    const { client } = renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    await userEvent.click(
      screen.getAllByRole("button", { name: "更正该字段" })[0],
    );
    const submit = screen.getByRole("button", { name: "提交修正" });
    await userEvent.type(
      screen.getByTestId("review-correction-raw"),
      CORRECTION_SENTINEL,
    );
    await userEvent.click(submit);
    await vi.waitFor(() =>
      expect(
        (restrictedElement("review-command-status").textContent ?? "").includes(
          "结果未知：网络未确认，重试将使用同一幂等键",
        ),
      ).toBe(true),
    );
    expect(restrictedElements("retry-button").length).toBe(1);
    // An authoritative context change (only the command context differs)
    // while the outcome is unknown must end the replay and clear its raw.
    await act(async () => {
      await client.refetchQueries({ queryKey: MANUAL_WORK_KEY(WORK_ID) });
    });
    await vi.waitFor(() =>
      expect(restrictedElements("retry-button").length).toBe(0),
    );
    expect(restrictedElements("review-correction-pending").length).toBe(0);
    expectRestrictedAbsent(
      restrictedElement("review-panel").textContent,
      CORRECTION_SENTINEL,
    );
    const cached = client
      .getMutationCache()
      .getAll()
      .map((mutation) => JSON.stringify(mutation.state.variables ?? {}));
    expectRestrictedAbsent(cached.join("\n"), CORRECTION_SENTINEL);
    expect(router.calls.filter((call) => call.method === "POST").length).toBe(1);
  });

  it("a reclaimed issuance replaces a deferred reveal A; A is discarded and B alone renders", async () => {
    let workRequests = 0;
    const pending: Array<(response: Response) => void> = [];
    const router = fetchRouter({
      ...baseRoutes(),
      [`GET ${WORK_PATH}`]: () => {
        workRequests += 1;
        return jsonResponse(
          workPayload({
            status: "claimed",
            claim_subject: "t02-reviewer",
            claim_fence: workRequests >= 2 ? 2 : 1,
            claim_expires_at: LIVE_CLAIM_EXPIRES_AT,
          }),
        );
      },
      [`GET ${WORKSPACE_PATH}`]: () => jsonResponse(t03WorkspacePayload()),
      [`POST ${REVEAL_PATH}`]: () =>
        new Promise<Response>((resolve) => {
          pending.push(resolve);
        }),
    });
    const { client } = renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    await userEvent.click(restrictedButton("查看来源"));
    const pendingMarkers = await findRestrictedElements("review-reveal-pending");
    expect(pendingMarkers.length).toBeGreaterThanOrEqual(2);
    expect(pending).toHaveLength(1);
    // Reclaim advances the fence: an authoritative refetch invalidates the
    // first issuance so a second reveal can be issued under the new context.
    await act(async () => {
      await client.refetchQueries({ queryKey: MANUAL_WORK_KEY(WORK_ID) });
    });
    await vi.waitFor(() =>
      expect(restrictedElements("review-reveal-pending").length).toBe(0),
    );
    await userEvent.click(restrictedButton("查看来源"));
    await vi.waitFor(() => expect(pending.length).toBe(2));
    // Resolve the first (superseded) issuance first: it must be discarded
    // before storage, then the current issuance alone may render.
    await act(async () => {
      pending[0](
        new Response(JSON.stringify(revealResultPayload()), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    });
    expectRestrictedElementsAbsent("review-reveal-source");
    expect(restrictedElements("review-reveal-pending").length).toBeGreaterThan(0);
    expect(
      (restrictedElement("review-command-status").textContent ?? "").includes(
        "揭示提交中",
      ),
    ).toBe(true);
    await act(async () => {
      pending[1](
        new Response(JSON.stringify(revealResultPayload()), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    });
    const sources = await findRestrictedElements("review-reveal-source");
    expect(sources.length).toBe(1);
    expectRestrictedEqual(sources[0].textContent, SOURCE_SENTINEL);
    // The superseded issuance left nothing behind: no restricted value in
    // the MutationCache and no second reveal source anywhere.
    const cached = client
      .getMutationCache()
      .getAll()
      .map((mutation) => JSON.stringify(mutation.state.variables ?? {}));
    expectRestrictedAbsent(cached.join("\n"), SOURCE_SENTINEL);
    expect(router.calls.filter((call) => call.method === "POST").length).toBe(2);
  });

  it("keeps correction B pending when superseded correction A settles late", async () => {
    let workRequests = 0;
    const pending: Array<(response: Response) => void> = [];
    const router = fetchRouter({
      ...baseRoutes(),
      [`GET ${WORK_PATH}`]: () => {
        workRequests += 1;
        return jsonResponse(
          workPayload({
            status: "claimed",
            claim_subject: "t02-reviewer",
            claim_fence: workRequests >= 2 ? 2 : 1,
            claim_expires_at: LIVE_CLAIM_EXPIRES_AT,
          }),
        );
      },
      [`GET ${WORKSPACE_PATH}`]: () => jsonResponse(t03WorkspacePayload()),
      [`GET ${ROUTE_PATH}`]: () =>
        jsonResponse(
          routePayload({
            evidence_revision: 2,
            current_run_id: "run_t03succ",
            route: "auto_complete",
            phase: "Auto Complete",
          }),
        ),
      [`GET ${HISTORY_PATH}`]: () => jsonResponse(t03HistoryPayload()),
      [`POST ${CORRECT_PATH}`]: () =>
        new Promise<Response>((resolve) => {
          pending.push(resolve);
        }),
    });
    const { client } = renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();

    const issueCorrection = async (raw: string) => {
      await userEvent.click(restrictedButton("更正该字段"));
      const submit = restrictedElement("review-correction-submit");
      await userEvent.type(restrictedElement("review-correction-raw"), raw);
      await userEvent.click(submit);
    };

    await issueCorrection(`${CORRECTION_SENTINEL}:A`);
    await vi.waitFor(() => expect(pending.length).toBe(1));
    await act(async () => {
      await client.refetchQueries({ queryKey: MANUAL_WORK_KEY(WORK_ID) });
    });
    await vi.waitFor(() =>
      expect(restrictedElements("review-correction-pending").length).toBe(0),
    );
    await issueCorrection(`${CORRECTION_SENTINEL}:B`);
    await vi.waitFor(() => expect(pending.length).toBe(2));

    await act(async () => {
      pending[0](jsonResponse(correctionResultPayload()));
    });
    expect(
      (restrictedElement("review-command-status").textContent ?? "").includes(
        "更正提交中",
      ),
    ).toBe(true);
    expect(restrictedButton("续期").disabled).toBe(true);

    await act(async () => {
      pending[1](jsonResponse(correctionResultPayload()));
    });
    await vi.waitFor(() =>
      expect(restrictedElements("review-correction-converged").length).toBe(1),
    );
    const cached = client
      .getMutationCache()
      .getAll()
      .map((mutation) => JSON.stringify(mutation.state));
    expectRestrictedAbsent(cached.join("\n"), CORRECTION_SENTINEL);
    expect(router.calls.filter((call) => call.method === "POST").length).toBe(2);
  });
});

describe("ReviewWorkPanel evidence correction rerun (T03)", () => {
  it("posts the exact correction command, scrubs the reveal, and keeps the shell with history until convergence", async () => {
    let workRequests = 0;
    let workspaceRequests = 0;
    const router = fetchRouter({
      ...baseRoutes(),
      [`GET ${WORK_PATH}`]: () => {
        workRequests += 1;
        return workRequests === 1
          ? jsonResponse(claimedWorkPayload())
          : jsonResponse({ detail: { error: "S03_NOT_FOUND" } }, 404);
      },
      [`GET ${WORKSPACE_PATH}`]: () => {
        workspaceRequests += 1;
        return workspaceRequests === 1
          ? jsonResponse(t03WorkspacePayload())
          : jsonResponse({ detail: { error: "S03_NOT_FOUND" } }, 404);
      },
      [`GET ${ROUTE_PATH}`]: () =>
        jsonResponse(
          routePayload({
            evidence_revision: 2,
            current_run_id: "run_t03succ",
            route: "auto_complete",
            phase: "Auto Complete",
          }),
        ),
      [`GET ${HISTORY_PATH}`]: () => jsonResponse(t03HistoryPayload()),
      [`POST ${REVEAL_PATH}`]: () => jsonResponse(revealResultPayload()),
      [`POST ${CORRECT_PATH}`]: () => jsonResponse(correctionResultPayload()),
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    const correctButton = screen.getAllByRole("button", {
      name: "更正该字段",
    })[0];
    await userEvent.click(screen.getAllByRole("button", { name: "查看来源" })[0]);
    await findRestrictedElements("review-reveal-source");
    await userEvent.click(correctButton);
    const submit = restrictedElement("review-correction-submit");
    expect(submit).toBeDisabled();
    // The raw evidence crosses the boundary byte-for-byte: leading/trailing
    // whitespace is part of the entered value, not trimmed by the client.
    const paddedRaw = `  ${CORRECTION_SENTINEL}  `;
    await userEvent.type(restrictedElement("review-correction-raw"), paddedRaw);
    expect(submit).toBeEnabled();
    await userEvent.click(submit);
    await vi.waitFor(() =>
      expect(
        (
          restrictedElement("review-correction-converged").textContent ?? ""
        ).includes("run_t03succ"),
      ).toBe(true),
    );
    const correctCall = router.calls.filter(
      (call) => call.method === "POST",
    )[1];
    const correctBody = correctCall?.body as {
      application_id: string;
      expected_fence: number;
      expected_context: typeof CONTEXT;
      idempotency_key: string;
      correction: Record<string, unknown>;
    };
    const { raw: submittedRaw, ...correctionWithoutRaw } =
      correctBody.correction;
    expect({ ...correctBody, correction: correctionWithoutRaw }).toEqual({
      application_id: APP_ID,
      expected_fence: 1,
      expected_context: CONTEXT,
      idempotency_key: expect.any(String),
      correction: {
        schema_version: "field-observation-correction/1",
        finding_id: FINDING_ID,
        observation_id: "observation_t02panel",
        document_id: "reg",
        document_role: "机动车登记证书",
        field: "engine_no",
        source_location: {
          source_sha256: "d".repeat(64),
          source_page: 1,
          source_region: "region:1",
        },
        reason_code: "SOURCE_VALUE_MISREAD",
      },
    });
    expectRestrictedEqual(submittedRaw, paddedRaw);
    expect(Object.keys(correctBody)).toEqual([
      "application_id",
      "expected_fence",
      "expected_context",
      "idempotency_key",
      "correction",
    ]);
    expect(
      Object.keys(correctBody.correction),
    ).toEqual([
      "schema_version",
      "finding_id",
      "observation_id",
      "document_id",
      "document_role",
      "field",
      "raw",
      "source_location",
      "reason_code",
    ]);
    // The restricted reveal was scrubbed before the correction command; the
    // sentinel exists nowhere in the panel.
    expectRestrictedElementsAbsent("review-reveal-source");
    expectRestrictedAbsent(
      restrictedElement("review-panel").textContent,
      SOURCE_SENTINEL,
    );
    expectRestrictedAbsent(
      restrictedElement("review-panel").textContent,
      CORRECTION_SENTINEL,
    );
    // The invalidated old workspace/work reads existence-hide (404) but the
    // review shell, the authoritative gate, and history stay usable; the
    // successor convergence is resolved from server-owned route/history.
    expect(restrictedElements("review-correction-pending").length).toBe(0);
    const convergenceText =
      restrictedElement("review-correction-converged").textContent ?? "";
    expect(convergenceText.includes("证据修订 2")).toBe(true);
    expect(convergenceText.includes("run_t03succ")).toBe(true);
    expect(
      (restrictedElement("gate-phase").textContent ?? "").includes("Auto Complete"),
    ).toBe(true);
    const correctionsText =
      restrictedElement("review-history-corrections").textContent ?? "";
    expect(correctionsText.includes("correction_t03panel")).toBe(true);
    expect(
      correctionsText.includes(
        "observation_t02panel → observation_t02panel_succ",
      ),
    ).toBe(true);
    expect(correctionsText.includes("SOURCE_VALUE_MISREAD")).toBe(true);
    expect(correctionsText.includes("证据修订 2")).toBe(true);
    const runsText = restrictedElement("review-history-runs").textContent ?? "";
    expect(runsText.includes("run_t02panel")).toBe(true);
    expect(runsText.includes("run_t03succ")).toBe(true);
    expect(runsText.match(/· 当前/g)).toHaveLength(1);
    expect(runsText.match(/· 非当前/g)).toHaveLength(1);
    // All manual-review writes are fenced while the successor run converges.
    expect(restrictedButtons("查看来源").length).toBe(0);
    expect(restrictedButtons("提交人工核验").length).toBe(0);
  });

  it("renders the sanitized terminal outcome when the authoritative convergence read definitively fails", async () => {
    let routeRequests = 0;
    let workRequestsForTerminal = 0;
    let workspaceRequestsForTerminal = 0;
    const router = fetchRouter({
      ...baseRoutes(),
      [`GET ${WORK_PATH}`]: () => {
        workRequestsForTerminal += 1;
        return workRequestsForTerminal === 1
          ? jsonResponse(claimedWorkPayload())
          : jsonResponse({ detail: { error: "S03_NOT_FOUND" } }, 404);
      },
      [`GET ${WORKSPACE_PATH}`]: () => {
        workspaceRequestsForTerminal += 1;
        return workspaceRequestsForTerminal === 1
          ? jsonResponse(t03WorkspacePayload())
          : jsonResponse({ detail: { error: "S03_NOT_FOUND" } }, 404);
      },
      [`GET ${ROUTE_PATH}`]: () => {
        routeRequests += 1;
        return routeRequests === 1
          ? jsonResponse(routePayload())
          : jsonResponse({ detail: { error: "S03_NOT_FOUND" } }, 404);
      },
      [`GET ${HISTORY_PATH}`]: () => jsonResponse(t03HistoryPayload()),
      [`POST ${CORRECT_PATH}`]: () => jsonResponse(correctionResultPayload()),
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    await userEvent.click(
      screen.getAllByRole("button", { name: "更正该字段" })[0],
    );
    const submit = screen.getByRole("button", { name: "提交修正" });
    await userEvent.type(
      screen.getByTestId("review-correction-raw"),
      CORRECTION_SENTINEL,
    );
    await userEvent.click(submit);
    // The definitive 404 on the authoritative convergence read renders the
    // sanitized terminal outcome (never an elapsed-timeout claim, and never
    // raw error detail).
    await vi.waitFor(() =>
      expect(restrictedElements("review-correction-terminal").length).toBe(1),
    );
    expect(restrictedElements("review-correction-timeout").length).toBe(0);
    const terminalText =
      restrictedElement("review-correction-terminal").textContent ?? "";
    expect(terminalText.includes("S03_NOT_FOUND")).toBe(false);
    expectRestrictedAbsent(
      terminalText,
      CORRECTION_SENTINEL,
    );
    expect(router.calls.filter((call) => call.method === "POST").length).toBe(1);
  });

  it("scrubs the reveal and the correction form on a definitive rejection", async () => {
    const router = fetchRouter({
      ...baseRoutes(),
      [`GET ${WORK_PATH}`]: () => jsonResponse(claimedWorkPayload()),
      [`GET ${WORKSPACE_PATH}`]: () => jsonResponse(t03WorkspacePayload()),
      [`POST ${REVEAL_PATH}`]: () => jsonResponse(revealResultPayload()),
      [`POST ${CORRECT_PATH}`]: () =>
        jsonResponse(
          {
            detail: {
              error: "S03_INVALID_COMMAND",
              reason_code: "CORRECTION_REJECTED",
            },
          },
          422,
        ),
    });
    const { client } = renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    const correctButton = screen.getAllByRole("button", {
      name: "更正该字段",
    })[0];
    await userEvent.click(screen.getAllByRole("button", { name: "查看来源" })[0]);
    await findRestrictedElements("review-reveal-source");
    await userEvent.click(correctButton);
    const submit = restrictedElement("review-correction-submit");
    await userEvent.type(
      restrictedElement("review-correction-raw"),
      CORRECTION_SENTINEL,
    );
    await userEvent.click(submit);
    await vi.waitFor(() =>
      expect(
        (restrictedElement("review-command-status").textContent ?? "").includes(
          "更正未接受（CORRECTION_REJECTED）：请重新加载权威上下文后再试",
        ),
      ).toBe(true),
    );
    // A definitive rejection proves no correction committed: the reveal and
    // the form are scrubbed, and no invalidation shell may appear.
    expectRestrictedElementsAbsent("review-correction-form");
    expectRestrictedElementsAbsent("review-reveal-source");
    expectRestrictedAbsent(
      restrictedElement("review-panel").textContent,
      SOURCE_SENTINEL,
    );
    expect(restrictedElements("review-correction-pending").length).toBe(0);
    // The restricted raw must not survive in the MutationCache after the
    // definitive rejection scrubbed the panel.
    const cached = client
      .getMutationCache()
      .getAll()
      .map((mutation) => JSON.stringify(mutation.state.variables ?? {}));
    expectRestrictedAbsent(cached.join("\n"), CORRECTION_SENTINEL);
    expect(restrictedElements("review-reload-note").length).toBe(1);
    for (const button of restrictedButtons("查看来源")) {
      expect(button.disabled).toBe(true);
    }
    for (const name of ["认领", "续期", "释放", "提交人工核验"]) {
      expect(restrictedButton(name).disabled).toBe(true);
    }
    expect(router.calls.filter((call) => call.method === "POST").length).toBe(2);
  });
});

const T04_REQUEST_ID = "supplement_request_t04panel00000000000000000000000";
const T04_SUPP_PATH =
  "/controlled/s01/api/commands/review-work-items/work_t02panel1234567890abcdef/supplement";
const T04_REQUEST_VIEW_PATH =
  "/controlled/s01/api/queries/supplement-requests/supplement_request_t04panel00000000000000000000000";

function t04ClaimedWorkPayload() {
  return workPayload({
    status: "claimed",
    claim_subject: "t02-reviewer",
    claim_fence: 1,
    claim_expires_at: LIVE_CLAIM_EXPIRES_AT,
    automatic_findings: [
      {
        finding_id: FINDING_ID,
        rule_id: "R_VIN_CROSS",
        verdict: "uncertain",
        severity: "critical",
        reason_code: "MISSING_DOCS",
      },
    ],
  });
}

function t04ClaimedWorkspacePayload() {
  const finding = {
    finding_id: FINDING_ID,
    run_id: "run_t02panel",
    rule_id: "R_VIN_CROSS",
    verdict: "uncertain",
    severity: "critical",
    reason_code: "MISSING_DOCS",
    mandatory: true,
    evidence_links: [],
  };
  return workspacePayload({
    claim_fence: 1,
    claim_expires_at: LIVE_CLAIM_EXPIRES_AT,
    mandatory_blockers: [finding],
    selected_finding: finding,
  });
}

function t04RequestViewPayload(overrides: Record<string, unknown> = {}) {
  return {
    schema_version: "supplement-request/1",
    request_id: T04_REQUEST_ID,
    work_item_id: "work_t04supplement1234567890abcd",
    source_work_item_id: WORK_ID,
    application_id: APP_ID,
    cycle: 1,
    run_id: "run_t02panel",
    finding_id: FINDING_ID,
    rule_id: "R_VIN_CROSS",
    finding_reason_code: "MISSING_DOCS",
    finding_verdict: "uncertain",
    requester_claim_fence: 1,
    requested_at: 100,
    due_at: 9999999999,
    fixed_context: CONTEXT,
    context_digest: "g".repeat(64),
    expected_predecessor_attachment_id: "attachment_t04v1",
    expected_predecessor_attachment_version: 1,
    satisfaction_policy_digest: "h".repeat(64),
    status: "open",
    current: true,
    phase: "Supplement",
    route: "supplement_pending",
    lifecycle_revision: 7,
    evidence_revision: 1,
    projection_watermark: 1,
    material_requirement: {
      material_requirement_id: "c-demo-financing-lease-vin/1",
      document_role: "financing_lease_contract",
      material_kind: "financing_lease_contract",
      operation: "replacement",
      required_fact_kinds: [
        "attachment",
        "page",
        "producer",
        "vin_observation",
      ],
      responsible_party: "application_material_provider",
      allowed_tenant_id: "c-demo",
      allowed_source_system_ids: ["s06-material-source"],
      allowed_workload_identity_ids: ["s06-material-workload"],
      satisfaction_policy_id: "c-demo-supplement-satisfaction/1",
      batch_item_count: 2,
      batch_closure_required: true,
      integrity_required: true,
      provenance_required: true,
      evidence_eligibility_required: true,
    },
    ...overrides,
  };
}

function t04AcceptedResult() {
  return {
    status: "accepted",
    replayed: false,
    application_id: APP_ID,
    request_id: T04_REQUEST_ID,
    work_item_id: "work_t04supplement1234567890abcd",
    finding_id: FINDING_ID,
    material_requirement_id: "c-demo-financing-lease-vin/1",
    phase: "Supplement",
    route: "supplement_pending",
    due_at: 9999999999,
    lifecycle_revision: 7,
    evidence_revision: 1,
  };
}

function t04HistoryPayload(overrides: Record<string, unknown> = {}) {
  const runs = historyPayload().runs;
  return historyPayload({
    current_run_id: "run_t04succ",
    runs: [
      { ...runs[0], current: false },
      {
        ...runs[0],
        run_id: "run_t04succ",
        current: true,
        evidence_revision: 2,
      },
    ],
    attachment_versions: [
      {
        attachment_id: "attachment_t04v1",
        version: 1,
        document_id: "lease",
        document_role: "融资租赁合同",
        supersedes_attachment_id: null,
        page_ids: ["page_t04v1"],
        producer_result_id: null,
        evidence_revision: 1,
        current: false,
      },
      {
        attachment_id: "attachment_t04v2",
        version: 2,
        document_id: "lease",
        document_role: "融资租赁合同",
        supersedes_attachment_id: "attachment_t04v1",
        page_ids: ["page_t04v2"],
        producer_result_id: "result_t04v2",
        evidence_revision: 2,
        current: true,
      },
    ],
    ...overrides,
  });
}

function fetchMockBody(method: string, path: string): unknown {
  const mock = vi.mocked(fetch);
  for (let index = mock.mock.calls.length - 1; index >= 0; index -= 1) {
    const call = mock.mock.calls[index];
    const url = String(call[0]);
    const callMethod = (call[1]?.method ?? "GET") as string;
    const pathname = new URL(url, "http://localhost").pathname;
    if (callMethod === method && pathname === path) {
      return call[1]?.body !== undefined ? JSON.parse(String(call[1].body)) : undefined;
    }
  }
  return undefined;
}

describe("ReviewWorkPanel supplement request (T04)", () => {
  it("hides the supplement control for an ineligible finding", async () => {
    fetchRouter({
      ...baseRoutes(),
      [`GET ${WORK_PATH}`]: () => jsonResponse(workPayload()),
      [`GET ${WORKSPACE_PATH}`]: () => jsonResponse(workspacePayload()),
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    expect(
      screen.queryByRole("button", { name: "请求补充材料" }),
    ).not.toBeInTheDocument();
  });

  it("shows the supplement control for a claimed eligible R_VIN_CROSS finding", async () => {
    fetchRouter({
      ...baseRoutes(),
      [`GET ${WORK_PATH}`]: () => jsonResponse(t04ClaimedWorkPayload()),
      [`GET ${WORKSPACE_PATH}`]: () => jsonResponse(t04ClaimedWorkspacePayload()),
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    const supplementButton = screen.getByRole("button", {
      name: "请求补充材料",
    });
    expect(supplementButton).toBeEnabled();
  });

  it("posts the exact server-bound supplement command and keeps the shell with the request view", async () => {
    let workRequests = 0;
    let workspaceRequests = 0;
    const router = fetchRouter({
      ...baseRoutes(),
      [`GET ${WORK_PATH}`]: () => {
        workRequests += 1;
        return workRequests === 1
          ? jsonResponse(t04ClaimedWorkPayload())
          : jsonResponse({ detail: { error: "S03_NOT_FOUND" } }, 404);
      },
      [`GET ${WORKSPACE_PATH}`]: () => {
        workspaceRequests += 1;
        return workspaceRequests === 1
          ? jsonResponse(t04ClaimedWorkspacePayload())
          : jsonResponse({ detail: { error: "S03_NOT_FOUND" } }, 404);
      },
      [`GET ${T04_REQUEST_VIEW_PATH}`]: () =>
        jsonResponse(t04RequestViewPayload()),
      [`POST ${T04_SUPP_PATH}`]: () => jsonResponse(t04AcceptedResult()),
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    await userEvent.click(screen.getByRole("button", { name: "请求补充材料" }));
    await vi.waitFor(() =>
      expect(restrictedElements("review-supplement-status").length).toBeGreaterThan(
        0,
      ),
    );
    const supplementCalls = router.calls.filter((call) =>
      call.method === "POST" && call.url.endsWith("/supplement"),
    );
    expect(supplementCalls).toHaveLength(1);
    expect(supplementCalls[0].body).toEqual({
      finding_id: FINDING_ID,
      reason_code: "MISSING_REQUIRED_MATERIAL",
      expected_fence: 1,
      expected_context: CONTEXT,
      idempotency_key: expect.any(String),
      predecessor_request_id: null,
    });
    expect(
      Object.keys(supplementCalls[0].body as Record<string, unknown>),
    ).toEqual([
      "finding_id",
      "reason_code",
      "expected_fence",
      "expected_context",
      "idempotency_key",
      "predecessor_request_id",
    ]);
    // The old work item existence-hides but the shell, request view, gate
    // and history stay usable; no fulfillment or route is ever inferred.
    await vi.waitFor(() =>
      expect(
        (
          restrictedElement("review-supplement-status").textContent ?? ""
        ).includes("open"),
      ).toBe(true),
    );
    expect(restrictedElements("gate-phase").length).toBe(1);
    expect(restrictedElements("review-history-runs").length).toBe(1);
    expect(restrictedElements("review-supplement-status").length).toBe(1);
    expect(
      restrictedElement("review-supplement-status").textContent,
    ).not.toContain("fulfilled");
  });

  it("keeps the exact supplement command and key when the transport outcome is unknown", async () => {
    let supplementPosts = 0;
    fetchRouter({
      ...baseRoutes(),
      [`GET ${WORK_PATH}`]: () => jsonResponse(t04ClaimedWorkPayload()),
      [`GET ${WORKSPACE_PATH}`]: () => jsonResponse(t04ClaimedWorkspacePayload()),
      [`POST ${T04_SUPP_PATH}`]: () => {
        supplementPosts += 1;
        return Promise.reject(new TypeError("fetch failed: connection reset"));
      },
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    await userEvent.click(screen.getByRole("button", { name: "请求补充材料" }));
    await vi.waitFor(() =>
      expect(
        (
          restrictedElement("review-command-status").textContent ?? ""
        ).includes("结果未知"),
      ).toBe(true),
    );
    expect(screen.getByRole("button", { name: "重试" })).toBeInTheDocument();
    const firstBody = fetchMockBody("POST", T04_SUPP_PATH);
    await userEvent.click(screen.getByRole("button", { name: "重试" }));
    await vi.waitFor(() => expect(supplementPosts).toBe(2));
    expect(fetchMockBody("POST", T04_SUPP_PATH)).toEqual(firstBody);
  });

  it("fences the supplement action after a definitive 409 and recovers only on reload", async () => {
    const router = fetchRouter({
      ...baseRoutes(),
      [`GET ${WORK_PATH}`]: () => jsonResponse(t04ClaimedWorkPayload()),
      [`GET ${WORKSPACE_PATH}`]: () => jsonResponse(t04ClaimedWorkspacePayload()),
      [`POST ${T04_SUPP_PATH}`]: () =>
        jsonResponse(
          {
            detail: {
              error: "S03_CONFLICT",
              reason_code: "ACTIVE_SUPPLEMENT_REQUEST_EXISTS",
            },
          },
          409,
        ),
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    await userEvent.click(screen.getByRole("button", { name: "请求补充材料" }));
    await vi.waitFor(() =>
      expect(
        (
          restrictedElement("review-command-status").textContent ?? ""
        ).includes("ACTIVE_SUPPLEMENT_REQUEST_EXISTS"),
      ).toBe(true),
    );
    expect(restrictedElements("review-reload-note").length).toBe(1);
    expect(
      (
        screen.getByRole("button", { name: "请求补充材料" }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
    await userEvent.click(screen.getByRole("button", { name: "重新加载" }));
    await vi.waitFor(() =>
      expect(
        (
          restrictedElement("review-command-status").textContent ?? ""
        ).includes("等待操作"),
      ).toBe(true),
    );
    expect(
      (
        screen.getByRole("button", { name: "请求补充材料" }) as HTMLButtonElement
      ).disabled,
    ).toBe(false);
    expect(router.calls.filter((call) => call.method === "POST")).toHaveLength(1);
  });

  it("renders attachment versions with exactly one server-current version", async () => {
    fetchRouter({
      ...baseRoutes(),
      [`GET ${WORK_PATH}`]: () => jsonResponse(t04ClaimedWorkPayload()),
      [`GET ${WORKSPACE_PATH}`]: () => jsonResponse(t04ClaimedWorkspacePayload()),
      [`GET ${HISTORY_PATH}`]: () => jsonResponse(t04HistoryPayload()),
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    const attachments = restrictedElement("review-history-attachments");
    const text = attachments.textContent ?? "";
    expect(text.includes("v1")).toBe(true);
    expect(text.includes("v2")).toBe(true);
    expect(text.match(/· 当前/g)).toHaveLength(1);
    expect(text.match(/· 非当前/g)).toHaveLength(1);
  });

  it("renders the fulfilled request and the converged successor from server facts", async () => {
    const router = fetchRouter({
      ...baseRoutes(),
      [`GET ${WORK_PATH}`]: () => jsonResponse(t04ClaimedWorkPayload()),
      [`GET ${WORKSPACE_PATH}`]: () => jsonResponse(t04ClaimedWorkspacePayload()),
      [`GET ${HISTORY_PATH}`]: () => jsonResponse(t04HistoryPayload()),
      [`GET ${ROUTE_PATH}`]: () =>
        jsonResponse(
          routePayload({
            evidence_revision: 2,
            current_run_id: "run_t04succ",
            route: "manual_review",
            phase: "Manual Review",
          }),
        ),
      [`GET ${T04_REQUEST_VIEW_PATH}`]: () =>
        jsonResponse(
          t04RequestViewPayload({
            status: "fulfilled",
            current: false,
            phase: "Assembly",
            route: "pending_check",
            evidence_revision: 2,
            lifecycle_revision: 9,
          }),
        ),
      [`POST ${T04_SUPP_PATH}`]: () => jsonResponse(t04AcceptedResult()),
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    await userEvent.click(screen.getByRole("button", { name: "请求补充材料" }));
    await vi.waitFor(() =>
      expect(restrictedElements("review-supplement-converged").length).toBe(1),
    );
    const converged =
      restrictedElement("review-supplement-converged").textContent ?? "";
    expect(converged.includes("证据修订 2")).toBe(true);
    expect(converged.includes("run_t04succ")).toBe(true);
    // The request status is server data rendered verbatim; the browser never
    // invents fulfillment.
    expect(
      (
        restrictedElement("review-supplement-status").textContent ?? ""
      ).includes("fulfilled"),
    ).toBe(true);
    const runsText = restrictedElement("review-history-runs").textContent ?? "";
    expect(runsText.match(/· 当前/g)).toHaveLength(1);
    expect(
      router.calls.filter((call) => call.method === "POST" && call.url.endsWith("/supplement")).length,
    ).toBe(1);
  });
});

describe("ReviewWorkPanel business exception request (T05)", () => {
  const REQUEST_PATH =
    "/controlled/s01/api/commands/review-work-items/work_t02panel1234567890abcdef/business-exceptions";
  const REQUEST_ID = "exception_request_t05abcdef";

  function claimedPayload() {
    return workPayload({
      status: "claimed",
      claim_subject: "t02-reviewer",
      claim_fence: 1,
      claim_expires_at: LIVE_CLAIM_EXPIRES_AT,
    });
  }

  function eligibleWorkspace() {
    return workspacePayload({
      claim_fence: 1,
      claim_expires_at: LIVE_CLAIM_EXPIRES_AT,
      business_exception_eligibility: {
        eligible: true,
        request_reason: "DOCUMENTED_BRAND_VARIANCE",
        ineligible_reason_code: null,
        predecessor_request_id: null,
      },
    });
  }

  it("requests only when the DTO says eligible and never posts an operator command", async () => {
    let phase: "manual_review" | "pending_exception_approval" = "manual_review";
    const router = fetchRouter({
      [`GET ${WORK_PATH}`]: () =>
        router.jsonResponse(
          phase === "manual_review"
            ? claimedPayload()
            : workPayload({
                status: "exception_requested",
                claim_subject: null,
                claim_fence: 0,
                claim_expires_at: 0,
                completed_finding_ids: [FINDING_ID],
              }),
        ),
      [`GET ${WORKSPACE_PATH}`]: () =>
        phase === "manual_review"
          ? router.jsonResponse(eligibleWorkspace())
          : router.errorResponse(404, "S01_NOT_FOUND"),
      [`GET ${ROUTE_PATH}`]: () =>
        router.jsonResponse(
          phase === "manual_review"
            ? routePayload()
            : routePayload({
                phase: "Pending Exception Approval",
                route: "pending_exception_approval",
              }),
        ),
      [`GET ${HISTORY_PATH}`]: () =>
        router.jsonResponse(
          phase === "manual_review"
            ? historyPayload()
            : historyPayload({
                business_exceptions: [
                  {
                    request_id: REQUEST_ID,
                    run_id: "run_t02panel",
                    finding_id: FINDING_ID,
                    rule_id: "R_ENGINE_CROSS",
                    machine_verdict: "inconsistent",
                    status: "pending",
                    current: true,
                    request_reason: "DOCUMENTED_BRAND_VARIANCE",
                    scope: "one_application_cycle_run_finding",
                    requested_at: 10,
                    expires_at: 9999999999,
                    decision_id: null,
                    decision: null,
                    routed: false,
                    route: null,
                    completion_basis: null,
                  },
                ],
              }),
        ),
      [`POST ${REQUEST_PATH}`]: () => {
        phase = "pending_exception_approval";
        return router.jsonResponse({
          status: "accepted",
          replayed: false,
          application_id: APP_ID,
          request_id: REQUEST_ID,
          work_item_id: "work_exception_t05",
          finding_id: FINDING_ID,
          phase: "Pending Exception Approval",
          route: "pending_exception_approval",
          expires_at: 9999999999,
          lifecycle_revision: 7,
          evidence_revision: 1,
        });
      },
    });
    const user = userEvent.setup();
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitFor(() =>
      expect(screen.getByTestId("exception-request-button")).toBeEnabled(),
    );
    await user.click(screen.getByTestId("exception-request-button"));
    await waitFor(() =>
      expect(screen.getByTestId("exception-request-accepted")).toBeInTheDocument(),
    );
    const posts = router.calls.filter(
      (call) => call.method === "POST" && call.url.includes("/business-exceptions"),
    );
    expect(posts).toHaveLength(1);
    expect(posts[0].body).toMatchObject({
      finding_id: FINDING_ID,
      reason_code: "DOCUMENTED_BRAND_VARIANCE",
      expected_fence: 1,
      expected_context: CONTEXT,
      predecessor_request_id: null,
    });
    expect(
      typeof (posts[0].body as Record<string, unknown>).idempotency_key,
    ).toBe("string");
    const operatorCommands = router.calls.filter((call) =>
      /\/route|\/expire|\/invalidate|\/close|\/resume/.test(call.url),
    );
    expect(operatorCommands).toHaveLength(0);
    expect(
      restrictedElement("exception-request-accepted").textContent,
    ).toContain(REQUEST_ID);
    expect(
      (restrictedElement("exception-request-accepted").textContent ?? "").includes(
        "pending_exception_approval",
      ),
    ).toBe(true);
  });

  it("disables the request and shows the server ineligible reason when denied", async () => {
    const router = fetchRouter({
      [`GET ${WORK_PATH}`]: () => router.jsonResponse(claimedPayload()),
      [`GET ${WORKSPACE_PATH}`]: () =>
        router.jsonResponse(
          workspacePayload({
            claim_fence: 1,
            claim_expires_at: LIVE_CLAIM_EXPIRES_AT,
            business_exception_eligibility: {
              eligible: false,
              request_reason: null,
              ineligible_reason_code: "PROTECTED_CHECK_NOT_WAIVABLE",
              predecessor_request_id: null,
            },
          }),
        ),
      [`GET ${ROUTE_PATH}`]: () => router.jsonResponse(routePayload()),
      [`GET ${HISTORY_PATH}`]: () => router.jsonResponse(historyPayload()),
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitFor(() =>
      expect(screen.getByTestId("exception-ineligible")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("exception-ineligible")).toHaveTextContent(
      "PROTECTED_CHECK_NOT_WAIVABLE",
    );
    expect(screen.getByTestId("exception-request-button")).toBeDisabled();
  });

  it("surfaces a definitive stale rejection and keeps the key for unknown outcomes", async () => {
    let outcome: "ok" | "stale" | "unknown" = "ok";
    const router = fetchRouter({
      [`GET ${WORK_PATH}`]: () => router.jsonResponse(claimedPayload()),
      [`GET ${WORKSPACE_PATH}`]: () => router.jsonResponse(eligibleWorkspace()),
      [`GET ${ROUTE_PATH}`]: () => router.jsonResponse(routePayload()),
      [`GET ${HISTORY_PATH}`]: () => router.jsonResponse(historyPayload()),
      [`POST ${REQUEST_PATH}`]: () => {
        if (outcome === "stale") {
          return router.errorResponse(409, "S05_STALE", "STALE_REVIEW_CONTEXT");
        }
        if (outcome === "unknown") {
          return router.errorResponse(500, "S05_INTERNAL_ERROR");
        }
        outcome = "stale";
        return router.errorResponse(409, "S05_STALE", "STALE_REVIEW_CONTEXT");
      },
    });
    const user = userEvent.setup();
    const { unmount } = renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitFor(() =>
      expect(screen.getByTestId("exception-request-button")).toBeEnabled(),
    );
    await user.click(screen.getByTestId("exception-request-button"));
    await waitFor(() =>
      expect(screen.getByTestId("review-command-status")).toHaveTextContent(
        "未接受",
      ),
    );
    expect(screen.getByTestId("review-command-status").textContent).toContain(
      "STALE_REVIEW_CONTEXT",
    );
    expect(screen.getByTestId("review-reload-note")).toBeInTheDocument();
    unmount();

    outcome = "unknown";
    const second = fetchRouter({
      [`GET ${WORK_PATH}`]: () => router.jsonResponse(claimedPayload()),
      [`GET ${WORKSPACE_PATH}`]: () => router.jsonResponse(eligibleWorkspace()),
      [`GET ${ROUTE_PATH}`]: () => router.jsonResponse(routePayload()),
      [`GET ${HISTORY_PATH}`]: () => router.jsonResponse(historyPayload()),
      [`POST ${REQUEST_PATH}`]: () => {
        if (outcome === "unknown") {
          outcome = "ok";
          return router.errorResponse(500, "S05_INTERNAL_ERROR");
        }
        return router.jsonResponse({
          status: "accepted",
          replayed: false,
          application_id: APP_ID,
          request_id: REQUEST_ID,
          work_item_id: "work_exception_t05",
          finding_id: FINDING_ID,
          phase: "Pending Exception Approval",
          route: "pending_exception_approval",
          expires_at: 9999999999,
          lifecycle_revision: 7,
          evidence_revision: 1,
        });
      },
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitFor(() =>
      expect(screen.getByTestId("exception-request-button")).toBeEnabled(),
    );
    await user.click(screen.getByTestId("exception-request-button"));
    await waitFor(() =>
      expect(screen.getByTestId("retry-button")).toBeInTheDocument(),
    );
    await user.click(screen.getByTestId("retry-button"));
    await waitFor(() =>
      expect(screen.getByTestId("exception-request-accepted")).toBeInTheDocument(),
    );
    const posts = second.calls.filter(
      (call) => call.method === "POST" && call.url.includes("/business-exceptions"),
    );
    expect(posts).toHaveLength(2);
    const firstKey = (posts[0].body as Record<string, unknown>).idempotency_key;
    const secondKey = (posts[1].body as Record<string, unknown>).idempotency_key;
    expect(firstKey).toBe(secondKey);
  });

  it("surfaces a failed authoritative refetch as unavailable and clears it only after both queries succeed", async () => {
    let phase: "manual_review" | "pending_exception_approval" = "manual_review";
    let failRefetch = false;
    const router = fetchRouter({
      [`GET ${WORK_PATH}`]: () =>
        router.jsonResponse(
          phase === "manual_review"
            ? claimedPayload()
            : workPayload({
                status: "exception_requested",
                claim_subject: null,
                claim_fence: 0,
                claim_expires_at: 0,
                completed_finding_ids: [FINDING_ID],
              }),
        ),
      [`GET ${WORKSPACE_PATH}`]: () =>
        phase === "manual_review"
          ? router.jsonResponse(eligibleWorkspace())
          : router.errorResponse(404, "S01_NOT_FOUND"),
      [`GET ${ROUTE_PATH}`]: () =>
        failRefetch
          ? router.errorResponse(500, "S01_INTERNAL_ERROR")
          : router.jsonResponse(
              phase === "manual_review"
                ? routePayload()
                : routePayload({
                    phase: "Pending Exception Approval",
                    route: "pending_exception_approval",
                  }),
            ),
      [`GET ${HISTORY_PATH}`]: () =>
        failRefetch
          ? router.errorResponse(500, "S01_INTERNAL_ERROR")
          : router.jsonResponse(
              phase === "manual_review"
                ? historyPayload()
                : historyPayload({
                    business_exceptions: [
                      {
                        request_id: REQUEST_ID,
                        run_id: "run_t02panel",
                        finding_id: FINDING_ID,
                        rule_id: "R_ENGINE_CROSS",
                        machine_verdict: "inconsistent",
                        status: "pending",
                        current: true,
                        request_reason: "DOCUMENTED_BRAND_VARIANCE",
                        scope: "one_application_cycle_run_finding",
                        requested_at: 10,
                        expires_at: LIVE_CLAIM_EXPIRES_AT,
                        decision_id: null,
                        decision: null,
                        routed: false,
                        route: null,
                        completion_basis: null,
                      },
                    ],
                  }),
            ),
      [`POST ${REQUEST_PATH}`]: () => {
        phase = "pending_exception_approval";
        return router.jsonResponse({
          status: "accepted",
          replayed: false,
          application_id: APP_ID,
          request_id: REQUEST_ID,
          work_item_id: "work_exception_t05",
          finding_id: FINDING_ID,
          phase: "Pending Exception Approval",
          route: "pending_exception_approval",
          expires_at: LIVE_CLAIM_EXPIRES_AT,
          lifecycle_revision: 7,
          evidence_revision: 1,
        });
      },
    });
    const user = userEvent.setup();
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitFor(() =>
      expect(screen.getByTestId("exception-request-button")).toBeEnabled(),
    );
    await user.click(screen.getByTestId("exception-request-button"));
    await waitFor(() =>
      expect(screen.getByTestId("exception-request-accepted")).toBeInTheDocument(),
    );
    expect(
      (restrictedElement("exception-request-accepted").textContent ?? "").includes(
        "pending · 当前",
      ),
    ).toBe(true);

    failRefetch = true;
    await user.click(screen.getByTestId("exception-reload-button"));
    await waitFor(() =>
      expect(screen.getByTestId("exception-route")).toHaveTextContent(
        "unavailable",
      ),
    );
    expect(screen.getByTestId("exception-status")).toHaveTextContent(
      "不可用（权威重新加载失败）",
    );
    const staleText = restrictedElement("exception-request-accepted").textContent ?? "";
    expect(staleText.includes("pending · 当前")).toBe(false);

    failRefetch = false;
    await user.click(screen.getByTestId("exception-reload-button"));
    await waitFor(() =>
      expect(screen.getByTestId("exception-route")).toHaveTextContent(
        "pending_exception_approval",
      ),
    );
    expect(screen.getByTestId("exception-status")).toHaveTextContent(
      "pending · 当前",
    );
  });

  it("marks accepted recovery unavailable when an owning query fails automatically and recovers on owner success", async () => {
    let phase: "manual_review" | "pending_exception_approval" = "manual_review";
    let failOwners = false;
    const router = fetchRouter({
      [`GET ${WORK_PATH}`]: () =>
        router.jsonResponse(
          phase === "manual_review"
            ? claimedPayload()
            : workPayload({
                status: "exception_requested",
                claim_subject: null,
                claim_fence: 0,
                claim_expires_at: 0,
                completed_finding_ids: [FINDING_ID],
              }),
        ),
      [`GET ${WORKSPACE_PATH}`]: () =>
        phase === "manual_review"
          ? router.jsonResponse(eligibleWorkspace())
          : router.errorResponse(404, "S01_NOT_FOUND"),
      [`GET ${ROUTE_PATH}`]: () =>
        failOwners
          ? router.errorResponse(500, "S01_INTERNAL_ERROR")
          : router.jsonResponse(
              phase === "manual_review"
                ? routePayload()
                : routePayload({
                    phase: "Pending Exception Approval",
                    route: "pending_exception_approval",
                  }),
            ),
      [`GET ${HISTORY_PATH}`]: () =>
        failOwners
          ? router.errorResponse(500, "S01_INTERNAL_ERROR")
          : router.jsonResponse(
              phase === "manual_review"
                ? historyPayload()
                : historyPayload({
                    business_exceptions: [
                      {
                        request_id: REQUEST_ID,
                        run_id: "run_t02panel",
                        finding_id: FINDING_ID,
                        rule_id: "R_ENGINE_CROSS",
                        machine_verdict: "inconsistent",
                        status: "pending",
                        current: true,
                        request_reason: "DOCUMENTED_BRAND_VARIANCE",
                        scope: "one_application_cycle_run_finding",
                        requested_at: 10,
                        expires_at: LIVE_CLAIM_EXPIRES_AT,
                        decision_id: null,
                        decision: null,
                        routed: false,
                        route: null,
                        completion_basis: null,
                      },
                    ],
                  }),
            ),
      [`POST ${REQUEST_PATH}`]: () => {
        phase = "pending_exception_approval";
        return router.jsonResponse({
          status: "accepted",
          replayed: false,
          application_id: APP_ID,
          request_id: REQUEST_ID,
          work_item_id: "work_exception_t05",
          finding_id: FINDING_ID,
          phase: "Pending Exception Approval",
          route: "pending_exception_approval",
          expires_at: LIVE_CLAIM_EXPIRES_AT,
          lifecycle_revision: 7,
          evidence_revision: 1,
        });
      },
    });
    const user = userEvent.setup();
    const { client } = renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitFor(() =>
      expect(screen.getByTestId("exception-request-button")).toBeEnabled(),
    );
    await user.click(screen.getByTestId("exception-request-button"));
    await waitFor(() =>
      expect(screen.getByTestId("exception-request-accepted")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("exception-recovery-status")).toHaveTextContent(
      "权威状态正常",
    );
    expect(screen.getByTestId("exception-status")).toHaveTextContent(
      "pending · 当前",
    );

    // An automatic refetch (not the manual reload action) fails: the owners
    // are in error, so retained facts are never presented as current.
    failOwners = true;
    client.invalidateQueries({ queryKey: ["s01"] });
    await waitFor(() =>
      expect(screen.getByTestId("exception-route")).toHaveTextContent(
        "unavailable",
      ),
    );
    expect(screen.getByTestId("exception-status")).toHaveTextContent(
      "不可用（权威读取失败）",
    );
    expect(screen.getByTestId("exception-recovery-status")).toHaveTextContent(
      "权威状态不可用（读取失败）",
    );
    const staleText =
      restrictedElement("exception-request-accepted").textContent ?? "";
    expect(staleText.includes("pending · 当前")).toBe(false);
    expect(staleText.includes("权威状态正常")).toBe(false);

    // The owners succeed again: currentness returns naturally.
    failOwners = false;
    client.invalidateQueries({ queryKey: ["s01"] });
    await waitFor(() =>
      expect(screen.getByTestId("exception-route")).toHaveTextContent(
        "pending_exception_approval",
      ),
    );
    expect(screen.getByTestId("exception-status")).toHaveTextContent(
      "pending · 当前",
    );
    expect(screen.getByTestId("exception-recovery-status")).toHaveTextContent(
      "权威状态正常",
    );
  });
});

/** A run carrying the complete server-governed release pin.  Every governed
 * field is optional on the type, so legacy runs omit them entirely and the
 * panel must render placeholders instead of inventing a release. */
function governedRunPayload(overrides: Record<string, unknown> = {}) {
  return {
    ...historyPayload().runs[0],
    candidate_id: "candidate_t42pin0001",
    manifest_id: "manifest_t42pin0001",
    manifest_digest: "11".repeat(32),
    validation_bundle_id: "validation_t42pin0001",
    validation_bundle_digest: "22".repeat(32),
    approval_binding_id: "approval_t42pin0001",
    approval_binding_digest: "33".repeat(32),
    activation_event_id: "activation_t42pin0001",
    active_generation: 2,
    release_id: "auto_lease@1.9.0",
    release_digest: "44".repeat(32),
    components: [
      { type: "ruleset", id: "component_rules_t42pin", digest: "55".repeat(32) },
      {
        type: "semantic_catalog",
        id: "component_catalog_t42pin",
        digest: "66".repeat(32),
      },
      {
        type: "entity_knowledge",
        id: "component_kb_t42pin",
        digest: "77".repeat(32),
      },
    ],
    ...overrides,
  };
}

describe("ReviewWorkPanel governed-release pin (T08)", () => {
  it("renders the complete governed-release pin for the current run", async () => {
    fetchRouter({
      ...baseRoutes(),
      [`GET ${HISTORY_PATH}`]: () =>
        jsonResponse(historyPayload({ runs: [governedRunPayload()] })),
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    const runs = restrictedElements("review-history-run");
    expect(runs).toHaveLength(1);
    expect(runs[0].textContent).toContain("· 当前");
    const pin = within(runs[0]);
    expect(pin.getByTestId("review-run-candidate")).toHaveTextContent(
      "candidate_t42pin0001",
    );
    expect(pin.getByTestId("review-run-manifest")).toHaveTextContent(
      "manifest_t42pin0001",
    );
    expect(pin.getByTestId("review-run-manifest-digest")).toHaveTextContent(
      "11".repeat(32),
    );
    expect(
      pin.getByTestId("review-run-validation-bundle"),
    ).toHaveTextContent("validation_t42pin0001");
    expect(
      pin.getByTestId("review-run-validation-bundle-digest"),
    ).toHaveTextContent("22".repeat(32));
    expect(pin.getByTestId("review-run-approval-binding")).toHaveTextContent(
      "approval_t42pin0001",
    );
    expect(
      pin.getByTestId("review-run-approval-binding-digest"),
    ).toHaveTextContent("33".repeat(32));
    expect(pin.getByTestId("review-run-activation-event")).toHaveTextContent(
      "activation_t42pin0001",
    );
    expect(pin.getByTestId("review-run-active-generation")).toHaveTextContent(
      "2",
    );
    expect(pin.getByTestId("review-run-release")).toHaveTextContent(
      "auto_lease@1.9.0",
    );
    expect(pin.getByTestId("review-run-release-digest")).toHaveTextContent(
      "44".repeat(32),
    );
  });

  it("renders the complete governed-release pin for a non-current run", async () => {
    fetchRouter({
      ...baseRoutes(),
      [`GET ${HISTORY_PATH}`]: () =>
        jsonResponse(
          historyPayload({
            runs: [
              historyPayload().runs[0],
              {
                ...governedRunPayload(),
                run_id: "run_t42pinold",
                current: false,
                currentness_reason: "SUPERSEDED_BY_EVIDENCE_REVISION",
              },
            ],
          }),
        ),
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    const runs = restrictedElements("review-history-run");
    expect(runs).toHaveLength(2);
    const nonCurrent = runs.find((run) =>
      (run.textContent ?? "").includes("· 非当前"),
    );
    expect(nonCurrent).toBeDefined();
    const pin = within(nonCurrent as HTMLElement);
    expect(pin.getByTestId("review-run-candidate")).toHaveTextContent(
      "candidate_t42pin0001",
    );
    expect(pin.getByTestId("review-run-manifest")).toHaveTextContent(
      "manifest_t42pin0001",
    );
    expect(pin.getByTestId("review-run-approval-binding")).toHaveTextContent(
      "approval_t42pin0001",
    );
    expect(pin.getByTestId("review-run-activation-event")).toHaveTextContent(
      "activation_t42pin0001",
    );
    expect(pin.getByTestId("review-run-active-generation")).toHaveTextContent(
      "2",
    );
    expect(pin.getByTestId("review-run-release")).toHaveTextContent(
      "auto_lease@1.9.0",
    );
  });

  it("renders no governed pin block for legacy runs without governed identity", async () => {
    const legacy = historyPayload().runs[0];
    fetchRouter({
      ...baseRoutes(),
      [`GET ${HISTORY_PATH}`]: () =>
        jsonResponse(
          historyPayload({
            runs: [
              legacy,
              {
                ...legacy,
                run_id: "run_t42pinlegacy",
                current: false,
                currentness_reason: "LEGACY_RUN_BEFORE_GOVERNED_RELEASE",
                candidate_id: null,
                manifest_id: null,
                manifest_digest: null,
                validation_bundle_id: null,
                validation_bundle_digest: null,
                approval_binding_id: null,
                approval_binding_digest: null,
                activation_event_id: null,
                active_generation: null,
                release_id: null,
                release_digest: null,
                components: null,
              },
            ],
          }),
        ),
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    const runs = restrictedElements("review-history-run");
    expect(runs).toHaveLength(2);
    // Legacy runs predate the governed release: they carry no governed
    // identity, so the pin block is absent entirely and the base history row
    // (run id, status, currentness) renders exactly as before.
    for (const run of runs) {
      expect(
        within(run).queryByTestId("review-run-governed-pin"),
      ).not.toBeInTheDocument();
      expect(run.textContent).toContain(" · ");
    }
  });

  it("renders the pinned components with kind, id and digest", async () => {
    fetchRouter({
      ...baseRoutes(),
      [`GET ${HISTORY_PATH}`]: () =>
        jsonResponse(historyPayload({ runs: [governedRunPayload()] })),
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    const runs = restrictedElements("review-history-run");
    expect(runs).toHaveLength(1);
    const components = within(runs[0]).getAllByTestId("review-run-component");
    expect(components).toHaveLength(3);
    expect(components[0].textContent).toContain("ruleset");
    expect(components[0].textContent).toContain("component_rules_t42pin");
    expect(components[0].textContent).toContain("55".repeat(32));
    expect(components[1].textContent).toContain("semantic_catalog");
    expect(components[1].textContent).toContain("component_catalog_t42pin");
    expect(components[1].textContent).toContain("66".repeat(32));
    expect(components[2].textContent).toContain("entity_knowledge");
    expect(components[2].textContent).toContain("component_kb_t42pin");
    expect(components[2].textContent).toContain("77".repeat(32));
  });
});

describe("ReviewWorkPanel S11 entity-link explicit choice (T13)", () => {
  const ENTITY_LINK_PATH =
    "/controlled/s01/api/commands/review-work-items/work_t02panel1234567890abcdef/correct-entity-link";
  const S11_MENTION_ORG = "s11_mention_org_pol";
  const S11_MENTION_CITY = "s11_mention_city_lease";
  const S11_MENTION_BRAND = "s11_mention_brand_inv";
  const S11_FINDING_ORG = "finding_s11panel0000000000000004";
  const S11_FINDING_CITY = "finding_s11panel0000000000000005";
  const S11_FINDING_BRAND = "finding_s11panel0000000000000006";
  const S11_SOURCE_EVIDENCE = {
    event_id: "evidence_s11panel",
    evidence_revision: 2,
  };
  const SUCCESSOR_CONTEXT = {
    ...CONTEXT,
    evidence_revision: 2,
    current_context: "7".repeat(64),
  };
  const S11_ORG_CANDIDATES = [
    {
      claim_id: "s11_claim_org_picc",
      entity_id: "org:picc_full",
      entity_type: "insurer",
      label: "中国人民财产保险股份有限公司",
      confidence: 0.97,
      provenance: {
        matcher_id: "c-demo-entity-matcher/1",
        matcher_version: "1",
        knowledge_release_id: "c-demo-entity-knowledge/1",
        method: "alias-longest-key",
        source_pointer: "/documents/1/fields/insured_org",
      },
      knowledge: { same_as: ["org:picc"], conflict_with: [] },
    },
    {
      claim_id: "s11_claim_org_pingan",
      entity_id: "org:pingan_full",
      entity_type: "insurer",
      label: "中国平安财产保险股份有限公司",
      confidence: 0.94,
      provenance: {
        matcher_id: "c-demo-entity-matcher/1",
        matcher_version: "1",
        knowledge_release_id: "c-demo-entity-knowledge/1",
        method: "alias-fuzzy",
        source_pointer: "/documents/1/fields/insured_org",
      },
      knowledge: { same_as: ["org:pingan"], conflict_with: [] },
    },
  ];
  /** The org mention as the workspace selected finding: ambiguous with two
   * coexisting insurer candidates, exact fixture provenance, and no active
   * decision yet.  The Reviewer must pick one claim explicitly. */
  const S11_ORG_ENTITY_LINK = {
    mention_id: S11_MENTION_ORG,
    mention: {
      mention_id: S11_MENTION_ORG,
      entity_type: "insurer",
      document_id: "pol",
      document_role: "交强险保单",
      field: "insured_org",
      raw: "人保财险",
    },
    state: "ambiguous",
    candidates: S11_ORG_CANDIDATES,
    active_decision_ids: [],
    source_evidence: S11_SOURCE_EVIDENCE,
    low_confidence: false,
  };

  function s11EntityLinkWorkspacePayload(
    overrides: Record<string, unknown> = {},
  ): WorkspaceResponse {
    return workspacePayload({
      claim_fence: 1,
      claim_expires_at: LIVE_CLAIM_EXPIRES_AT,
      evidence_revision: 2,
      mandatory_blockers: [
        {
          finding_id: S11_FINDING_ORG,
          run_id: "run_t02panel",
          rule_id: "ENTITY_LINK_AMBIGUOUS",
          verdict: "uncertain",
          severity: "critical",
          reason_code: "ENTITY_LINK_AMBIGUOUS",
          mandatory: true,
          evidence_links: [],
          entity_link: S11_ORG_ENTITY_LINK,
        },
      ],
      selected_finding: {
        finding_id: S11_FINDING_ORG,
        run_id: "run_t02panel",
        rule_id: "ENTITY_LINK_AMBIGUOUS",
        verdict: "uncertain",
        severity: "critical",
        reason_code: "ENTITY_LINK_AMBIGUOUS",
        mandatory: true,
        evidence_links: [],
        entity_link: S11_ORG_ENTITY_LINK,
      },
      entity_link_ledger: [
        {
          mention_id: S11_MENTION_ORG,
          mention: S11_ORG_ENTITY_LINK.mention,
          state: "ambiguous",
          finding_id: S11_FINDING_ORG,
          active_decision_ids: [],
          source_evidence: S11_SOURCE_EVIDENCE,
          candidates: S11_ORG_CANDIDATES,
          decisions: [],
          low_confidence: false,
        },
      ],
      ...overrides,
    });
  }

  function s11EntityLinkRoutes(
    overrides: Record<string, RouteHandler> = {},
  ) {
    return {
      [`GET ${WORK_PATH}`]: () =>
        jsonResponse(
          claimedWorkPayload({
            evidence_revision: 2,
            command_context: SUCCESSOR_CONTEXT,
          }),
        ),
      [`GET ${WORKSPACE_PATH}`]: () =>
        jsonResponse(s11EntityLinkWorkspacePayload()),
      [`GET ${ROUTE_PATH}`]: () =>
        jsonResponse(routePayload({ evidence_revision: 2 })),
      [`GET ${HISTORY_PATH}`]: () =>
        jsonResponse(
          historyPayload({
            runs: [
              {
                ...historyPayload().runs[0],
                evidence_revision: 2,
              },
            ],
          }),
        ),
      ...overrides,
    };
  }

  it("opens the entity-link comparison with an empty disabled candidate and submits one exact S11 command", async () => {
    const router = fetchRouter({
      ...s11EntityLinkRoutes({
        [`POST ${ENTITY_LINK_PATH}`]: () =>
          jsonResponse({
            status: "accepted",
            replayed: false,
            application_id: APP_ID,
            work_item_id: WORK_ID,
            correction_id: "entity_link_s11panel",
            entity_link_decision_id: "decision_s11panel_org",
            candidate_claim_id: "s11_claim_org_pingan",
            mention_id: S11_MENTION_ORG,
            entity_id: "org:pingan_full",
            entity_type: "insurer",
            label: "中国平安财产保险股份有限公司",
            relationship: "same_as",
            cycle: 1,
            invalidated_run_id: "run_t02panel",
            job_id: "job_s11panel",
            phase: "Assembly",
            route: "pending_check",
            lifecycle_revision: 7,
            evidence_revision: 2,
          }),
      }),
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    // The ledger keeps every mention, candidate claim and provenance.
    const ledger = screen.getByTestId("review-entity-link-ledger");
    expect(ledger).toHaveTextContent("s11_claim_org_picc");
    expect(ledger).toHaveTextContent("s11_claim_org_pingan");
    expect(ledger).toHaveTextContent("中国人民财产保险股份有限公司");
    expect(ledger).toHaveTextContent("c-demo-entity-matcher/1");
    expect(ledger).toHaveTextContent("c-demo-entity-knowledge/1");
    // The comparison pane renders the server ambiguous state without
    // changing it.
    expect(screen.getByTestId("review-entity-link")).toHaveTextContent(
      "歧义（多候选并存）",
    );
    await userEvent.click(screen.getByTestId("review-entity-link-start"));
    await screen.findByTestId("review-entity-link-form");
    // An opened draft never preselects a candidate: the native select shows
    // an explicit disabled placeholder, submit stays disabled, and no
    // entity-link POST may leave before an explicit Reviewer choice.
    const candidateSelect = screen.getByRole("combobox", { name: "候选实体" });
    expect(candidateSelect).toHaveValue("");
    expect(
      within(candidateSelect).getByRole("option", {
        name: "请选择候选实体",
      }),
    ).toBeDisabled();
    expect(screen.getByTestId("review-entity-link-submit")).toBeDisabled();
    expect(
      router.calls.filter(
        (call) => call.method === "POST" && call.url === ENTITY_LINK_PATH,
      ),
    ).toHaveLength(0);
    // The Reviewer picks the second candidate by its opaque claim id and a
    // registered reason; array order never selects a winner.
    await userEvent.selectOptions(candidateSelect, "s11_claim_org_pingan");
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "原因" }),
      "ENTITY_LINK_AMBIGUITY_RESOLVED",
    );
    await userEvent.click(screen.getByTestId("review-entity-link-submit"));
    await vi.waitFor(() =>
      expect(
        router.calls.some(
          (call) =>
            call.method === "POST" && call.url === ENTITY_LINK_PATH,
        ),
      ).toBe(true),
    );
    const calls = router.calls.filter(
      (call) => call.method === "POST" && call.url === ENTITY_LINK_PATH,
    );
    expect(calls).toHaveLength(1);
    // The serialized command carries only the generated S11 contract fields:
    // the selected candidate identity, mention/finding/source evidence, the
    // empty predecessor list, the live context/fence/application, and one
    // idempotency key.
    expect(calls[0].body).toEqual({
      application_id: APP_ID,
      expected_fence: 1,
      expected_context: SUCCESSOR_CONTEXT,
      idempotency_key: expect.any(String),
      entity_link: {
        schema_version: "entity-link-correction/1",
        finding_id: S11_FINDING_ORG,
        candidate_claim_id: "s11_claim_org_pingan",
        mention_id: S11_MENTION_ORG,
        source_evidence: S11_SOURCE_EVIDENCE,
        expected_active_decision_ids: [],
        decision: "accept",
        entity_id: "org:pingan_full",
        entity_type: "insurer",
        label: "中国平安财产保险股份有限公司",
        relationship: "same_as",
        matcher_id: "c-demo-entity-matcher/1",
        matcher_version: "1",
        knowledge_release_id: "c-demo-entity-knowledge/1",
        reason_code: "ENTITY_LINK_AMBIGUITY_RESOLVED",
      },
    });
  });

  it("renders unresolved, ambiguous and conflict mentions with low confidence as server states", async () => {
    const CITY_ENTITY_LINK = {
      mention_id: S11_MENTION_CITY,
      mention: {
        mention_id: S11_MENTION_CITY,
        entity_type: "city",
        document_id: "lease",
        document_role: "融资租赁合同",
        field: "city",
        raw: "南京",
      },
      state: "unresolved",
      candidates: [
        {
          claim_id: "s11_claim_city_nanjing",
          entity_id: "addr:nanjing",
          entity_type: "city",
          label: "南京市",
          confidence: 0.5,
          provenance: {
        matcher_id: "c-demo-entity-matcher/1",
        matcher_version: "1",
        knowledge_release_id: "c-demo-entity-knowledge/1",
            method: "alias-longest-key",
            source_pointer: "/documents/2/fields/city",
      },
          knowledge: { same_as: [], conflict_with: [] },
      },
      ],
      active_decision_ids: [],
        source_evidence: S11_SOURCE_EVIDENCE,
      low_confidence: true,
    };
    const BRAND_ENTITY_LINK = {
      mention_id: S11_MENTION_BRAND,
      mention: {
        mention_id: S11_MENTION_BRAND,
        entity_type: "brand",
        document_id: "inv",
        document_role: "发票",
        field: "brand_short",
        raw: "一汽大众",
      },
      state: "conflict",
      candidates: [
        {
          claim_id: "s11_claim_brand_faw",
          entity_id: "brand:faw-vw",
          entity_type: "brand",
          label: "一汽-大众汽车有限公司",
          confidence: 0.98,
          provenance: {
        matcher_id: "c-demo-entity-matcher/1",
        matcher_version: "1",
        knowledge_release_id: "c-demo-entity-knowledge/1",
            method: "alias-longest-key",
            source_pointer: "/documents/3/fields/brand_short",
      },
          knowledge: { same_as: [], conflict_with: ["brand:saic-vw"] },
      },
        {
          claim_id: "s11_claim_brand_saic",
          entity_id: "brand:saic-vw",
          entity_type: "brand",
          label: "上汽大众汽车有限公司",
          confidence: 0.71,
          provenance: {
        matcher_id: "c-demo-entity-matcher/1",
        matcher_version: "1",
        knowledge_release_id: "c-demo-entity-knowledge/1",
            method: "alias-fuzzy",
            source_pointer: "/documents/3/fields/brand_short",
      },
          knowledge: { same_as: [], conflict_with: ["brand:faw-vw"] },
      },
      ],
      active_decision_ids: [],
        source_evidence: S11_SOURCE_EVIDENCE,
      low_confidence: false,
    };
    const router = fetchRouter({
      ...s11EntityLinkRoutes({
        [`GET ${WORKSPACE_PATH}`]: () =>
          jsonResponse(
            s11EntityLinkWorkspacePayload({
              mandatory_blockers: [
                {
                  finding_id: S11_FINDING_CITY,
                  run_id: "run_t02panel",
                  rule_id: "ENTITY_LINK_UNRESOLVED",
                  verdict: "uncertain",
                  severity: "critical",
                  reason_code: "ENTITY_LINK_UNRESOLVED",
                  mandatory: true,
                  evidence_links: [],
                  entity_link: CITY_ENTITY_LINK,
      },
                {
                  finding_id: S11_FINDING_BRAND,
                  run_id: "run_t02panel",
                  rule_id: "ENTITY_LINK_CONFLICT",
                  verdict: "uncertain",
                  severity: "critical",
                  reason_code: "ENTITY_LINK_CONFLICT",
                  mandatory: true,
                  evidence_links: [],
                  entity_link: BRAND_ENTITY_LINK,
      },
              ],
              selected_finding: {
                finding_id: S11_FINDING_CITY,
                run_id: "run_t02panel",
                rule_id: "ENTITY_LINK_UNRESOLVED",
                verdict: "uncertain",
                severity: "critical",
                reason_code: "ENTITY_LINK_UNRESOLVED",
                mandatory: true,
                evidence_links: [],
                entity_link: CITY_ENTITY_LINK,
      },
              entity_link_ledger: [
                {
        mention_id: S11_MENTION_ORG,
                  mention: S11_ORG_ENTITY_LINK.mention,
                  state: "ambiguous",
        finding_id: S11_FINDING_ORG,
                  active_decision_ids: [],
        source_evidence: S11_SOURCE_EVIDENCE,
                  candidates: S11_ORG_CANDIDATES,
                  decisions: [],
                  low_confidence: false,
      },
                {
                  mention_id: S11_MENTION_CITY,
                  mention: CITY_ENTITY_LINK.mention,
                  state: "unresolved",
                  finding_id: S11_FINDING_CITY,
                  active_decision_ids: [],
        source_evidence: S11_SOURCE_EVIDENCE,
                  candidates: CITY_ENTITY_LINK.candidates,
                  decisions: [],
                  low_confidence: true,
      },
                {
                  mention_id: S11_MENTION_BRAND,
                  mention: BRAND_ENTITY_LINK.mention,
                  state: "conflict",
                  finding_id: S11_FINDING_BRAND,
                  active_decision_ids: [],
        source_evidence: S11_SOURCE_EVIDENCE,
                  candidates: BRAND_ENTITY_LINK.candidates,
                  decisions: [],
                  low_confidence: false,
      },
              ],
            }),
          ),
      }),
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    const ledger = screen.getByTestId("review-entity-link-ledger");
    expect(
      ledger.querySelectorAll(
        '[data-testid="review-entity-link-ledger-page"]',
      ),
    ).toHaveLength(3);
    // The ambiguous org mention keeps its server state.
    expect(ledger).toHaveTextContent("歧义（多候选并存）");
    expect(ledger).toHaveTextContent("s11_claim_org_picc");
    expect(ledger).toHaveTextContent("s11_claim_org_pingan");
    // The unresolved low-confidence city mention keeps its server state and
    // the explicit low-confidence marker; no client threshold invents it.
    expect(ledger).toHaveTextContent("未解析");
    expect(ledger).toHaveTextContent("低置信（服务端）");
    expect(ledger).toHaveTextContent("addr:nanjing");
    expect(ledger).toHaveTextContent("0.5");
    // The conflicting brand mention keeps its conflict state and the frozen
    // knowledge-release conflict targets.
    expect(ledger).toHaveTextContent("冲突（别名互斥）");
    expect(ledger).toHaveTextContent("brand:faw-vw");
    expect(ledger).toHaveTextContent("brand:saic-vw");
    expect(ledger).toHaveTextContent("conflict_with brand:saic-vw");
    // The selected city finding renders the low-confidence marker in the
    // comparison pane as a server fact.
    expect(screen.getByTestId("review-entity-link")).toHaveTextContent(
      "未解析",
    );
    expect(
      screen.getByTestId("review-entity-link-low-confidence"),
    ).toHaveTextContent("低置信候选（服务端）");
    // No command may leave from render-only server states.
    expect(router.calls.filter((call) => call.method === "POST")).toHaveLength(
      0,
    );
  });

  it("renders the entity-link ledger, corrections and run pins from server history", async () => {
    const S11_DECISION_ORG = "decision_s11panel_org";
    const S11_DECISION_SUPERSEDING = "decision_s11panel_org_2";
    const ledgerRecord = (overrides: Record<string, unknown>) => ({
      mention: S11_ORG_ENTITY_LINK.mention,
      candidate_entity: {
        entity_id: "org:picc_full",
        entity_type: "insurer",
        label: "中国人民财产保险股份有限公司",
      },
      ...overrides,
    });
    fetchRouter({
      ...s11EntityLinkRoutes({
        [`GET ${HISTORY_PATH}`]: () =>
          jsonResponse(
            historyPayload({
              runs: [
                {
                  ...historyPayload().runs[0],
                  evidence_revision: 3,
                  entity_link_decisions: [
                    {
                      decision_id: S11_DECISION_SUPERSEDING,
                      candidate_claim_id: "s11_claim_org_picc",
                      mention_id: S11_MENTION_ORG,
                      entity_id: "org:picc_full",
                      entity_type: "insurer",
                      label: "中国人民财产保险股份有限公司",
                      relationship: "same_as",
                      evidence_revision: 3,
                      matcher_id: "c-demo-entity-matcher/1",
                      matcher_version: "1",
                      knowledge_release_id: "c-demo-entity-knowledge/1",
                      release_id: "auto_lease@1.9.0",
                      release_digest: "4fdc8736",
                    },
                  ],
                },
              ],
              entity_links: [
                ledgerRecord({
                  record_kind: "candidate",
                  claim_id: "s11_claim_org_picc",
                  confidence: 0.97,
                  provenance: S11_ORG_CANDIDATES[0].provenance,
                  knowledge: S11_ORG_CANDIDATES[0].knowledge,
                }),
                ledgerRecord({
                  record_kind: "accepted",
                  decision_id: S11_DECISION_ORG,
                  link_id: "link_s11panel_org",
                  relationship: "same_as",
                  actor: "t02-reviewer",
                  reason_code: "ENTITY_LINK_AMBIGUITY_RESOLVED",
                  time: 200,
                  cycle: 1,
                  source_evidence: {
                    ...S11_SOURCE_EVIDENCE,
                    candidate_claim_id: "s11_claim_org_picc",
                  },
                  supersedes: [],
                  status: "superseded",
                  claim_id: "s11_claim_org_picc",
                }),
                ledgerRecord({
                  record_kind: "accepted",
                  decision_id: S11_DECISION_SUPERSEDING,
                  link_id: "link_s11panel_org_2",
                  relationship: "same_as",
                  actor: "t02-reviewer",
                  reason_code: "ENTITY_LINK_SOURCE_MISASSIGNED",
                  time: 300,
                  cycle: 2,
                  source_evidence: {
                    ...S11_SOURCE_EVIDENCE,
                    evidence_revision: 3,
                    candidate_claim_id: "s11_claim_org_picc",
                  },
                  supersedes: [S11_DECISION_ORG],
                  status: "active",
                  claim_id: "s11_claim_org_picc",
                  matcher_id: "c-demo-entity-matcher/1",
                  matcher_version: "1",
                  knowledge_release_id: "c-demo-entity-knowledge/1",
                  release_id: "auto_lease@1.9.0",
                  release_digest: "4fdc8736",
                }),
              ],
              entity_link_history: [
                {
                  evidence_revision: 3,
                  event_id: "evidence_s11panel",
                  correction_id: "entity_link_s11panel_2",
                  decision_id: S11_DECISION_SUPERSEDING,
                  candidate_claim_id: "s11_claim_org_picc",
                  mention_id: S11_MENTION_ORG,
                  mention: S11_ORG_ENTITY_LINK.mention,
                  entity_id: "org:picc_full",
                  entity_type: "insurer",
                  label: "中国人民财产保险股份有限公司",
                  relationship: "same_as",
                  source_evidence: {
                    ...S11_SOURCE_EVIDENCE,
                    evidence_revision: 3,
                  },
                  decision: "accept",
                  matcher_id: "c-demo-entity-matcher/1",
                  matcher_version: "1",
                  knowledge_release_id: "c-demo-entity-knowledge/1",
                  reason_code: "ENTITY_LINK_SOURCE_MISASSIGNED",
                  actor: "t02-reviewer",
                  recorded_at: 300,
                  cycle: 2,
                  supersedes: [S11_DECISION_ORG],
                },
              ],
            }),
          ),
      }),
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    // The preserved ledger shows candidate claims and both decisions with
    // their explicit active/superseded server statuses; predecessor links
    // stay navigable in the immutable history.
    const ledger = screen.getByTestId("review-history-entity-links");
    expect(ledger).toHaveTextContent("candidate");
    expect(ledger).toHaveTextContent("s11_claim_org_picc");
    expect(ledger).toHaveTextContent("中国人民财产保险股份有限公司");
    expect(ledger).toHaveTextContent("superseded");
    expect(ledger).toHaveTextContent("active");
    expect(ledger).toHaveTextContent("supersedes decision_s11panel_org");
    // Ordinary history output never re-exposes the raw mention value; the
    // public references (mention id, candidate label, status, provenance)
    // remain navigable.
    expect(ledger.textContent ?? "").not.toContain("人保财险");
    expect(ledger).toHaveTextContent("s11_mention_org_pol");
    // The chronological correction history preserves the reason, cycle and
    // superseded predecessor.
    const corrections = screen.getByTestId(
      "review-history-entity-link-corrections",
    );
    expect(corrections).toHaveTextContent("entity_link_s11panel_2");
    expect(corrections).toHaveTextContent("ENTITY_LINK_SOURCE_MISASSIGNED");
    expect(corrections).toHaveTextContent("周期 2");
    expect(corrections).toHaveTextContent("supersedes decision_s11panel_org");
    // The current run pin carries the full provenance binding.
    const pin = screen.getByTestId("review-run-entity-link-decisions");
    expect(pin).toHaveTextContent("decision_s11panel_org_2");
    expect(pin).toHaveTextContent("matcher c-demo-entity-matcher/1@1");
    expect(pin).toHaveTextContent("knowledge c-demo-entity-knowledge/1");
    expect(pin).toHaveTextContent("release auto_lease@1.9.0@4fdc8736");
  });

  it.each([
    ["stale context 409", 409, "S03_STALE", "STALE_REVIEW_CONTEXT"],
    [
      "release mismatch 422",
      422,
      "S03_REJECTED",
      "ENTITY_LINK_RELEASE_MISMATCH",
    ],
  ])(
    "%s locks entity link into reload-required without an unknown-outcome retry",
    async (_label, status, error, reason) => {
      const router = fetchRouter({
        ...s11EntityLinkRoutes({
          [`POST ${ENTITY_LINK_PATH}`]: () =>
            jsonResponse({ detail: { error, reason_code: reason } }, status),
        }),
      });
      renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
      await waitForReviewReady();
      await userEvent.click(screen.getByTestId("review-entity-link-start"));
      await screen.findByTestId("review-entity-link-form");
      await userEvent.selectOptions(
        screen.getByRole("combobox", { name: "候选实体" }),
        "s11_claim_org_pingan",
      );
      await userEvent.click(screen.getByTestId("review-entity-link-submit"));
      await waitFor(() =>
        expect(screen.getByTestId("review-command-status")).toHaveTextContent(
          `实体链接未接受（${reason}）：请重新加载权威上下文后再试`,
        ),
      );
      // The definitive rejection closes the draft and blocks every command
      // behind the reload fence; the outcome is never offered for retry.
      expect(
        screen.queryByTestId("review-entity-link-form"),
      ).not.toBeInTheDocument();
      expect(screen.queryByTestId("retry-button")).not.toBeInTheDocument();
      for (const name of ["认领", "续期", "释放", "提交人工核验"]) {
        expect(screen.getByRole("button", { name })).toBeDisabled();
      }
      expect(
        router.calls.filter(
          (call) => call.method === "POST" && call.url === ENTITY_LINK_PATH,
        ),
      ).toHaveLength(1);
    },
  );

  it("keeps the exact entity-link body and idempotency key when the transport outcome is unknown", async () => {
    let posts = 0;
    const router = fetchRouter({
      ...s11EntityLinkRoutes({
        [`POST ${ENTITY_LINK_PATH}`]: () => {
          posts += 1;
          if (posts === 1) {
            return Promise.reject(new TypeError("network unreachable"));
          }
          return jsonResponse({
            status: "accepted",
            replayed: false,
            application_id: APP_ID,
            work_item_id: WORK_ID,
            correction_id: "entity_link_s11panel",
            entity_link_decision_id: "decision_s11panel_org",
            candidate_claim_id: "s11_claim_org_pingan",
            mention_id: S11_MENTION_ORG,
            entity_id: "org:pingan_full",
            entity_type: "insurer",
            label: "中国平安财产保险股份有限公司",
            relationship: "same_as",
            cycle: 1,
            invalidated_run_id: "run_t02panel",
            job_id: "job_s11panel",
            phase: "Assembly",
            route: "pending_check",
            lifecycle_revision: 7,
            evidence_revision: 2,
          });
        },
      }),
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitForReviewReady();
    await userEvent.click(screen.getByTestId("review-entity-link-start"));
    await screen.findByTestId("review-entity-link-form");
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "候选实体" }),
      "s11_claim_org_pingan",
    );
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "原因" }),
      "ENTITY_LINK_AMBIGUITY_RESOLVED",
    );
    await userEvent.click(screen.getByTestId("review-entity-link-submit"));
    await waitFor(() =>
      expect(screen.getByTestId("review-command-status")).toHaveTextContent(
        "结果未知：网络未确认，重试将使用同一幂等键",
      ),
    );
    await userEvent.click(screen.getByTestId("retry-button"));
    await vi.waitFor(() =>
      expect(
        router.calls.filter(
          (call) => call.method === "POST" && call.url === ENTITY_LINK_PATH,
        ),
      ).toHaveLength(2),
    );
    const bodies = router.calls
      .filter(
        (call) => call.method === "POST" && call.url === ENTITY_LINK_PATH,
      )
      .map((call) => call.body);
    // The unknown outcome retains the byte-identical command and key; only
    // the acceptance rotates the semantic key.
    expect(bodies[1]).toEqual(bodies[0]);
  });

  it("hides unauthorized entity-link reads behind a sanitized 404 with no candidate identifiers", async () => {
    fetchRouter({
      ...s11EntityLinkRoutes({
        [`GET ${WORKSPACE_PATH}`]: () =>
          jsonResponse({ detail: { error: "S03_NOT_FOUND" } }, 404),
      }),
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitFor(() =>
      expect(screen.getByTestId("review-error")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("review-error")).toHaveTextContent(
      "未找到或无权访问",
    );
    const text = screen.getByTestId("review-panel").textContent ?? "";
    expect(text).not.toContain("s11_claim_org_picc");
    expect(text).not.toContain("org:picc_full");
    expect(text).not.toContain(APP_ID);
  });
});
