import { randomUUID } from "node:crypto";
import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { describe, expect, it, vi } from "vitest";

import ReviewWorkPanel from "./ReviewWorkPanel";
import { MANUAL_WORK_KEY, WORKSPACE_KEY } from "../api/hooks";
import {
  fetchRouter,
  renderWithQuery,
  restrictedDigest,
} from "../test-utils";

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

function claimedWorkPayload() {
  return workPayload({
    status: "claimed",
    claim_subject: "t02-reviewer",
    claim_fence: 1,
    claim_expires_at: LIVE_CLAIM_EXPIRES_AT,
  });
}

function claimedWorkspacePayload() {
  return workspacePayload({
    claim_fence: 1,
    claim_expires_at: LIVE_CLAIM_EXPIRES_AT,
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
    await userEvent.click(screen.getAllByRole("button", { name: "查看来源" })[0]);
    const sources = await findRestrictedElements("review-reveal-source");
    expect(sources.length).toBe(1);
    expectRestrictedEqual(sources[0].textContent, SOURCE_SENTINEL);
    // The restricted value exists in exactly one element of the whole panel.
    const panelText = restrictedElement("review-panel").textContent ?? "";
    expect(panelText.split(SOURCE_SENTINEL).length - 1).toBe(1);
    // The evidence stays masked for both observations.
    for (const masked of screen.getAllByTestId("review-evidence-masked")) {
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
    await userEvent.click(screen.getAllByRole("button", { name: "查看来源" })[0]);
    await findRestrictedElements("review-reveal-source");
    await userEvent.click(screen.getByRole("button", { name: "重新加载" }));
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
    await screen.findByTestId("review-error");
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
    await userEvent.click(screen.getAllByRole("button", { name: "查看来源" })[0]);
    await findRestrictedElements("review-reveal-source");
    await userEvent.click(
      screen.getAllByRole("button", { name: "更正该字段" })[0],
    );
    expect(screen.getByTestId("review-correction-form")).toBeInTheDocument();
    // The renewal extends the claim expiry without touching the fence: the
    // issued authorization (which includes the claim expiry) is no longer the
    // live one, so every restricted holder is scrubbed.
    await userEvent.click(screen.getByRole("button", { name: "续期" }));
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
    await waitFor(() =>
      expect(screen.getByTestId("review-command-status")).toHaveTextContent(
        "结果未知：网络未确认，重试将使用同一幂等键",
      ),
    );
    expect(screen.getByTestId("retry-button")).toBeInTheDocument();
    // An authoritative context change (only the command context differs)
    // while the outcome is unknown must end the replay and clear its raw.
    await act(async () => {
      await client.refetchQueries({ queryKey: MANUAL_WORK_KEY(WORK_ID) });
    });
    await waitFor(() =>
      expect(screen.queryByTestId("retry-button")).not.toBeInTheDocument(),
    );
    expect(
      screen.queryByTestId("review-correction-pending"),
    ).not.toBeInTheDocument();
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
    await userEvent.click(screen.getAllByRole("button", { name: "查看来源" })[0]);
    const pendingMarkers = await screen.findAllByTestId("review-reveal-pending");
    expect(pendingMarkers.length).toBeGreaterThanOrEqual(2);
    expect(pending).toHaveLength(1);
    // Reclaim advances the fence: an authoritative refetch invalidates the
    // first issuance so a second reveal can be issued under the new context.
    await act(async () => {
      await client.refetchQueries({ queryKey: MANUAL_WORK_KEY(WORK_ID) });
    });
    await waitFor(() =>
      expect(
        screen.queryByTestId("review-reveal-pending"),
      ).not.toBeInTheDocument(),
    );
    await userEvent.click(screen.getAllByRole("button", { name: "查看来源" })[0]);
    await waitFor(() => expect(pending).toHaveLength(2));
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
    expect(screen.getAllByTestId("review-reveal-pending").length).toBeGreaterThan(
      0,
    );
    expect(screen.getByTestId("review-command-status")).toHaveTextContent(
      "揭示提交中",
    );
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
      await userEvent.click(
        screen.getAllByRole("button", { name: "更正该字段" })[0],
      );
      const submit = screen.getByRole("button", { name: "提交修正" });
      await userEvent.type(screen.getByTestId("review-correction-raw"), raw);
      await userEvent.click(submit);
    };

    await issueCorrection(`${CORRECTION_SENTINEL}:A`);
    await waitFor(() => expect(pending.length).toBe(1));
    await act(async () => {
      await client.refetchQueries({ queryKey: MANUAL_WORK_KEY(WORK_ID) });
    });
    await waitFor(() =>
      expect(screen.queryByTestId("review-correction-pending")).not.toBeInTheDocument(),
    );
    await issueCorrection(`${CORRECTION_SENTINEL}:B`);
    await waitFor(() => expect(pending.length).toBe(2));

    await act(async () => {
      pending[0](jsonResponse(correctionResultPayload()));
    });
    expect(screen.getByTestId("review-command-status")).toHaveTextContent(
      "更正提交中",
    );
    expect(screen.getByRole("button", { name: "续期" })).toBeDisabled();

    await act(async () => {
      pending[1](jsonResponse(correctionResultPayload()));
    });
    await waitFor(() =>
      expect(screen.getByTestId("review-correction-converged")).toBeInTheDocument(),
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
    await userEvent.click(screen.getAllByRole("button", { name: "查看来源" })[0]);
    await findRestrictedElements("review-reveal-source");
    await userEvent.click(
      screen.getAllByRole("button", { name: "更正该字段" })[0],
    );
    const submit = screen.getByRole("button", { name: "提交修正" });
    expect(submit).toBeDisabled();
    // The raw evidence crosses the boundary byte-for-byte: leading/trailing
    // whitespace is part of the entered value, not trimmed by the client.
    const paddedRaw = `  ${CORRECTION_SENTINEL}  `;
    await userEvent.type(screen.getByTestId("review-correction-raw"), paddedRaw);
    expect(submit).toBeEnabled();
    await userEvent.click(submit);
    await waitFor(() =>
      expect(screen.getByTestId("review-correction-converged")).toHaveTextContent(
        "run_t03succ",
      ),
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
    // The invalidated old workspace/work reads existence-hide (404) but the
    // review shell, the authoritative gate, and history stay usable; the
    // successor convergence is resolved from server-owned route/history.
    expect(
      screen.queryByTestId("review-correction-pending"),
    ).not.toBeInTheDocument();
    expect(screen.getByTestId("review-correction-converged")).toHaveTextContent(
      "证据修订 2",
    );
    expect(screen.getByTestId("review-correction-converged")).toHaveTextContent(
      "run_t03succ",
    );
    expect(screen.getByTestId("gate-phase")).toHaveTextContent("Auto Complete");
    expect(screen.getByTestId("review-history-corrections")).toHaveTextContent(
      "correction_t03panel",
    );
    expect(screen.getByTestId("review-history-corrections")).toHaveTextContent(
      "observation_t02panel → observation_t02panel_succ",
    );
    expect(screen.getByTestId("review-history-corrections")).toHaveTextContent(
      "SOURCE_VALUE_MISREAD",
    );
    expect(screen.getByTestId("review-history-corrections")).toHaveTextContent(
      "证据修订 2",
    );
    const runsText = screen.getByTestId("review-history-runs").textContent ?? "";
    expect(runsText).toContain("run_t02panel");
    expect(runsText).toContain("run_t03succ");
    expect(runsText.match(/· 当前/g)).toHaveLength(1);
    expect(runsText.match(/· 非当前/g)).toHaveLength(1);
    // All manual-review writes are fenced while the successor run converges.
    expect(
      screen.queryByRole("button", { name: "查看来源" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "提交人工核验" }),
    ).not.toBeInTheDocument();
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
    await waitFor(() =>
      expect(screen.getByTestId("review-correction-terminal")).toBeInTheDocument(),
    );
    expect(
      screen.queryByTestId("review-correction-timeout"),
    ).not.toBeInTheDocument();
    expect(
      screen.getByTestId("review-correction-terminal").textContent ?? "",
    ).not.toContain("S03_NOT_FOUND");
    expectRestrictedAbsent(
      screen.getByTestId("review-correction-terminal").textContent,
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
    await userEvent.click(screen.getAllByRole("button", { name: "查看来源" })[0]);
    await findRestrictedElements("review-reveal-source");
    await userEvent.click(
      screen.getAllByRole("button", { name: "更正该字段" })[0],
    );
    const submit = screen.getByRole("button", { name: "提交修正" });
    await userEvent.type(
      screen.getByTestId("review-correction-raw"),
      CORRECTION_SENTINEL,
    );
    await userEvent.click(submit);
    await waitFor(() =>
      expect(screen.getByTestId("review-command-status")).toHaveTextContent(
        "更正未接受（CORRECTION_REJECTED）：请重新加载权威上下文后再试",
      ),
    );
    // A definitive rejection proves no correction committed: the reveal and
    // the form are scrubbed, and no invalidation shell may appear.
    expectRestrictedElementsAbsent("review-correction-form");
    expectRestrictedElementsAbsent("review-reveal-source");
    expectRestrictedAbsent(
      restrictedElement("review-panel").textContent,
      SOURCE_SENTINEL,
    );
    expect(
      screen.queryByTestId("review-correction-pending"),
    ).not.toBeInTheDocument();
    // The restricted raw must not survive in the MutationCache after the
    // definitive rejection scrubbed the panel.
    const cached = client
      .getMutationCache()
      .getAll()
      .map((mutation) => JSON.stringify(mutation.state.variables ?? {}));
    expectRestrictedAbsent(cached.join("\n"), CORRECTION_SENTINEL);
    expect(screen.getByTestId("review-reload-note")).toBeInTheDocument();
    for (const button of screen.getAllByRole("button", { name: "查看来源" })) {
      expect(button).toBeDisabled();
    }
    for (const name of ["认领", "续期", "释放", "提交人工核验"]) {
      expect(screen.getByRole("button", { name })).toBeDisabled();
    }
    expect(router.calls.filter((call) => call.method === "POST").length).toBe(2);
  });
});
