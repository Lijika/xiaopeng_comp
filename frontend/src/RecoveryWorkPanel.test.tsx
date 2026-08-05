import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import RecoveryWorkPanel from "./components/RecoveryWorkPanel";
import { fetchRouter, renderWithQuery, type RouteHandler } from "./test-utils";

const WORK_ID = "recovery_work_t01panel1234567890abcdef";
const APP_ID = "app_t01panel9876543210fedcba";
const WORK_PATH =
  "/controlled/s01/api/queries/recovery-work-items/recovery_work_t01panel1234567890abcdef";
const VERIFY_PATH =
  "/controlled/s01/api/commands/recovery-work-items/recovery_work_t01panel1234567890abcdef/verify";
const ROUTE_PATH =
  "/controlled/s01/api/queries/applications/app_t01panel9876543210fedcba/current-route";

function workPayload(overrides: Record<string, unknown> = {}) {
  return {
    schema_version: "recovery-work-view/1",
    recovery_work_id: WORK_ID,
    status: "open",
    application_id: APP_ID,
    cycle: 1,
    phase: "Unprocessable",
    route: "unprocessable",
    lifecycle_revision: 5,
    evidence_revision: 1,
    primary_reason_code: "configuration.checker_unavailable",
    related_reason_codes: [],
    operation: "execute_check_run",
    dependency: "c-demo-target-checker",
    logical_operation_id: "job_t01panel000000000000000000",
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
        {
          condition_id: "s07-checker-compatibility/1",
          reason_code: "configuration.checker_unavailable",
        },
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

const RESOLVED = workPayload({
  status: "resolved",
  phase: "Evidence Ready",
  route: "pending_check",
  lifecycle_revision: 6,
  recovery_fact_count: 1,
  resolution_count: 1,
  current_run_id: null,
});

function workRoute(overrides: Record<string, unknown> = {}): RouteHandler {
  return () =>
    new Response(JSON.stringify(workPayload(overrides)), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
}

describe("RecoveryWorkPanel", () => {
  it("renders the minimized work without restricted values and keeps the Reviewer command disabled", async () => {
    fetchRouter({
      [`GET ${WORK_PATH}`]: workRoute(),
    });
    renderWithQuery(<RecoveryWorkPanel workId={WORK_ID} />);
    await waitFor(() =>
      expect(screen.getByTestId("recovery-status")).toHaveTextContent("open"),
    );
    expect(screen.getByTestId("recovery-status")).toHaveTextContent("open");
    expect(screen.getByTestId("recovery-phase")).toHaveTextContent(
      "Unprocessable",
    );
    expect(screen.getByTestId("recovery-route")).toHaveTextContent(
      "unprocessable",
    );
    expect(screen.getByTestId("recovery-lifecycle-revision")).toHaveTextContent(
      "5",
    );
    expect(screen.getByTestId("recovery-watermark")).toHaveTextContent("1");
    expect(screen.getByTestId("recovery-dependency")).toHaveTextContent(
      "c-demo-target-checker",
    );
    expect(
      screen.getByRole("button", { name: "验证恢复" }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "重新加载" }),
    ).toBeInTheDocument();
    const panelText = screen.getByTestId("recovery-panel").textContent ?? "";
    expect(panelText).not.toContain("recovered");
    expect(panelText).not.toContain("raw");
    expect(panelText).not.toContain("verifier");
  });

  it("shows an existence-hiding error state for missing work", async () => {
    fetchRouter({
      [`GET ${WORK_PATH}`]: () =>
        new Response(JSON.stringify({ detail: { error: "S07_NOT_FOUND" } }), {
          status: 404,
          headers: { "Content-Type": "application/json" },
        }),
    });
    renderWithQuery(<RecoveryWorkPanel workId={WORK_ID} />);
    await waitFor(() =>
      expect(screen.getByTestId("recovery-error")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("recovery-error")).toHaveTextContent(
      "未找到或无权访问",
    );
    expect(screen.getByTestId("recovery-error").textContent).not.toContain(
      WORK_ID,
    );
    expect(
      screen.queryByRole("button", { name: "验证恢复" }),
    ).not.toBeInTheDocument();
  });

  it("lets the Operator submit exactly the three command fields and refetches the server-owned gate", async () => {
    let postCount = 0;
    let getCount = 0;
    const router = fetchRouter({
      [`GET ${WORK_PATH}`]: () => {
        getCount += 1;
        return new Response(JSON.stringify(workPayload({ can_verify: true })), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      },
      [`POST ${VERIFY_PATH}`]: () => {
        postCount += 1;
        return new Response(
          JSON.stringify({
            status: "accepted",
            replayed: false,
            recovery_work_id: WORK_ID,
            recovery_fact_id: "fact-t01-1",
            application_id: APP_ID,
            phase: "Evidence Ready",
            lifecycle_revision: 6,
            evidence_revision: 1,
            successor_job_id: "job_t01successor00000000000000",
            successor_fence: 2,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
    });
    renderWithQuery(<RecoveryWorkPanel workId={WORK_ID} />);
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "验证恢复" }),
      ).toBeEnabled(),
    );
    await userEvent.click(screen.getByRole("button", { name: "验证恢复" }));
    await waitFor(() =>
      expect(screen.getByTestId("recovery-command-status")).toHaveTextContent(
        "恢复事实已接受",
      ),
    );
    expect(postCount).toBe(1);
    expect(getCount).toBeGreaterThanOrEqual(2);
    const postCalls = router.calls.filter((call) => call.method === "POST");
    expect(postCalls).toHaveLength(1);
    const body = postCalls[0].body as Record<string, unknown>;
    expect(Object.keys(body).sort()).toEqual([
      "expected_criterion_digest",
      "expected_lifecycle_revision",
      "idempotency_key",
    ]);
    expect(body.expected_lifecycle_revision).toBe(5);
    expect(body.expected_criterion_digest).toBe("a".repeat(64));
    expect(JSON.stringify(body)).not.toContain("target");
    expect(JSON.stringify(body)).not.toContain("recovered");
  });

  it("surfaces a real stale conflict, requires an authoritative reload, and issues a new semantic key", async () => {
    let stale = true;
    let getCount = 0;
    const router = fetchRouter({
      [`GET ${WORK_PATH}`]: () => {
        getCount += 1;
        return new Response(JSON.stringify(workPayload({ can_verify: true })), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      },
      [`POST ${VERIFY_PATH}`]: () => {
        if (stale) {
          stale = false;
          return new Response(
            JSON.stringify({
              detail: {
                error: "S07_STALE",
                reason_code: "recovery.context_changed",
              },
            }),
            { status: 409, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(
          JSON.stringify({
            status: "accepted",
            replayed: false,
            recovery_work_id: WORK_ID,
            recovery_fact_id: "fact-t01-stale",
            application_id: APP_ID,
            phase: "Evidence Ready",
            lifecycle_revision: 6,
            evidence_revision: 1,
            successor_job_id: "job_t01successor00000000000000",
            successor_fence: 2,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
    });
    renderWithQuery(<RecoveryWorkPanel workId={WORK_ID} />);
    const verifyButton = () =>
      screen.getByRole("button", { name: "验证恢复" });
    await waitFor(() => expect(verifyButton()).toBeEnabled());

    await userEvent.click(verifyButton());
    await waitFor(() =>
      expect(screen.getByTestId("recovery-command-status")).toHaveTextContent(
        "recovery.context_changed",
      ),
    );
    expect(screen.getByTestId("recovery-status")).toHaveTextContent("open");
    expect(screen.getByTestId("recovery-phase")).toHaveTextContent(
      "Unprocessable",
    );
    expect(verifyButton()).toBeDisabled();

    const beforeReload = router.calls.filter((call) => call.method === "POST");
    const staleKey = (beforeReload[0].body as Record<string, unknown>)
      .idempotency_key;
    expect(typeof staleKey).toBe("string");

    await userEvent.click(screen.getByRole("button", { name: "重新加载" }));
    await waitFor(() => expect(verifyButton()).toBeEnabled());
    expect(getCount).toBeGreaterThanOrEqual(2);

    await userEvent.click(verifyButton());
    await waitFor(() =>
      expect(screen.getByTestId("recovery-command-status")).toHaveTextContent(
        "恢复事实已接受",
      ),
    );
    const afterReload = router.calls
      .filter((call) => call.method === "POST")
      .map((call) => (call.body as Record<string, unknown>).idempotency_key);
    expect(afterReload).toHaveLength(2);
    expect(afterReload[1]).not.toBe(staleKey);
  });

  it("keeps the same idempotency key when the transport outcome is unknown", async () => {
    let networkFailure = true;
    const router = fetchRouter({
      [`GET ${WORK_PATH}`]: () =>
        new Response(JSON.stringify(workPayload({ can_verify: true })), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      [`POST ${VERIFY_PATH}`]: () => {
        if (networkFailure) {
          networkFailure = false;
          return Promise.reject(new TypeError("fetch failed: connection reset"));
        }
        return new Response(
          JSON.stringify({
            status: "accepted",
            replayed: false,
            recovery_work_id: WORK_ID,
            recovery_fact_id: "fact-t01-unknown",
            application_id: APP_ID,
            phase: "Evidence Ready",
            lifecycle_revision: 6,
            evidence_revision: 1,
            successor_job_id: "job_t01successor00000000000000",
            successor_fence: 2,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
    });
    renderWithQuery(<RecoveryWorkPanel workId={WORK_ID} />);
    const verifyButton = () =>
      screen.getByRole("button", { name: "验证恢复" });
    await waitFor(() => expect(verifyButton()).toBeEnabled());

    await userEvent.click(verifyButton());
    await waitFor(() =>
      expect(screen.getByTestId("recovery-command-status")).toHaveTextContent(
        "结果未知",
      ),
    );
    expect(screen.getByTestId("recovery-status")).toHaveTextContent("open");
    await userEvent.click(screen.getByRole("button", { name: "重试" }));

    await waitFor(() =>
      expect(screen.getByTestId("recovery-command-status")).toHaveTextContent(
        "恢复事实已接受",
      ),
    );
    const keys = router.calls
      .filter((call) => call.method === "POST")
      .map((call) => (call.body as Record<string, unknown>).idempotency_key);
    expect(keys).toHaveLength(2);
    expect(keys[1]).toBe(keys[0]);
  });

  it("shows the authoritative current-route gate to the Reviewer and never calls it for the Operator", async () => {
    const router = fetchRouter({
      [`GET ${WORK_PATH}`]: () =>
        new Response(JSON.stringify(RESOLVED), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      [`GET ${ROUTE_PATH}`]: () =>
        new Response(
          JSON.stringify({
            schema_version: "s04-current-route/1",
            application_id: APP_ID,
            phase: "Evidence Ready",
            route: "pending_check",
            current_run_id: null,
            cycle: 1,
            lifecycle_revision: 6,
            evidence_revision: 1,
            evidence_snapshot_id: null,
            evidence_snapshot_digest: null,
            release_id: "auto_lease@1.9.0",
            release_digest: "b".repeat(64),
            checker_build: "s01-target-checker/6",
            currentness_reason: "NO_CURRENT_RUN",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });
    const firstRender = renderWithQuery(<RecoveryWorkPanel workId={WORK_ID} />);
    await waitFor(() =>
      expect(screen.getByTestId("recovery-status")).toHaveTextContent(
        "resolved",
      ),
    );
    expect(screen.getByTestId("recovery-phase")).toHaveTextContent(
      "Evidence Ready",
    );
    expect(screen.getByTestId("recovery-route")).toHaveTextContent(
      "pending_check",
    );
    await waitFor(() =>
      expect(screen.getByTestId("gate-phase")).toHaveTextContent(
        "Evidence Ready",
      ),
    );
    expect(screen.getByTestId("gate-route")).toHaveTextContent("pending_check");
    expect(screen.getByTestId("gate-currentness")).toHaveTextContent(
      "NO_CURRENT_RUN",
    );
    expect(
      router.calls.some((call) => call.url.includes("current-route")),
    ).toBe(true);
    expect(
      screen.getByRole("button", { name: "验证恢复" }),
    ).toBeDisabled();
    firstRender.unmount();

    const operatorRouter = fetchRouter({
      [`GET ${WORK_PATH}`]: () =>
        new Response(JSON.stringify({ ...RESOLVED, can_verify: true }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    });
    renderWithQuery(<RecoveryWorkPanel workId={WORK_ID} />);
    await waitFor(() =>
      expect(screen.getByTestId("recovery-status")).toHaveTextContent(
        "resolved",
      ),
    );
    expect(
      operatorRouter.calls.some((call) => call.url.includes("current-route")),
    ).toBe(false);
    expect(screen.queryByTestId("gate-panel")).not.toBeInTheDocument();
  });

  it("keeps the conflict fence and the semantic key when the authoritative reload fails", async () => {
    let stale = true;
    let failReload = false;
    const router = fetchRouter({
      [`GET ${WORK_PATH}`]: () => {
        if (failReload) {
          return Promise.reject(new TypeError("fetch failed: reload refused"));
        }
        return new Response(JSON.stringify(workPayload({ can_verify: true })), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      },
      [`POST ${VERIFY_PATH}`]: () => {
        if (stale) {
          stale = false;
          return new Response(
            JSON.stringify({
              detail: {
                error: "S07_STALE",
                reason_code: "recovery.context_changed",
              },
            }),
            { status: 409, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(
          JSON.stringify({
            status: "accepted",
            replayed: false,
            recovery_work_id: WORK_ID,
            recovery_fact_id: "fact-t01-failed-reload",
            application_id: APP_ID,
            phase: "Evidence Ready",
            lifecycle_revision: 6,
            evidence_revision: 1,
            successor_job_id: "job_t01successor00000000000000",
            successor_fence: 2,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
    });
    renderWithQuery(<RecoveryWorkPanel workId={WORK_ID} />);
    const verifyButton = () =>
      screen.getByRole("button", { name: "验证恢复" });
    await waitFor(() => expect(verifyButton()).toBeEnabled());

    await userEvent.click(verifyButton());
    await waitFor(() =>
      expect(screen.getByTestId("recovery-command-status")).toHaveTextContent(
        "recovery.context_changed",
      ),
    );
    const staleKey = (
      router.calls.find((call) => call.method === "POST")?.body as
        | Record<string, unknown>
        | undefined
    )?.idempotency_key as string;

    failReload = true;
    await userEvent.click(screen.getByRole("button", { name: "重新加载" }));
    await waitFor(() =>
      expect(screen.getByTestId("recovery-command-status")).toHaveTextContent(
        "recovery.context_changed",
      ),
    );
    expect(verifyButton()).toBeDisabled();
    expect(
      router.calls.filter((call) => call.method === "POST"),
    ).toHaveLength(1);

    failReload = false;
    await userEvent.click(screen.getByRole("button", { name: "重新加载" }));
    await waitFor(() => expect(verifyButton()).toBeEnabled());
    expect(screen.getByTestId("recovery-command-status")).toHaveTextContent(
      "等待操作",
    );

    await userEvent.click(verifyButton());
    await waitFor(() =>
      expect(screen.getByTestId("recovery-command-status")).toHaveTextContent(
        "恢复事实已接受",
      ),
    );
    const keys = router.calls
      .filter((call) => call.method === "POST")
      .map((call) => (call.body as Record<string, unknown>).idempotency_key);
    expect(keys).toHaveLength(2);
    expect(keys[1]).not.toBe(staleKey);
  });

  it("preserves the original idempotency key through unknown -> Reload -> Retry", async () => {
    let networkFailure = true;
    const router = fetchRouter({
      [`GET ${WORK_PATH}`]: () =>
        new Response(JSON.stringify(workPayload({ can_verify: true })), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      [`POST ${VERIFY_PATH}`]: () => {
        if (networkFailure) {
          networkFailure = false;
          return Promise.reject(new TypeError("fetch failed: connection reset"));
        }
        return new Response(
          JSON.stringify({
            status: "accepted",
            replayed: false,
            recovery_work_id: WORK_ID,
            recovery_fact_id: "fact-t01-reload-retry",
            application_id: APP_ID,
            phase: "Evidence Ready",
            lifecycle_revision: 6,
            evidence_revision: 1,
            successor_job_id: "job_t01successor00000000000000",
            successor_fence: 2,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
    });
    renderWithQuery(<RecoveryWorkPanel workId={WORK_ID} />);
    const verifyButton = () =>
      screen.getByRole("button", { name: "验证恢复" });
    await waitFor(() => expect(verifyButton()).toBeEnabled());

    await userEvent.click(verifyButton());
    await waitFor(() =>
      expect(screen.getByTestId("recovery-command-status")).toHaveTextContent(
        "结果未知",
      ),
    );
    const originalKey = (
      router.calls.find((call) => call.method === "POST")?.body as
        | Record<string, unknown>
        | undefined
    )?.idempotency_key as string;

    await userEvent.click(screen.getByRole("button", { name: "重新加载" }));
    await waitFor(() =>
      expect(screen.getByTestId("recovery-command-status")).toHaveTextContent(
        "结果未知",
      ),
    );
    expect(
      screen.getByRole("button", { name: "重试" }),
    ).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() =>
      expect(screen.getByTestId("recovery-command-status")).toHaveTextContent(
        "恢复事实已接受",
      ),
    );
    const keys = router.calls
      .filter((call) => call.method === "POST")
      .map((call) => (call.body as Record<string, unknown>).idempotency_key);
    expect(keys).toHaveLength(2);
    expect(keys[1]).toBe(originalKey);
  });

  it("latches acceptance locally so a delayed server refetch allows zero extra POSTs", async () => {
    let getCount = 0;
    let postCount = 0;
    let resolveRefetch: ((value: Response) => void) | undefined;
    const refetchGate = new Promise<Response>((resolve) => {
      resolveRefetch = resolve;
    });
    const router = fetchRouter({
      [`GET ${WORK_PATH}`]: () => {
        getCount += 1;
        if (getCount === 1) {
          return new Response(
            JSON.stringify(workPayload({ can_verify: true })),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        return refetchGate;
      },
      [`POST ${VERIFY_PATH}`]: () => {
        postCount += 1;
        return new Response(
          JSON.stringify({
            status: "accepted",
            replayed: false,
            recovery_work_id: WORK_ID,
            recovery_fact_id: "fact-t01-latched",
            application_id: APP_ID,
            phase: "Evidence Ready",
            lifecycle_revision: 6,
            evidence_revision: 1,
            successor_job_id: "job_t01successor00000000000000",
            successor_fence: 2,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
    });
    renderWithQuery(<RecoveryWorkPanel workId={WORK_ID} />);
    const verifyButton = () =>
      screen.getByRole("button", { name: "验证恢复" });
    await waitFor(() => expect(verifyButton()).toBeEnabled());

    await userEvent.click(verifyButton());
    await waitFor(() => expect(postCount).toBe(1));
    expect(getCount).toBeGreaterThanOrEqual(2);

    // While the server-owned refetch is still in flight the command stays
    // pending and latched: no re-enabled button, zero extra POSTs.
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(postCount).toBe(1);
    expect(verifyButton()).toBeDisabled();
    expect(
      screen.getByTestId("recovery-command-status"),
    ).toHaveTextContent("恢复验证提交中");

    resolveRefetch?.(
      new Response(JSON.stringify(RESOLVED), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    await waitFor(() =>
      expect(screen.getByTestId("recovery-status")).toHaveTextContent(
        "resolved",
      ),
    );
    expect(
      screen.getByTestId("recovery-command-status"),
    ).toHaveTextContent("恢复事实已接受");
    expect(postCount).toBe(1);
    expect(verifyButton()).toBeDisabled();
    expect(router.calls.filter((call) => call.method === "POST")).toHaveLength(
      1,
    );
  });
});
