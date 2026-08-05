import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import App from "./App";
import { fetchRouter, renderWithQuery } from "./test-utils";

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
});
