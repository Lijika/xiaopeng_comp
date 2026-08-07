import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import BusinessExceptionApproverPanel from "./BusinessExceptionApproverPanel";
import { fetchRouter, renderWithQuery } from "../test-utils";

const REQUEST_ID = "exception_request_approver0001";
const WORK_ITEM_ID = "work_exception_approver0001";
const VIEW_PATH = `/controlled/s01/api/queries/business-exceptions/${REQUEST_ID}`;
const CLAIM_PATH = `/controlled/s01/api/commands/exception-work-items/${WORK_ITEM_ID}/claim`;
const DECIDE_PATH = `/controlled/s01/api/commands/business-exceptions/${REQUEST_ID}/decide`;

const CONTEXT = {
  cycle: 1,
  lifecycle_revision: 7,
  evidence_revision: 1,
  run_id: "run_approver",
  projection_watermark: 2,
  current_context: "a".repeat(64),
};

function viewPayload(overrides: Record<string, unknown> = {}) {
  return {
    schema_version: "business-exception-approver-view/1",
    request_id: REQUEST_ID,
    work_item_id: WORK_ITEM_ID,
    status: "pending",
    current: true,
    currentness_reason: "CURRENT_FIXED_CONTEXT",
    application_reference: "application:abcd1234ef56",
    finding: {
      finding_id: "finding_approver0001",
      rule_id: "R_BRAND_CROSS",
      verdict: "inconsistent",
      severity: "critical",
      reason_code: "BRAND_CROSS_INCONSISTENT",
    },
    evidence_references: [
      {
        observation_id: "observation_approver",
        document_role: "pol",
        field: "brand",
        source_page: 1,
        source_region: "region:1",
      },
    ],
    requester: {
      subject: "c-demo-test-user",
      role: "reviewer",
      source_id: "c-demo-review-console",
    },
    request_reason: "DOCUMENTED_BRAND_VARIANCE",
    scope: "one_application_cycle_run_finding",
    requested_at: 100,
    expires_at: 9999999999,
    run_id: "run_approver",
    evidence_snapshot_id: "snapshot_approver",
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
    command_context: CONTEXT,
    projection_watermark: 2,
    actions: ["claim"],
    ...overrides,
  };
}

