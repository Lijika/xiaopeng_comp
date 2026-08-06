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

function claimedWorkPayload() {
  return workPayload({
    status: "claimed",
    claim_subject: "t02-reviewer",
    claim_fence: 1,
  });
}

function claimedWorkspacePayload() {
  return workspacePayload({ claim_fence: 1 });
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
  await waitFor(() =>
    expect(screen.getByTestId("review-workspace-rule")).toBeInTheDocument(),
  );
  await screen.findByTestId("gate-phase");
  await screen.findByTestId("review-history-decisions");
  await screen.findByTestId("review-status");
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
    const panelText = screen.getByTestId("review-panel").textContent ?? "";
    expect(panelText).not.toContain("Verification Completed");
    expect(panelText).not.toContain("human_complete");
    expect(panelText).not.toContain("S2ENG54A");
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
    const panelText = screen.getByTestId("review-panel").textContent ?? "";
    expect(panelText).not.toContain("S2ENG54A");
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
    expect(router.calls.filter((call) => call.method === "POST")).toHaveLength(1);
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

  it("renders an explicit workspace error instead of an endless load for an initial 404", async () => {
    fetchRouter({
      ...baseRoutes(),
      [`GET ${WORKSPACE_PATH}`]: () =>
        jsonResponse({ detail: { error: "S03_NOT_FOUND" } }, 404),
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitFor(() =>
      expect(screen.getByTestId("review-workspace-error")).toHaveTextContent(
        "工作区未找到或无权访问",
      ),
    );
    expect(
      screen.queryByTestId("review-workspace-loading"),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "认领" })).toBeDisabled();
  });

  it("renders an explicit history error and keeps writes fenced on an initial 404", async () => {
    fetchRouter({
      ...baseRoutes(),
      [`GET ${HISTORY_PATH}`]: () =>
        jsonResponse({ detail: { error: "S03_NOT_FOUND" } }, 404),
    });
    renderWithQuery(<ReviewWorkPanel workId={WORK_ID} />);
    await waitFor(() =>
      expect(screen.getByTestId("review-history-error")).toHaveTextContent(
        "历史未找到或无权访问",
      ),
    );
    expect(screen.getByRole("button", { name: "认领" })).toBeDisabled();
  });

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
