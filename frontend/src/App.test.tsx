import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import App from "./App";
import { fetchRouter, renderWithQuery } from "./test-utils";
import { S13_APPLICATION_ID, s13QueryPayload } from "./test-fixtures/s13";

// The canonical Reviewer workbench path is the baseline for tests that render
// <App /> without choosing a shell pathname: the jsdom default URL ("/") now
// mounts the demo shell, so every controlled-shell render must start from
// /controlled/s01.
beforeEach(() => {
  window.history.replaceState(null, "", "/controlled/s01");
});

const WORK_ID = "recovery_work_t01queue1234567890abcdef";

function queuePayload() {
  return {
    items: [],
    recovery_items: [
      {
        recovery_work_id: WORK_ID,
        application_id: "app_t01queue9876543210fedcba",
        status: "open",
        phase: "Unprocessable",
        primary_reason_code: "configuration.checker_unavailable",
        responsible_party: "policy_owner",
        lifecycle_revision: 5,
        projection_watermark: 1,
      },
    ],
    projection_watermark: 1,
  };
}

function workPayload(overrides: Record<string, unknown> = {}) {
  return {
    schema_version: "recovery-work-view/1",
    recovery_work_id: WORK_ID,
    status: "open",
    application_id: "app_t01queue9876543210fedcba",
    cycle: 1,
    phase: "Unprocessable",
    route: "unprocessable",
    lifecycle_revision: 5,
    evidence_revision: 1,
    primary_reason_code: "configuration.checker_unavailable",
    related_reason_codes: [],
    operation: "execute_check_run",
    dependency: "c-demo-target-checker",
    logical_operation_id: "job_t01queue000000000000000000",
    attempts: [
      {
        attempt: 1,
        classification: "terminal",
        status: "blocked",
        started_at: 10,
        retry_not_before: null,
      },
    ],
    responsible_party: "policy_owner",
    recovery_action: "restore_exact_release_or_activate_compatible_successor",
    recovery_target: "Evidence Ready",
    criterion: {
      id: "s07-checker-compatibility/1",
      version: "1",
      operation: "execute_check_run",
      dependency: "c-demo-target-checker",
      required_conditions: ["configuration.checker_unavailable"],
      trusted_verifier: "policy_owner",
      evidence_kind: "checker_compatibility_probe",
      conditions: [
        { condition_id: "s07-checker-compatibility/1", reason_code: "configuration.checker_unavailable" },
      ],
      digest: "a".repeat(64),
    },
    retry_policy: {
      id: "s07-c-demo-retry/1",
      max_attempts: 3,
      retry_offsets_seconds: [1, 2],
      jitter: false,
    },
    outcome_known: true,
    retryable: false,
    recovery_fact_count: 0,
    resolution_count: 0,
    job_status: "blocked",
    delivery_semantics: "at_least_once",
    protected_business_revision: 0,
    current_run_id: null,
    projection_watermark: 1,
    can_verify: false,
    ...overrides,
  };
}