describe("BusinessExceptionApproverPanel (T05)", () => {
  it("shows an explicit empty state without a request id", () => {
    fetchRouter({});
    renderWithQuery(<BusinessExceptionApproverPanel requestId={null} />);
    expect(screen.getByTestId("approver-empty")).toBeInTheDocument();
  });

  it("shows the minimized server view and never exposes raw values", async () => {
    const router = fetchRouter({
      [`GET ${VIEW_PATH}`]: () => router.jsonResponse(viewPayload()),
    });
    renderWithQuery(
      <BusinessExceptionApproverPanel requestId={REQUEST_ID} />,
    );
    await waitFor(() =>
      expect(screen.getByTestId("approver-view")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("approver-finding-rule")).toHaveTextContent(
      "R_BRAND_CROSS",
    );
    expect(screen.getByTestId("approver-verdict")).toHaveTextContent(
      "inconsistent",
    );
    expect(screen.getByTestId("approver-requester")).toHaveTextContent(
      "c-demo-test-user",
    );
    expect(screen.getByTestId("approver-actions")).toHaveTextContent("认领");
    const body = document.body.textContent ?? "";
    expect(body).not.toContain("LSVAA4182N3000004");
    expect(body).not.toContain("330106199203034560");
  });

  it("shows the existence-hiding not-found and unavailable states", async () => {
    const router = fetchRouter({
      [`GET ${VIEW_PATH}`]: () => router.errorResponse(404, "S05_NOT_FOUND"),
    });
    renderWithQuery(
      <BusinessExceptionApproverPanel requestId={REQUEST_ID} />,
    );
    await waitFor(() =>
      expect(screen.getByTestId("approver-not-found")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("approver-not-found")).toHaveTextContent(
      "未找到或无权访问",
    );
  });

  it("claims with the exact server context and then offers approve/reject", async () => {
    let actions: string[] = ["claim"];
    const router = fetchRouter({
      [`GET ${VIEW_PATH}`]: () =>
        router.jsonResponse(
          viewPayload({
            actions,
            claim_status: actions.includes("decide") ? "claimed" : "unclaimed",
            claim_subject: actions.includes("decide")
              ? "s05-approver"
              : null,
            claim_fence: actions.includes("decide") ? 1 : 0,
            claim_expires_at: actions.includes("decide")
              ? 9999999999
              : 0,
          }),
        ),
      [`POST ${CLAIM_PATH}`]: () => {
        actions = ["decide"];
        return router.jsonResponse({
          status: "claimed",
          request_id: REQUEST_ID,
          work_item_id: WORK_ITEM_ID,
          claim_subject: "s05-approver",
          claim_fence: 1,
          claim_expires_at: 9999999999,
        });
      },
    });
    const user = userEvent.setup();
    renderWithQuery(
      <BusinessExceptionApproverPanel requestId={REQUEST_ID} />,
    );
    await waitFor(() =>
      expect(screen.getByTestId("approver-claim-button")).toBeEnabled(),
    );
    await user.click(screen.getByTestId("approver-claim-button"));
    await waitFor(() =>
      expect(screen.getByTestId("approver-approve-button")).toBeEnabled(),
    );
    const claimPosts = router.calls.filter(
      (call) => call.method === "POST" && call.url.endsWith("/claim"),
    );
    expect(claimPosts).toHaveLength(1);
    expect(claimPosts[0].body).toEqual({ expected_context: CONTEXT });
    expect(
      router.calls.filter((call) => call.method === "POST").length,
    ).toBe(1);
  });

  it("approves with the exact server-bound decision command and never routes", async () => {
    let state: "pending" | "claimed" | "approved" = "pending";
    const router = fetchRouter({
      [`GET ${VIEW_PATH}`]: () =>
        router.jsonResponse(
          state === "pending"
            ? viewPayload()
            : state === "claimed"
              ? viewPayload({
                  actions: ["decide"],
                  claim_status: "claimed",
                  claim_subject: "s05-approver",
                  claim_fence: 1,
                  claim_expires_at: 9999999999,
                })
              : viewPayload({
                  status: "approved",
                  actions: [],
                  claim_status: "completed",
                  claim_subject: "s05-approver",
                  claim_fence: 1,
                  claim_expires_at: 9999999999,
                }),
        ),
      [`POST ${CLAIM_PATH}`]: () => {
        state = "claimed";
        return router.jsonResponse({
          status: "claimed",
          request_id: REQUEST_ID,
          work_item_id: WORK_ITEM_ID,
          claim_subject: "s05-approver",
          claim_fence: 1,
          claim_expires_at: 9999999999,
        });
      },
      [`POST ${DECIDE_PATH}`]: () => {
        state = "approved";
        return router.jsonResponse({
          status: "accepted",
          replayed: false,
          request_id: REQUEST_ID,
          work_item_id: WORK_ITEM_ID,
          decision_id: "exception_decision_approver",
          decision: "approved",
          phase: "Routing Determination",
          route: "routing_determination",
          successor_work_item_id: null,
          lifecycle_revision: 8,
          evidence_revision: 1,
          routing_context: {
            cycle: 1,
            lifecycle_revision: 8,
            evidence_revision: 1,
            run_id: "run_approver",
            request_id: REQUEST_ID,
            decision_id: "exception_decision_approver",
            current_context: "e".repeat(64),
          },
        });
      },
    });
    const user = userEvent.setup();
    renderWithQuery(
      <BusinessExceptionApproverPanel requestId={REQUEST_ID} />,
    );
    await waitFor(() =>
      expect(screen.getByTestId("approver-claim-button")).toBeEnabled(),
    );
    await user.click(screen.getByTestId("approver-claim-button"));
    await waitFor(() =>
      expect(screen.getByTestId("approver-approve-button")).toBeEnabled(),
    );
    await user.click(screen.getByTestId("approver-approve-button"));
    await waitFor(() =>
      expect(screen.getByTestId("approver-status")).toHaveTextContent(
        "approved",
      ),
    );
    const decidePosts = router.calls.filter(
      (call) => call.method === "POST" && call.url.endsWith("/decide"),
    );
    expect(decidePosts).toHaveLength(1);
    const body = decidePosts[0].body as Record<string, unknown>;
    expect(body).toMatchObject({
      work_item_id: WORK_ITEM_ID,
      decision: "approved",
      reason_code: "DOCUMENTED_VARIANCE_ACCEPTED",
      expected_fence: 1,
      expected_context: CONTEXT,
    });
    expect(typeof body.idempotency_key).toBe("string");
    const operatorCommands = router.calls.filter((call) =>
      /\/route|\/expire|\/invalidate|\/close|\/resume/.test(call.url),
    );
    expect(operatorCommands).toHaveLength(0);
  });

  it("rejects with the exact server-bound decision command", async () => {
    let state: "pending" | "claimed" | "rejected" = "pending";
    const router = fetchRouter({
      [`GET ${VIEW_PATH}`]: () =>
        router.jsonResponse(
          state === "pending"
            ? viewPayload()
            : state === "claimed"
              ? viewPayload({
                  actions: ["decide"],
                  claim_status: "claimed",
                  claim_subject: "s05-approver",
                  claim_fence: 1,
                  claim_expires_at: 9999999999,
                })
              : viewPayload({
                  status: "rejected",
                  actions: [],
                  claim_status: "completed",
                  claim_subject: "s05-approver",
                  claim_fence: 1,
                  claim_expires_at: 9999999999,
                }),
        ),
      [`POST ${CLAIM_PATH}`]: () => {
        state = "claimed";
        return router.jsonResponse({
          status: "claimed",
          request_id: REQUEST_ID,
          work_item_id: WORK_ITEM_ID,
          claim_subject: "s05-approver",
          claim_fence: 1,
          claim_expires_at: 9999999999,
        });
      },
      [`POST ${DECIDE_PATH}`]: () => {
        state = "rejected";
        return router.jsonResponse({
          status: "accepted",
          replayed: false,
          request_id: REQUEST_ID,
          work_item_id: WORK_ITEM_ID,
          decision_id: "exception_decision_approver_reject",
          decision: "rejected",
          phase: "Manual Review",
          route: "manual_review",
          successor_work_item_id: "work_manual_successor",
          lifecycle_revision: 8,
          evidence_revision: 1,
        });
      },
    });
    const user = userEvent.setup();
    renderWithQuery(
      <BusinessExceptionApproverPanel requestId={REQUEST_ID} />,
    );
    await waitFor(() =>
      expect(screen.getByTestId("approver-claim-button")).toBeEnabled(),
    );
    await user.click(screen.getByTestId("approver-claim-button"));
    await waitFor(() =>
      expect(screen.getByTestId("approver-reject-button")).toBeEnabled(),
    );
    await user.click(screen.getByTestId("approver-reject-button"));
    await waitFor(() =>
      expect(screen.getByTestId("approver-status")).toHaveTextContent(
        "rejected",
      ),
    );
    const decidePosts = router.calls.filter(
      (call) => call.method === "POST" && call.url.endsWith("/decide"),
    );
    expect(decidePosts).toHaveLength(1);
    expect(decidePosts[0].body).toMatchObject({
      decision: "rejected",
      reason_code: "DOCUMENTED_VARIANCE_REJECTED",
    });
  });

  it("shows definitive claim conflicts and unavailable outcomes", async () => {
    let mode: "conflict" | "stopped" = "conflict";
    const router = fetchRouter({
      [`GET ${VIEW_PATH}`]: () =>
        router.jsonResponse(
          mode === "conflict"
            ? viewPayload()
            : viewPayload({ actions: [], claim_status: "claimed" }),
        ),
      [`POST ${CLAIM_PATH}`]: () => {
        if (mode === "conflict") {
          return router.errorResponse(
            409,
            "S05_CONFLICT",
            "EXCEPTION_WORK_ITEM_ALREADY_CLAIMED",
          );
        }
        return router.errorResponse(503, "S05_STOPPED");
      },
    });
    const user = userEvent.setup();
    renderWithQuery(
      <BusinessExceptionApproverPanel requestId={REQUEST_ID} />,
    );
    await waitFor(() =>
      expect(screen.getByTestId("approver-claim-button")).toBeEnabled(),
    );
    await user.click(screen.getByTestId("approver-claim-button"));
    await waitFor(() =>
      expect(screen.getByTestId("approver-outcome")).toHaveTextContent(
        "EXCEPTION_WORK_ITEM_ALREADY_CLAIMED",
      ),
    );
  });
});
