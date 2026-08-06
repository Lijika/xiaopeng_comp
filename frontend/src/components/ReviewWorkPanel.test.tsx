import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { describe, expect, it } from "vitest";

import ReviewWorkPanel from "./ReviewWorkPanel";
import { fetchRouter, renderWithQuery } from "../test-utils";

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
        verdict: "inconsistent",
        severity: "critical",
        reason_code: "ENGINE_MISMATCH",
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
        verdict: "inconsistent",
        severity: "critical",
        reason_code: "ENGINE_MISMATCH",
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
      verdict: "inconsistent",
      severity: "critical",
      reason_code: "ENGINE_MISMATCH",
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

function claimedWorkPayload() {
  return workPayload({
    status: "claimed",
    claim_subject: "t02-reviewer",
    claim_fence: 1,
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
  });

  it("renders server-owned facts, the finding-first workspace, and never derives lifecycle", async () => {
    const router = fetchRouter(baseRoutes());
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitFor(() =>
      expect(screen.getByTestId("review-status")).toHaveTextContent("unclaimed"),
    );
    await screen.findByTestId("review-workspace-rule");
    await screen.findByTestId("gate-phase");
    await screen.findByTestId("review-history-decisions");
    expect(screen.getByTestId("review-phase")).toHaveTextContent("Manual Review");
    expect(screen.getByTestId("review-route")).toHaveTextContent("manual_review");
    expect(screen.getByTestId("review-claim-fence")).toHaveTextContent("0");
    expect(screen.getByTestId("review-finding-rule")).toHaveTextContent(
      "R_ENGINE_CROSS",
    );
    expect(screen.getByTestId("review-finding-verdict")).toHaveTextContent(
      "inconsistent",
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
    const panelText = screen.getByTestId("review-panel").textContent ?? "";
    expect(panelText).not.toContain("Verification Completed");
    expect(panelText).not.toContain("human_complete");
    expect(panelText).not.toContain("S2ENG54A");
    expect(router.calls.filter((call) => call.method === "GET")).toHaveLength(4);
  });

  it("claims with exactly the generated claim contract and acknowledges the accepted fence", async () => {
    let workRequests = 0;
    const routes = {
      ...baseRoutes(),
      [`GET ${WORK_PATH}`]: () =>
        jsonResponse(
          workRequests++ === 0
            ? workPayload()
            : claimedWorkPayload(),
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
    await waitFor(() =>
      expect(screen.getByTestId("review-status")).toHaveTextContent("unclaimed"),
    );
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
      [`GET ${WORKSPACE_PATH}`]: () =>
        jsonResponse(
          workspacePayload({ claim_fence: 1, claim_expires_at: 1786000000 }),
        ),
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
    await waitFor(() =>
      expect(screen.getByTestId("review-status")).toHaveTextContent("claimed"),
    );
    await userEvent.click(screen.getByRole("button", { name: "提交人工核验" }));
    await waitFor(() =>
      expect(screen.getByTestId("review-command-status")).toHaveTextContent(
        "结果未知：网络未确认，重试将使用同一幂等键",
      ),
    );
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

  it("rejects a stale command with a definitive conflict and requires an authoritative reload", async () => {
    const routes = {
      ...baseRoutes(),
      [`GET ${WORK_PATH}`]: () => jsonResponse(claimedWorkPayload()),
      [`GET ${WORKSPACE_PATH}`]: () =>
        jsonResponse(workspacePayload({ claim_fence: 1 })),
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
    await waitFor(() =>
      expect(screen.getByTestId("review-status")).toHaveTextContent("claimed"),
    );
    await userEvent.click(screen.getByRole("button", { name: "续期" }));
    await waitFor(() =>
      expect(screen.getByTestId("review-command-status")).toHaveTextContent(
        "续期未接受（STALE_WORK_ITEM_CLAIM）：请重新加载权威上下文后再试",
      ),
    );
    expect(screen.getByTestId("review-reload-note")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "续期" }),
    ).toBeDisabled();
  });

  it("submits a verification that covers every automatic finding and contains no automatic verdict", async () => {
    const routes = {
      ...baseRoutes(),
      [`GET ${WORK_PATH}`]: () => jsonResponse(claimedWorkPayload()),
      [`GET ${WORKSPACE_PATH}`]: () =>
        jsonResponse(workspacePayload({ claim_fence: 1 })),
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
    await waitFor(() =>
      expect(screen.getByTestId("review-status")).toHaveTextContent("claimed"),
    );
    await userEvent.click(screen.getByRole("button", { name: "提交人工核验" }));
    await waitFor(() =>
      expect(screen.getByTestId("review-command-status")).toHaveTextContent(
        "核验已接受",
      ),
    );
    const submitCall = router.calls.find((call) => call.method === "POST");
    const verification = (submitCall?.body as { verification: Record<string, unknown> })
      .verification;
    expect(verification).toEqual({
      schema_version: "human-decision/1",
      outcome: "confirmed",
      reason_code: "HUMAN_REVIEW_COMPLETED",
      finding_decisions: [{ finding_id: FINDING_ID, outcome: "confirmed" }],
    });
    const serialized = JSON.stringify(verification);
    expect(serialized).not.toContain("verdict");
    expect(serialized).not.toContain("route");
    expect(serialized).not.toContain("target");
  });

  it("treats a 503 unavailable authority as a definitive rejection and requires reload", async () => {
    const routes = {
      ...baseRoutes(),
      [`POST ${CLAIM_PATH}`]: () =>
        jsonResponse(
          {
            detail: {
              error: "S03_UNAVAILABLE",
              reason_code: "AUDIT_UNAVAILABLE",
            },
          },
          503,
        ),
    };
    fetchRouter(routes);
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitFor(() =>
      expect(screen.getByTestId("review-status")).toHaveTextContent("unclaimed"),
    );
    await userEvent.click(screen.getByRole("button", { name: "认领" }));
    await waitFor(() =>
      expect(screen.getByTestId("review-command-status")).toHaveTextContent(
        "认领未接受（AUDIT_UNAVAILABLE）：请重新加载权威上下文后再试",
      ),
    );
    expect(screen.getByTestId("review-reload-note")).toBeInTheDocument();
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
    await waitFor(() =>
      expect(screen.getByTestId("review-status")).toHaveTextContent("unclaimed"),
    );
    await userEvent.click(screen.getByRole("button", { name: "认领" }));
    await waitFor(() =>
      expect(screen.getByTestId("review-command-status")).toHaveTextContent(
        "结果未知：网络未确认，重试将使用同一幂等键",
      ),
    );
    await userEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() =>
      expect(screen.getByTestId("review-command-status")).toHaveTextContent(
        "认领已接受",
      ),
    );
    expect(screen.getByTestId("review-status")).toHaveTextContent("claimed");
    expect(screen.getByTestId("review-claim-fence")).toHaveTextContent("1");
    expect(router.calls.filter((call) => call.method === "POST")).toHaveLength(1);
  });

  it("shows the completed gate and server-owned history once the work is done", async () => {
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
    expect(screen.getByTestId("review-workspace-gone")).toBeInTheDocument();
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
});