describe("queue shell (App)", () => {
  it("shows an explicit loading state while the queue is in flight", () => {
    const router = fetchRouter({
      "GET /controlled/s01/api/queries/queue": () =>
        new Promise(() => {
          // The bounded pending promise owns the loading state.
        }),
    });
    renderWithQuery(<App />);
    expect(screen.getByTestId("queue-loading")).toBeInTheDocument();
    expect(router.calls.length).toBe(1);
  });

  it("shows an explicit empty state with the server-owned watermark", async () => {
    fetchRouter({
      "GET /controlled/s01/api/queries/queue": () =>
        new Response(
          JSON.stringify({
            items: [],
            recovery_items: [],
            projection_watermark: 0,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderWithQuery(<App />);
    await waitFor(() =>
      expect(screen.getByTestId("queue-empty")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("queue-watermark")).toHaveTextContent("0");
  });

  it("announces a synced queue when only manual work is present", async () => {
    fetchRouter({
      "GET /controlled/s01/api/queries/queue": () =>
        new Response(
          JSON.stringify({
            items: [
              {
                application_id: "app_t01manual9876543210fedcba",
                work_item_id: "work_t01manual1234567890abcdef",
                assigned_subject: "c-demo-test-user",
                claim_fence: 1,
                claim_expires_at: 9999999999,
                phase: "Manual Review",
                route: "manual_review",
                evidence_ready: true,
                mandatory_blockers: [],
                lifecycle_revision: 5,
                evidence_revision: 1,
                projection_watermark: 1,
              },
            ],
            recovery_items: [],
            projection_watermark: 1,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderWithQuery(<App />);
    await waitFor(() =>
      expect(screen.getByTestId("queue-status")).toHaveTextContent("队列已同步"),
    );
    expect(screen.getByTestId("queue-status").textContent).not.toContain(
      "队列为空",
    );
    expect(screen.queryByTestId("queue-empty")).not.toBeInTheDocument();
    expect(screen.getByTestId("queue-items")).toBeInTheDocument();
  });

  it("shows an explicit error state without echoing identifiers", async () => {
    fetchRouter({
      "GET /controlled/s01/api/queries/queue": () =>
        new Response(
          JSON.stringify({ detail: { error: "S01_INTERNAL_ERROR" } }),
          { status: 500, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderWithQuery(<App />);
    await waitFor(() =>
      expect(screen.getByTestId("queue-error")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("queue-error")).toHaveTextContent("队列不可用");
    expect(screen.getByTestId("queue-error").textContent).not.toContain(
      "S01_INTERNAL_ERROR",
    );
  });

  it("shows an explicit session-expired state without leaking work existence", async () => {
    const router = fetchRouter({
      "GET /controlled/s01/api/queries/queue": () =>
        new Response(
          JSON.stringify({
            items: [],
            recovery_items: [],
            projection_watermark: 0,
            access_ended: true,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderWithQuery(<App />);
    await waitFor(() =>
      expect(screen.getByTestId("queue-access-ended")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("queue-access-ended")).toHaveTextContent(
      "会话已过期",
    );
    expect(screen.queryByTestId("queue-empty")).not.toBeInTheDocument();
    expect(screen.queryByTestId("queue-recovery-empty")).not.toBeInTheDocument();
    expect(screen.queryByTestId("queue-recovery-items")).not.toBeInTheDocument();
    expect(screen.getByTestId("queue-status")).toHaveTextContent("会话已过期");
    expect(screen.getByTestId("queue-access-ended")).toHaveAttribute(
      "role",
      "alert",
    );
    expect(router.calls).toHaveLength(1);
  });

  it("syncs panel selection on popstate and removes the listener on unmount", async () => {
    const REVIEW_ID = "work_t03history1234567890abcdef";
    const addListener = vi.spyOn(window, "addEventListener");
    const removeListener = vi.spyOn(window, "removeEventListener");
    fetchRouter({
      "GET /controlled/s01/api/queries/queue": () =>
        new Response(JSON.stringify(queuePayload()), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      "GET /controlled/s01/api/queries/recovery-work-items/recovery_work_t01queue1234567890abcdef":
        () =>
          new Response(JSON.stringify(workPayload()), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
      [`GET /controlled/s01/api/queries/review-work-items/${REVIEW_ID}`]: () =>
        new Promise(() => {
          // The loading panel is enough to prove the URL-selected owner mounted.
        }),
    });
    window.history.replaceState(null, "", `?work=${encodeURIComponent(WORK_ID)}`);
    const view = renderWithQuery(<App />);
    await waitFor(() =>
      expect(screen.getByTestId("recovery-panel")).toBeInTheDocument(),
    );

    const popstateListener = addListener.mock.calls.find(
      ([type]) => type === "popstate",
    )?.[1];
    expect(popstateListener).toBeTypeOf("function");

    window.history.pushState(null, "", `?review=${encodeURIComponent(REVIEW_ID)}`);
    window.dispatchEvent(new PopStateEvent("popstate"));
    await waitFor(() => {
      expect(screen.getByTestId("review-panel")).toBeInTheDocument();
      expect(screen.queryByTestId("recovery-panel")).not.toBeInTheDocument();
    });

    window.history.pushState(null, "", `?work=${encodeURIComponent(WORK_ID)}`);
    window.dispatchEvent(new PopStateEvent("popstate"));
    await waitFor(() => {
      expect(screen.getByTestId("recovery-panel")).toBeInTheDocument();
      expect(screen.queryByTestId("review-panel")).not.toBeInTheDocument();
    });

    view.unmount();
    expect(removeListener).toHaveBeenCalledWith("popstate", popstateListener);
  });

  it("lists server-owned recovery items and opens the work view on click", async () => {
    const router = fetchRouter({
      "GET /controlled/s01/api/queries/queue": () =>
        new Response(JSON.stringify(queuePayload()), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      "GET /controlled/s01/api/queries/recovery-work-items/recovery_work_t01queue1234567890abcdef":
        () =>
          new Response(JSON.stringify(workPayload()), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
    });
    renderWithQuery(<App />);
    const link = await screen.findByRole("link", { name: new RegExp(WORK_ID) });
    expect(link).toHaveAttribute(
      "href",
      expect.stringContaining(encodeURIComponent(WORK_ID)),
    );
    await userEvent.click(link);
    await waitFor(() =>
      expect(screen.getByTestId("recovery-panel")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("recovery-status")).toHaveTextContent("open");
    expect(screen.getByTestId("recovery-phase")).toHaveTextContent(
      "Unprocessable",
    );
    expect(screen.getByTestId("recovery-primary-reason")).toHaveTextContent(
      "configuration.checker_unavailable",
    );
    expect(screen.getByTestId("recovery-attempts")).toHaveTextContent(
      "1 · terminal · blocked",
    );
    expect(screen.getByTestId("recovery-criterion-digest")).toHaveTextContent(
      /^[0-9a-f]{64}$/,
    );
    expect(screen.getByTestId("recovery-panel").textContent).not.toContain(
      "recovered",
    );
    const workRequests = router.calls.filter((call) =>
      call.url.includes("recovery-work-items"),
    );
    expect(workRequests).toHaveLength(1);
    expect(
      screen.getByRole("heading", { level: 2, name: /恢复工作/ }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("recovery-command-status")).toHaveAttribute(
      "role",
      "status",
    );
  });

  it("unmounts the open work detail and hides cached facts when the session access ends", async () => {
    const router = fetchRouter({
      "GET /controlled/s01/api/queries/queue": () =>
        new Response(
          JSON.stringify({
            items: [],
            recovery_items: [],
            projection_watermark: 0,
            access_ended: true,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      "GET /controlled/s01/api/queries/recovery-work-items/recovery_work_t01queue1234567890abcdef":
        () =>
          new Response(JSON.stringify(workPayload({ can_verify: true })), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
    });
    window.history.pushState(null, "", `?work=${encodeURIComponent(WORK_ID)}`);
    renderWithQuery(<App />);
    await waitFor(() =>
      expect(screen.getByTestId("queue-access-ended")).toBeInTheDocument(),
    );
    await waitFor(() =>
      expect(screen.queryByTestId("recovery-panel")).not.toBeInTheDocument(),
    );
    const bodyText = document.body.textContent ?? "";
    expect(bodyText).not.toContain(WORK_ID);
    expect(bodyText).not.toContain("app_t01queue9876543210fedcba");
    expect(router.calls.filter((call) => call.url.includes("recovery-work-items"))).toHaveLength(1);
  });

  it("isolates command state when navigating from work A to work B after a conflict", async () => {
    const WORK_B = "recovery_work_t01queueB_234567890abcdef";
    const router = fetchRouter({
      "GET /controlled/s01/api/queries/queue": () =>
        new Response(
          JSON.stringify({
            items: [],
            recovery_items: [
              { ...queuePayload().recovery_items[0], recovery_work_id: WORK_ID },
              {
                recovery_work_id: WORK_B,
                application_id: "app_t01queueB9876543210fedcba",
                status: "open",
                phase: "Unprocessable",
                primary_reason_code: "configuration.checker_unavailable",
                responsible_party: "policy_owner",
                lifecycle_revision: 5,
                projection_watermark: 1,
              },
            ],
            projection_watermark: 1,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      "GET /controlled/s01/api/queries/recovery-work-items/recovery_work_t01queue1234567890abcdef":
        () =>
          new Response(
            JSON.stringify(workPayload({ can_verify: true })),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
      "GET /controlled/s01/api/queries/recovery-work-items/recovery_work_t01queueB_234567890abcdef":
        () =>
          new Response(
            JSON.stringify(
              workPayload({ recovery_work_id: WORK_B, application_id: "app_t01queueB9876543210fedcba", can_verify: true }),
            ),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
      "POST /controlled/s01/api/commands/recovery-work-items/recovery_work_t01queue1234567890abcdef/verify":
        () =>
          new Response(
            JSON.stringify({
              detail: { error: "S07_STALE", reason_code: "recovery.context_changed" },
            }),
            { status: 409, headers: { "Content-Type": "application/json" } },
          ),
    });
    window.history.pushState(null, "", `?work=${encodeURIComponent(WORK_ID)}`);
    renderWithQuery(<App />);
    const verifyButton = () =>
      screen.getByRole("button", { name: "验证恢复" });
    await waitFor(() => expect(verifyButton()).toBeEnabled(), { timeout: 3000 });
    await userEvent.click(verifyButton());
    await waitFor(() =>
      expect(screen.getByTestId("recovery-command-status")).toHaveTextContent(
        "recovery.context_changed",
      ),
    );
    expect(verifyButton()).toBeDisabled();

    await userEvent.click(screen.getByRole("link", { name: new RegExp(WORK_B) }));
    await waitFor(
      () => expect(screen.getByTestId("recovery-status")).toHaveTextContent("open"),
      { timeout: 3000 },
    );
    expect(screen.getByTestId("recovery-command-status")).toHaveTextContent(
      "等待操作",
    );
    expect(screen.getByTestId("recovery-panel").textContent).not.toContain(
      "context_changed",
    );
    expect(verifyButton()).toBeEnabled();
    expect(router.calls.filter((call) => call.url.includes("queueB"))).toHaveLength(1);
  });

  it("isolates command state when navigating from work A to work B after an unknown outcome", async () => {
    const WORK_B = "recovery_work_t01queueC_234567890abcdef";
    fetchRouter({
      "GET /controlled/s01/api/queries/queue": () =>
        new Response(
          JSON.stringify({
            items: [],
            recovery_items: [
              { ...queuePayload().recovery_items[0], recovery_work_id: WORK_ID },
              {
                recovery_work_id: WORK_B,
                application_id: "app_t01queueC9876543210fedcba",
                status: "open",
                phase: "Unprocessable",
                primary_reason_code: "configuration.checker_unavailable",
                responsible_party: "policy_owner",
                lifecycle_revision: 5,
                projection_watermark: 1,
              },
            ],
            projection_watermark: 1,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      "GET /controlled/s01/api/queries/recovery-work-items/recovery_work_t01queue1234567890abcdef":
        () =>
          new Response(
            JSON.stringify(workPayload({ can_verify: true })),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
      "GET /controlled/s01/api/queries/recovery-work-items/recovery_work_t01queueC_234567890abcdef":
        () =>
          new Response(
            JSON.stringify(
              workPayload({ recovery_work_id: WORK_B, application_id: "app_t01queueC9876543210fedcba", can_verify: true }),
            ),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
      "POST /controlled/s01/api/commands/recovery-work-items/recovery_work_t01queue1234567890abcdef/verify":
        () => Promise.reject(new TypeError("fetch failed: connection reset")),
    });
    window.history.pushState(null, "", `?work=${encodeURIComponent(WORK_ID)}`);
    renderWithQuery(<App />);
    const verifyButton = () =>
      screen.getByRole("button", { name: "验证恢复" });
    await waitFor(() => expect(verifyButton()).toBeEnabled());
    await userEvent.click(verifyButton());
    await waitFor(() =>
      expect(screen.getByTestId("recovery-command-status")).toHaveTextContent(
        "结果未知",
      ),
    );
    expect(
      screen.getByRole("button", { name: "重试" }),
    ).toBeInTheDocument();

    await userEvent.click(screen.getByRole("link", { name: new RegExp(WORK_B) }));
    await waitFor(() =>
      expect(screen.getByTestId("recovery-status")).toHaveTextContent("open"),
    );
    expect(screen.getByTestId("recovery-command-status")).toHaveTextContent(
      "等待操作",
    );
    expect(
      screen.queryByRole("button", { name: "重试" }),
    ).not.toBeInTheDocument();
    expect(verifyButton()).toBeEnabled();
  });

  it("isolates command state when navigating from work A to work B after an acceptance", async () => {
    const WORK_B = "recovery_work_t01queueD_234567890abcdef";
    fetchRouter({
      "GET /controlled/s01/api/queries/queue": () =>
        new Response(
          JSON.stringify({
            items: [],
            recovery_items: [
              { ...queuePayload().recovery_items[0], recovery_work_id: WORK_ID },
              {
                recovery_work_id: WORK_B,
                application_id: "app_t01queueD9876543210fedcba",
                status: "open",
                phase: "Unprocessable",
                primary_reason_code: "configuration.checker_unavailable",
                responsible_party: "policy_owner",
                lifecycle_revision: 5,
                projection_watermark: 1,
              },
            ],
            projection_watermark: 1,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      "GET /controlled/s01/api/queries/recovery-work-items/recovery_work_t01queue1234567890abcdef":
        () =>
          new Response(
            JSON.stringify(workPayload({ can_verify: true })),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
      "GET /controlled/s01/api/queries/recovery-work-items/recovery_work_t01queueD_234567890abcdef":
        () =>
          new Response(
            JSON.stringify(
              workPayload({ recovery_work_id: WORK_B, application_id: "app_t01queueD9876543210fedcba", can_verify: true }),
            ),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
      "POST /controlled/s01/api/commands/recovery-work-items/recovery_work_t01queue1234567890abcdef/verify":
        () =>
          new Response(
            JSON.stringify({
              status: "accepted",
              replayed: false,
              recovery_work_id: WORK_ID,
              recovery_fact_id: "fact-t01-app-a",
              application_id: "app_t01queue9876543210fedcba",
              phase: "Evidence Ready",
              lifecycle_revision: 6,
              evidence_revision: 1,
              successor_job_id: "job_t01successor00000000000000",
              successor_fence: 2,
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
    });
    window.history.pushState(null, "", `?work=${encodeURIComponent(WORK_ID)}`);
    renderWithQuery(<App />);
    const verifyButton = () =>
      screen.getByRole("button", { name: "验证恢复" });
    await waitFor(() => expect(verifyButton()).toBeEnabled());
    await userEvent.click(verifyButton());
    await waitFor(() =>
      expect(screen.getByTestId("recovery-command-status")).toHaveTextContent(
        "恢复事实已接受",
      ),
    );
    expect(verifyButton()).toBeDisabled();

    await userEvent.click(screen.getByRole("link", { name: new RegExp(WORK_B) }));
    await waitFor(() =>
      expect(screen.getByTestId("recovery-status")).toHaveTextContent("open"),
    );
    expect(screen.getByTestId("recovery-command-status")).toHaveTextContent(
      "等待操作",
    );
    expect(screen.getByTestId("recovery-panel").textContent).not.toContain(
      "恢复事实已接受",
    );
    expect(verifyButton()).toBeEnabled();
  });

  it("lists server-owned manual items as links and opens the review panel on click", async () => {
    const WORK_ID_MANUAL = "work_t01manual1234567890abcdef";
    const APP_ID_MANUAL = "app_t01manual9876543210fedcba";
    const FINDING_ID_MANUAL = "finding_t01manual00000000000001";
    fetchRouter({
      "GET /controlled/s01/api/queries/queue": () =>
        new Response(
          JSON.stringify({
            items: [
              {
                application_id: APP_ID_MANUAL,
                work_item_id: WORK_ID_MANUAL,
                assigned_subject: "c-demo-test-user",
                claim_fence: 0,
                claim_expires_at: 0,
                phase: "Manual Review",
                route: "manual_review",
                evidence_ready: true,
                mandatory_blockers: [
                  {
                    finding_id: FINDING_ID_MANUAL,
                    rule_id: "R_ENGINE_CROSS",
                    reason_code: "ENGINE_MISMATCH",
                    severity: "critical",
                  },
                ],
                lifecycle_revision: 6,
                evidence_revision: 1,
                projection_watermark: 1,
              },
            ],
            recovery_items: [],
            projection_watermark: 1,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      [`GET /controlled/s01/api/queries/review-work-items/${WORK_ID_MANUAL}`]:
        () =>
          new Response(
            JSON.stringify({
              status: "unclaimed",
              application_id: APP_ID_MANUAL,
              work_item_id: WORK_ID_MANUAL,
              claim_subject: null,
              claim_fence: 0,
              claim_expires_at: 0,
              phase: "Manual Review",
              route: "manual_review",
              lifecycle_revision: 6,
              evidence_revision: 1,
              command_context: { current_context: "a".repeat(64) },
              automatic_findings: [
                {
                  finding_id: FINDING_ID_MANUAL,
                  rule_id: "R_ENGINE_CROSS",
                  verdict: "inconsistent",
                  severity: "critical",
                  reason_code: "ENGINE_MISMATCH",
                },
              ],
              run_authority: {
                run_id: "run_t01manual",
                status: "complete",
                authority_digest: "b".repeat(64),
              },
              decision: null,
              decisions: [],
              completed_finding_ids: [],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
      [`GET /controlled/s01/api/queries/applications/${APP_ID_MANUAL}/workspace`]:
        () =>
          new Response(
            JSON.stringify({
              application_id: APP_ID_MANUAL,
              work_item_id: WORK_ID_MANUAL,
              assigned_subject: "c-demo-test-user",
              claim_fence: 0,
              claim_expires_at: 0,
              track: "C-DEMO",
              phase: "Manual Review",
              route: "manual_review",
              evidence_ready: true,
              lifecycle_revision: 6,
              evidence_revision: 1,
              current_run_id: "run_t01manual",
              evidence_snapshot_id: "snapshot_t01manual",
              evidence_snapshot_digest: "c".repeat(64),
              projection_watermark: 1,
              mandatory_blockers: [],
              selected_finding: {
                finding_id: FINDING_ID_MANUAL,
                run_id: "run_t01manual",
                rule_id: "R_ENGINE_CROSS",
                verdict: "inconsistent",
                severity: "critical",
                reason_code: "ENGINE_MISMATCH",
                mandatory: true,
                evidence_links: [],
              },
              actions: ["read_evidence"],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
      [`GET /controlled/s01/api/queries/applications/${APP_ID_MANUAL}/current-route`]:
        () =>
          new Response(
            JSON.stringify({
              schema_version: "s04-current-route/1",
              application_id: APP_ID_MANUAL,
              phase: "Manual Review",
              route: "manual_review",
              current_run_id: "run_t01manual",
              cycle: 1,
              lifecycle_revision: 6,
              evidence_revision: 1,
              evidence_snapshot_id: "snapshot_t01manual",
              evidence_snapshot_digest: "c".repeat(64),
              release_id: "auto_lease@1.9.0",
              release_digest: "d".repeat(64),
              checker_build: "s01-target-checker/6",
              currentness_reason: "CURRENT_CONTEXT_MATCH",
              completion_basis: null,
              exception_id: null,
              exception_decision_id: null,
              exception_expires_at: null,
              failure: null,
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
      [`GET /controlled/s01/api/queries/applications/${APP_ID_MANUAL}/history`]:
        () =>
          new Response(
            JSON.stringify({
              schema_version: "s04-application-history/1",
              application_id: APP_ID_MANUAL,
              current_run_id: "run_t01manual",
              runs: [
                {
                  run_id: "run_t01manual",
                  status: "complete",
                  authority_digest: "b".repeat(64),
                  current: true,
                  currentness_reason: "CURRENT_CONTEXT_MATCH",
                  cycle: 1,
                  lifecycle_revision: 6,
                  evidence_revision: 1,
                  evidence_snapshot_id: "snapshot_t01manual",
                  evidence_snapshot_digest: "c".repeat(64),
                  release_id: "auto_lease@1.9.0",
                  release_digest: "d".repeat(64),
                  checker_build: "s01-target-checker/6",
                  finding_ids: [FINDING_ID_MANUAL],
                  cas_mismatches: [],
                  selected_observation_ids: [],
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
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
    });
    renderWithQuery(<App />);
    const link = await screen.findByRole("link", {
      name: new RegExp(WORK_ID_MANUAL),
    });
    expect(link).toHaveAttribute(
      "href",
      expect.stringContaining(encodeURIComponent(WORK_ID_MANUAL)),
    );
    expect(screen.getByTestId("queue-manual-link")).toBeInTheDocument();
    await userEvent.click(link);
    await waitFor(() =>
      expect(screen.getByTestId("review-panel")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("review-status")).toHaveTextContent("unclaimed");
    expect(screen.getByTestId("review-finding-rule")).toHaveTextContent(
      "R_ENGINE_CROSS",
    );
    expect(screen.getByTestId("review-finding-verdict")).toHaveTextContent(
      "inconsistent",
    );
    expect(window.location.search).toContain(encodeURIComponent(WORK_ID_MANUAL));
    expect(window.location.search).toContain("review=");
  });

  it("protects the Recovery gate error branch: a route failure renders the explicit error state", async () => {
    const RESOLVED_ID = "recovery_work_t01gate1234567890abcd";
    const RESOLVED_APP = "app_t01gate9876543210fedcba";
    fetchRouter({
      "GET /controlled/s01/api/queries/queue": () =>
        new Response(
          JSON.stringify({
            items: [],
            recovery_items: [
              {
                recovery_work_id: RESOLVED_ID,
                application_id: RESOLVED_APP,
                status: "resolved",
                phase: "Unprocessable",
                primary_reason_code: "configuration.checker_unavailable",
                responsible_party: "policy_owner",
                lifecycle_revision: 5,
                projection_watermark: 1,
              },
            ],
            projection_watermark: 1,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      [`GET /controlled/s01/api/queries/recovery-work-items/${RESOLVED_ID}`]:
        () =>
          new Response(
            JSON.stringify({
              ...workPayload({ status: "resolved", can_verify: false }),
              recovery_work_id: RESOLVED_ID,
              application_id: RESOLVED_APP,
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
      [`GET /controlled/s01/api/queries/applications/${RESOLVED_APP}/current-route`]:
        () =>
          new Response(
            JSON.stringify({ detail: { error: "S01_NOT_FOUND" } }),
            { status: 404, headers: { "Content-Type": "application/json" } },
          ),
    });
    renderWithQuery(<App />);
    const link = await screen.findByRole("link", {
      name: new RegExp(RESOLVED_ID),
    });
    await userEvent.click(link);
    // The shared GateSection checks isError before missing data, so an
    // initial 404 renders the explicit error state instead of loading forever.
    await waitFor(() =>
      expect(screen.getByTestId("gate-error")).toHaveTextContent(
        "当前路由未找到或无权访问",
      ),
    );
    expect(screen.queryByTestId("gate-loading")).not.toBeInTheDocument();
  });

  it("mounts the Integrator shell by pathname and never issues the S01 queue read", async () => {
    const router = fetchRouter({
      [`GET /controlled/s02/api/queries/supplement-requests/${encodeURIComponent("supplement_request_t04app00000000000000000000000")}`]:
        () =>
          new Response(
            JSON.stringify({
              schema_version: "supplement-request-integrator/1",
              request_id: "supplement_request_t04app00000000000000000000000",
              status: "open",
              current: true,
              requested_at: 100,
              due_at: 9999999999,
              context_digest: "c".repeat(64),
              upstream_application_ref: "APP-MISS-VINDOC",
              material_requirement: {
                material_requirement_id: "c-demo-financing-lease-vin/1",
                document_role: "financing_lease_contract",
                material_kind: "financing_lease_contract",
                operation: "replacement",
                required_fact_kinds: ["attachment"],
                responsible_party: "application_material_provider",
                allowed_tenant_id: "c-demo",
                allowed_source_system_ids: ["s06-material-source"],
                allowed_workload_identity_ids: ["s06-material-workload"],
                batch_item_count: 2,
                batch_closure_required: true,
                integrity_required: true,
                provenance_required: true,
                evidence_eligibility_required: true,
              },
              expected_predecessor_attachment_id: "attachment_t04v1",
              expected_predecessor_attachment_version: 1,
              next_attachment_version: 2,
              next_request_progress_revision: 1,
              next_source_revision: 1,
              expected_predecessor_revision: null,
              next_batch_item_sequence: 1,
              batch: { batch_id: null, manifest_digest: null, stream_id: null },
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
    });
    window.history.pushState(
      null,
      "",
      `/controlled/s02/react?request=${encodeURIComponent("supplement_request_t04app00000000000000000000000")}`,
    );
    renderWithQuery(<App />);
    await waitFor(() =>
      expect(screen.getByTestId("integrator-panel")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("integrator-boundary-track")).toHaveTextContent(
      "R-OBSERVED",
    );
    expect(screen.getByTestId("integrator-boundary-gate")).toHaveTextContent(
      "S02",
    );
    expect(screen.queryByTestId("queue-panel")).not.toBeInTheDocument();
    expect(
      router.calls.filter((call) => call.url.includes("/controlled/s01/")),
    ).toHaveLength(0);
  });

  it("mounts the Exception Approver shell on /controlled/s05 and never issues S01 reads", async () => {
    const REQUEST_ID = "exception_request_approverapp1";
    const router = fetchRouter({
      [`GET /controlled/s01/api/queries/business-exceptions/${REQUEST_ID}`]: () =>
        new Response(
          JSON.stringify({
            schema_version: "business-exception-approver-view/1",
            request_id: REQUEST_ID,
            work_item_id: "work_exception_approverapp1",
            status: "pending",
            current: true,
            currentness_reason: "CURRENT_FIXED_CONTEXT",
            application_reference: "application:abcd1234ef56",
            finding: {
              finding_id: "finding_approverapp1",
              rule_id: "R_BRAND_CROSS",
              verdict: "inconsistent",
              severity: "critical",
              reason_code: "BRAND_CROSS_INCONSISTENT",
            },
            evidence_references: [],
            requester: {
              subject: "c-demo-test-user",
              role: "reviewer",
              source_id: "c-demo-review-console",
            },
            request_reason: "DOCUMENTED_BRAND_VARIANCE",
            scope: "one_application_cycle_run_finding",
            requested_at: 100,
            expires_at: 9999999999,
            run_id: "run_approverapp1",
            evidence_snapshot_id: "snapshot_approverapp1",
            evidence_snapshot_digest: "b".repeat(64),
            release_id: "auto_lease@1.9.0",
            release_digest: "c".repeat(64),
            checker_build: "s01-target-checker/6",
            waiver_policy_id: "c-demo-brand-exception/1",
            waiver_policy_digest: "d".repeat(64),
            claim_status: "unclaimed",
            claim_subject: null,
            claim_fence: 0,
            claim_expires_at: 0,
            command_context: {
              cycle: 1,
              lifecycle_revision: 7,
              evidence_revision: 1,
              run_id: "run_approverapp1",
              projection_watermark: 2,
              current_context: "a".repeat(64),
            },
            projection_watermark: 2,
            actions: ["claim"],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });
    window.history.pushState(
      null,
      "",
      `/controlled/s05/react?request=${encodeURIComponent(REQUEST_ID)}`,
    );
    renderWithQuery(<App />);
    await waitFor(() =>
      expect(screen.getByTestId("approver-view")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("approver-boundary-gate")).toHaveTextContent(
      "S05",
    );
    expect(screen.queryByTestId("queue-panel")).not.toBeInTheDocument();
    expect(
      router.calls.filter((call) => call.url.includes("/queries/queue")),
    ).toHaveLength(0);
  });

  it("mounts the Reviewer workbench on the canonical /controlled/s01 and its /controlled/s01/react alias", async () => {
    fetchRouter({
      "GET /controlled/s01/api/queries/queue": () =>
        new Response(
          JSON.stringify({
            items: [],
            recovery_items: [],
            projection_watermark: 0,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });
    // Canonical route and its alias both mount the Reviewer workbench; the
    // Integrator shell must never mount on either.
    for (const pathname of ["/controlled/s01", "/controlled/s01/react"]) {
      window.history.pushState(null, "", pathname);
      const view = renderWithQuery(<App />);
      await waitFor(() =>
        expect(screen.getByTestId("queue-panel")).toBeInTheDocument(),
      );
      expect(screen.queryByTestId("integrator-panel")).not.toBeInTheDocument();
      expect(screen.getByTestId("boundary-track")).toHaveTextContent("C-DEMO");
      view.unmount();
    }
  });

  it("mounts the demo shell on the canonical root / and the /demo/react alias and never issues controlled reads", async () => {
    // The canonical root and its alias both mount the same closed synthetic
    // demo shell with the identical boundary contract.
    for (const pathname of ["/", "/demo/react"]) {
      const router = fetchRouter({
        "GET /api/demo/fixtures": () =>
          new Response(
            JSON.stringify({
              fixtures: [
                {
                  fixture_id: "app_demo_step2_ok",
                  title: "赛题样例绑定·字段一致",
                  description: "多单据关键字段对齐",
                  field_source: "synthetic",
                  step2_sample_id: "JFL25P02L080310-01",
                },
              ],
              batch_max_n: 50,
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
      });
      window.history.pushState(null, "", pathname);
      const view = renderWithQuery(<App />);
      await waitFor(() =>
        expect(screen.getByTestId("demo-panel")).toBeInTheDocument(),
      );
      expect(screen.getByTestId("demo-boundary-track")).toHaveTextContent(
        "C-DEMO",
      );
      expect(screen.getByTestId("demo-boundary-scope")).toHaveTextContent(
        "synthetic",
      );
      // T07: the batch + read-only summary panel mounts here too, but neither
      // fires a batch POST nor a summary GET without an explicit action.
      expect(screen.getByTestId("demo-batch-panel")).toBeInTheDocument();
      expect(screen.getByTestId("demo-eval-panel")).toBeInTheDocument();
      expect(screen.getByTestId("demo-eval-status")).toHaveTextContent("未加载");
      expect(screen.queryByTestId("queue-panel")).not.toBeInTheDocument();
      expect(
        router.calls.filter((call) => call.url.includes("/controlled/")),
      ).toHaveLength(0);
      expect(
        router.calls.filter(
          (call) => call.url === "/api/demo/evaluate/summary",
        ),
      ).toHaveLength(0);
      expect(router.calls.filter((call) => call.method === "POST")).toHaveLength(
        0,
      );
      view.unmount();
    }
  });
});

describe("governed policy-release shell (T08)", () => {
  const CANDIDATE = "candidate_t08app000000000000000000000000";

  it("mounts the S08 shell on /controlled/s08/react without a candidate and never issues S01 reads", async () => {
    // The Admin draft workflow fences every command on the server revision,
    // so the one S08 status query is the expected and authorized read of
    // this shell; S01/S02/S05 reads and any POST must never fire.
    const router = fetchRouter({
      "GET /controlled/s08/api/queries/status": () =>
        new Response(
          JSON.stringify({
            track: "C-DEMO",
            capability_gate: "G3",
            bootstrap: true,
            scope: "C-DEMO/demo",
            governance_revision: 3,
            active_generation: 1,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });
    window.history.pushState(null, "", "/controlled/s08/react");
    renderWithQuery(<App />);
    await waitFor(() =>
      expect(screen.getByTestId("t08-draft-workflow")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("s08-boundary-track")).toHaveTextContent(
      "C-DEMO",
    );
    expect(screen.getByTestId("s08-boundary-gate")).toHaveTextContent("S08");
    expect(screen.queryByTestId("queue-panel")).not.toBeInTheDocument();
    expect(
      router.calls.filter((call) => call.url.includes("/controlled/s01/")),
    ).toHaveLength(0);
    expect(
      router.calls.filter((call) => call.url.includes("/controlled/s02/")),
    ).toHaveLength(0);
    expect(
      router.calls.filter((call) => call.url.includes("/controlled/s05/")),
    ).toHaveLength(0);
    expect(router.calls.filter((call) => call.method === "POST")).toHaveLength(
      0,
    );
  });

  it("mounts the S09 governance workspace shell on /controlled/s09/react and never issues S01/S02/S05 reads", async () => {
    const router = fetchRouter({
      "GET /controlled/s09/api/queries/workspace": () =>
        new Response(
          JSON.stringify({
            track: "C-DEMO",
            capability_gate: "G3",
            scope: "C-DEMO/demo",
            governance_revision: 3,
            actor_role: "operator",
            actions: ["impose_hold"],
            active_release: null,
            recovery_anchor: null,
            holds: [],
            events: [],
            audit_events: [],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });
    window.history.pushState(null, "", "/controlled/s09/react");
    renderWithQuery(<App />);
    await waitFor(() =>
      expect(screen.getByTestId("t09-workspace")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("s09-boundary-track")).toHaveTextContent(
      "C-DEMO",
    );
    expect(screen.getByTestId("s09-boundary-gate")).toHaveTextContent("S09");
    expect(screen.queryByTestId("queue-panel")).not.toBeInTheDocument();
    expect(
      router.calls.filter((call) => call.url.includes("/controlled/s01/")),
    ).toHaveLength(0);
    expect(
      router.calls.filter((call) => call.url.includes("/controlled/s02/")),
    ).toHaveLength(0);
    expect(
      router.calls.filter((call) => call.url.includes("/controlled/s05/")),
    ).toHaveLength(0);
    expect(
      router.calls.filter((call) => call.url.includes("/controlled/s08/")),
    ).toHaveLength(0);
    expect(router.calls.filter((call) => call.method === "POST")).toHaveLength(
      0,
    );
  });

  it("mounts the candidate workspace from the non-sensitive URL navigation state and never issues S01 reads", async () => {
    const router = fetchRouter({
      [`GET /controlled/s08/api/queries/candidate/${CANDIDATE}`]: () =>
        new Response(
          JSON.stringify({
            track: "C-DEMO",
            capability_gate: "G3",
            candidate_id: CANDIDATE,
            status: "in_review",
            governance_revision: 3,
            actor_role: "approver",
            actions: ["approve", "reject"],
            events: [],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });
    window.history.pushState(
      null,
      "",
      `/controlled/s08/react?candidate=${encodeURIComponent(CANDIDATE)}`,
    );
    renderWithQuery(<App />);
    await waitFor(() =>
      expect(screen.getByTestId("t08-workspace-status")).toHaveTextContent(
        "in_review",
      ),
    );
    expect(
      router.calls.filter((call) => call.url.includes("/controlled/s01/")),
    ).toHaveLength(0);
    expect(
      router.calls.filter((call) => call.url.includes("/controlled/s02/")),
    ).toHaveLength(0);
  });
});

describe("evaluation operator shell (T14)", () => {
  it("mounts the S12 shell on /controlled/s12 and reads only the frozen-plan catalog", async () => {
    const router = fetchRouter({
      "GET /controlled/s12/plans": () =>
        new Response(
          JSON.stringify({
            schema_version: "s12-plan-catalog/1",
            plans: [
              {
                plan_id: "plan-c-1",
                plan_digest: "b".repeat(64),
                scope: "C",
                frozen_at: 1700000000,
                budget: { max_opportunities: 10, max_runtime_ms: 5000 },
                stop_rule: "plan-exhausted",
                opportunity_count: 4,
              },
            ],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });
    window.history.replaceState(null, "", "/controlled/s12");
    renderWithQuery(<App />);
    await waitFor(() =>
      expect(screen.getByTestId("s12-plan-select")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("s12-boundary-gate")).toHaveTextContent("S12");
    expect(screen.queryByTestId("queue-panel")).not.toBeInTheDocument();
    expect(
      router.calls.filter((call) => call.url.includes("/controlled/s01/")),
    ).toHaveLength(0);
    expect(
      router.calls.filter((call) => call.url.includes("/controlled/s02/")),
    ).toHaveLength(0);
    expect(
      router.calls.filter((call) => call.url.includes("/controlled/s05/")),
    ).toHaveLength(0);
    expect(
      router.calls.filter((call) => call.url.includes("/controlled/s08/")),
    ).toHaveLength(0);
    expect(
      router.calls.filter((call) => call.url.includes("/controlled/s09/")),
    ).toHaveLength(0);
    expect(router.calls.filter((call) => call.method === "POST")).toHaveLength(
      0,
    );
    expect(router.calls.map((call) => call.url)).toEqual([
      "/controlled/s12/plans",
    ]);
  });

  it("mounts the same operator shell on the /controlled/s12/react alias", async () => {
    const router = fetchRouter({
      "GET /controlled/s12/plans": () =>
        new Response(
          JSON.stringify({
            schema_version: "s12-plan-catalog/1",
            plans: [],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });
    window.history.pushState(null, "", "/controlled/s12/react");
    renderWithQuery(<App />);
    await waitFor(() =>
      expect(screen.getByTestId("s12-catalog-empty")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("s12-boundary-gate")).toHaveTextContent("S12");
    expect(router.calls.map((call) => call.url)).toEqual([
      "/controlled/s12/plans",
    ]);
  });
});

describe("delivery console shell (T15)", () => {
  const APP_ID = S13_APPLICATION_ID;
  const deliveryPayload = (
    overrides: Parameters<typeof s13QueryPayload>[0] = {},
  ) =>
    s13QueryPayload(overrides);

  it("mounts the S13 shell on /controlled/s13 and reads only the delivery view", async () => {
    const router = fetchRouter({
      [`GET /controlled/s13/delivery/${APP_ID}`]: () =>
        new Response(JSON.stringify(deliveryPayload()), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    });
    window.history.pushState(null, "", `/controlled/s13?application=${encodeURIComponent(APP_ID)}`);
    renderWithQuery(<App />);
    await waitFor(() =>
      expect(screen.getByTestId("s13-verification-completed")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("s13-boundary-gate")).toHaveTextContent("S13");
    expect(screen.getByTestId("s13-verification-completed")).toHaveTextContent("completed");
    expect(screen.getByTestId("s13-delivery-status")).toHaveTextContent("pending");
    // No S01/S02/S05/S08/S09/S12 reads.
    expect(
      router.calls.filter((call) => call.url.includes("/controlled/s01/")),
    ).toHaveLength(0);
    expect(
      router.calls.filter((call) => call.url.includes("/controlled/s02/")),
    ).toHaveLength(0);
    expect(
      router.calls.filter((call) => call.url.includes("/controlled/s05/")),
    ).toHaveLength(0);
    expect(
      router.calls.filter((call) => call.url.includes("/controlled/s08/")),
    ).toHaveLength(0);
    expect(
      router.calls.filter((call) => call.url.includes("/controlled/s09/")),
    ).toHaveLength(0);
    expect(
      router.calls.filter((call) => call.url.includes("/controlled/s12/")),
    ).toHaveLength(0);
    expect(router.calls.filter((call) => call.method === "POST")).toHaveLength(0);
    expect(router.calls.map((call) => call.url)).toEqual([
      `/controlled/s13/delivery/${APP_ID}`,
    ]);
  });

  it("mounts the same console shell on the /controlled/s13/react alias with query navigation", async () => {
    const router = fetchRouter({
      [`GET /controlled/s13/delivery/${APP_ID}`]: () =>
        new Response(JSON.stringify(deliveryPayload({ delivery_status: "received" })), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    });
    window.history.pushState(
      null,
      "",
      `/controlled/s13/react?application=${encodeURIComponent(APP_ID)}`,
    );
    renderWithQuery(<App />);
    await waitFor(() =>
      expect(screen.getByTestId("s13-delivery-status")).toHaveTextContent("received"),
    );
    expect(screen.getByTestId("s13-boundary-gate")).toHaveTextContent("S13");
    expect(router.calls.map((call) => call.url)).toEqual([
      `/controlled/s13/delivery/${APP_ID}`,
    ]);
  });

  it("clears delivery facts on S13 403 without leaking obligation identifiers", async () => {
    const router = fetchRouter({
      [`GET /controlled/s13/delivery/${APP_ID}`]: () =>
        new Response(
          JSON.stringify({ detail: { error: "S13_FORBIDDEN", message: "forbidden" } }),
          { status: 403, headers: { "Content-Type": "application/json" } },
        ),
    });
    window.history.pushState(null, "", `/controlled/s13?application=${encodeURIComponent(APP_ID)}`);
    renderWithQuery(<App />);
    await waitFor(() =>
      expect(screen.getByTestId("s13-error-forbidden")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("s13-error-code")).toHaveTextContent("S13_FORBIDDEN");
    expect(screen.queryByTestId("s13-obligation-id")).not.toBeInTheDocument();
    expect(router.calls.filter((call) => call.method === "POST")).toHaveLength(0);
  });

  it("treats an empty application query as unselected", () => {
    const router = fetchRouter({});
    window.history.pushState(null, "", "/controlled/s13?application=");
    renderWithQuery(<App />);

    expect(screen.getByTestId("s13-no-application")).toBeInTheDocument();
    expect(router.calls).toHaveLength(0);
  });
});
