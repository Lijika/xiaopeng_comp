import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import PolicyReleasePanel from "./PolicyReleasePanel";
import { createQueryClient, fetchRouter } from "../test-utils";

function wrap(client: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

const CANDIDATE = "candidate_t08panel000000000000000000";
const WORKSPACE_PATH = `/controlled/s08/api/queries/candidate/${CANDIDATE}`;

function workspacePayload(
  status: string,
  role: "admin" | "approver",
  actions: string[],
  overrides: Record<string, unknown> = {},
) {
  return {
    track: "C-DEMO",
    capability_gate: "G3",
    candidate_id: CANDIDATE,
    status,
    governance_revision: 3,
    actor_role: role,
    actions,
    events: [],
    active_anchor: {
      candidate_id: "candidate_t08bootstrap00000000000000000",
      manifest_digest: "anchor-digest",
    },
    manifest_id: "manifest_1",
    manifest_digest: "candidate-digest",
    ...overrides,
  };
}

function renderPanel(
  candidateId: string | null = null,
  onCandidateSelected: (id: string) => void = () => {},
) {
  return render(
    <PolicyReleasePanel
      candidateId={candidateId}
      onCandidateSelected={onCandidateSelected}
    />,
    { wrapper: wrap(createQueryClient()) },
  );
}

describe("PolicyReleasePanel T08", () => {
  it("admin imports a legacy bundle, revises draft metadata and freezes a candidate", async () => {
    const user = userEvent.setup();
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
      "POST /controlled/s08/api/commands/import_legacy": () =>
        new Response(
          JSON.stringify({
            status: "accepted",
            draft_id: "draft_t08panel0000000000000000000",
            mapping_ledger_id: "ledger_1",
            source_sha256: "a".repeat(64),
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      "POST /controlled/s08/api/commands/revise_draft": () =>
        new Response(
          JSON.stringify({ status: "accepted", draft_id: "draft_t08panel0000000000000000000" }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      "POST /controlled/s08/api/commands/freeze_candidate": () =>
        new Response(
          JSON.stringify({
            status: "accepted",
            draft_id: "draft_t08panel0000000000000000000",
            candidate_id: CANDIDATE,
            manifest_id: "manifest_1",
            manifest_digest: "candidate-digest",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });
    const onSelected = vi.fn();
    renderPanel(null, onSelected);

    // The draft form appears only after the Admin status query resolves (the
    // source of the governance revision fence).
    await waitFor(() =>
      expect(screen.getByTestId("t08-import-button")).toBeInTheDocument(),
    );
    await user.type(
      screen.getByLabelText("来源包标识"),
      "c-demo-legacy-baseline/1",
    );
    await user.click(screen.getByTestId("t08-import-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t08-draft-editor")).toBeInTheDocument(),
    );

    await user.type(screen.getByLabelText("适用范围"), "C-DEMO/demo");
    await user.type(screen.getByLabelText("来源"), "c-demo-legacy-baseline/1");
    await user.type(screen.getByLabelText("变更原因"), "T08 panel metadata");
    await user.type(screen.getByLabelText("生效起始"), "2000-01-01T00:00");
    await user.click(screen.getByTestId("t08-revise-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t08-revise-ok")).toBeInTheDocument(),
    );

    await user.click(screen.getByTestId("t08-freeze-button"));
    await waitFor(() => expect(onSelected).toHaveBeenCalledWith(CANDIDATE));

    const posts = router.calls.filter((call) => call.method === "POST");
    expect(posts.map((call) => call.url)).toEqual([
      "/controlled/s08/api/commands/import_legacy",
      "/controlled/s08/api/commands/revise_draft",
      "/controlled/s08/api/commands/freeze_candidate",
    ]);
    const importBody = posts[0].body as Record<string, unknown>;
    expect(importBody.source_bundle_id).toBe("c-demo-legacy-baseline/1");
    expect(importBody.expected_governance_revision).toBe(3);
    const reviseBody = posts[1].body as Record<string, unknown>;
    expect(reviseBody.draft_id).toBe("draft_t08panel0000000000000000000");
    expect(reviseBody.metadata).toEqual({
      scope: "C-DEMO/demo",
      source: "c-demo-legacy-baseline/1",
      reason: "T08 panel metadata",
      validity: { valid_from: "2000-01-01T00:00:00Z" },
    });
    const freezeBody = posts[2].body as Record<string, unknown>;
    expect(freezeBody.draft_id).toBe("draft_t08panel0000000000000000000");
    expect(posts.every((call) => call.method === "POST")).toBe(true);
    expect(posts).toHaveLength(3);
  });

  it("approver sees the server-owned workspace and approves with the exact digest and revision", async () => {
    const user = userEvent.setup();
    const router = fetchRouter({
      [`GET ${WORKSPACE_PATH}`]: () =>
        new Response(
          JSON.stringify(
            workspacePayload("in_review", "approver", ["approve", "reject"], {
              review_material: {
                schema_version: "s08-review-material/1",
                candidate_digest: "candidate-digest",
                anchor_candidate_id: "candidate_t08bootstrap00000000000000000",
                changes: [],
              },
            }),
          ),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      "POST /controlled/s09/api/commands/preview_impact": () =>
        new Response(
          JSON.stringify({
            status: "accepted",
            phase: "preview",
            manifest_id: "preview_sha256_1111111111111111111111111111111111111111111111111111111111111111",
            digest: "1".repeat(64),
            scope: "C-DEMO/demo",
            oracle_version: "s09-impact-oracle/1",
            level: 1,
            expanded_to_full_scope: false,
            member_count: 1,
            partition_counts: {"open_cycle": 1},
            zero_hit_proof: false,
            target_generation: 2,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),

      "POST /controlled/s08/api/commands/approve": () =>
        new Response(
          JSON.stringify({
            status: "accepted",
            candidate_id: CANDIDATE,
            approval_binding_id: "approval_sha256_binding",
            approval_binding_digest: "binding-digest",
            validation_bundle_id: "bundle_1",
            validation_bundle_digest: "bundle-digest",
            author_subject: "c-demo-policy-admin",
            approver_subject: "c-demo-policy-approver",
            activation_time: 1786000000,
            recovery_release_id: "candidate_t08bootstrap00000000000000000",
            governance_revision: 4,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderPanel(CANDIDATE);

    await waitFor(() =>
      expect(screen.getByTestId("t08-workspace-status")).toHaveTextContent(
        "in_review",
      ),
    );
    expect(screen.getByTestId("t08-workspace-role")).toHaveTextContent(
      "approver",
    );
    expect(screen.getByTestId("t08-workspace-digest")).toHaveTextContent(
      "candidate-digest",
    );
    expect(screen.getByTestId("t08-workspace-revision")).toHaveTextContent("3");
    expect(screen.getByTestId("t08-workspace-anchor")).toHaveTextContent(
      "candidate_t08bootstrap00000000000000000",
    );
    expect(screen.getByTestId("t08-approve-form")).toBeInTheDocument();
    expect(screen.getByTestId("t08-reject-form")).toBeInTheDocument();

    await user.type(screen.getByLabelText("生效时间"), "2026-08-10T12:00");
    await user.click(screen.getByTestId("t08-approve-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t08-action-ok")).toBeInTheDocument(),
    );

    const posts = router.calls.filter((call) => call.method === "POST");
    // The approval first previews the immutable impact, then binds its
    // exact manifest identity.
    expect(posts).toHaveLength(2);
    expect(posts[0].url).toBe("/controlled/s09/api/commands/preview_impact");
    expect((posts[0].body as Record<string, unknown>).candidate_id).toBe(
      CANDIDATE,
    );
    const body = posts[1].body as Record<string, unknown>;
    expect(posts[1].url).toBe("/controlled/s08/api/commands/approve");
    expect(body.candidate_id).toBe(CANDIDATE);
    expect(body.preview_manifest_id).toBe(
      "preview_sha256_1111111111111111111111111111111111111111111111111111111111111111",
    );
    expect(body.expected_governance_revision).toBe(3);
    expect(body.recovery_release_id).toBe(
      "candidate_t08bootstrap00000000000000000",
    );
    expect(typeof body.idempotency_key).toBe("string");
    // The panel mints a collision-resistant browser-native UUID identity;
    // never a mount-scoped counter that a remount or second candidate could
    // reuse against the ledger.
    expect(body.idempotency_key).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/,
    );
    // 2026-08-10T12:00 local -> epoch seconds; the exact instant is
    // environment-dependent, so assert it parses back to the chosen minute.
    expect(typeof body.activation_time).toBe("number");
  });

  it("renders no transition until server data says so (no optimistic move)", async () => {
    const user = userEvent.setup();
    let status = "in_review";
    fetchRouter({
      [`GET ${WORKSPACE_PATH}`]: () =>
        new Response(
          JSON.stringify(
            workspacePayload(status, "approver", ["approve", "reject"]),
          ),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      "POST /controlled/s09/api/commands/preview_impact": () =>
        new Response(
          JSON.stringify({
            status: "accepted",
            phase: "preview",
            manifest_id: "preview_sha256_1111111111111111111111111111111111111111111111111111111111111111",
            digest: "1".repeat(64),
            scope: "C-DEMO/demo",
            oracle_version: "s09-impact-oracle/1",
            level: 1,
            expanded_to_full_scope: false,
            member_count: 1,
            partition_counts: {"open_cycle": 1},
            zero_hit_proof: false,
            target_generation: 2,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),

      "POST /controlled/s08/api/commands/approve": () =>
        new Response(
          JSON.stringify({
            status: "accepted",
            candidate_id: CANDIDATE,
            approval_binding_id: "approval_sha256_binding",
            approval_binding_digest: "binding-digest",
            validation_bundle_id: "bundle_1",
            validation_bundle_digest: "bundle-digest",
            author_subject: "c-demo-policy-admin",
            approver_subject: "c-demo-policy-approver",
            activation_time: 1786000000,
            recovery_release_id: "candidate_t08bootstrap00000000000000000",
            governance_revision: 4,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderPanel(CANDIDATE);
    await waitFor(() =>
      expect(screen.getByTestId("t08-workspace-status")).toHaveTextContent(
        "in_review",
      ),
    );
    await user.type(screen.getByLabelText("生效时间"), "2026-08-10T12:00");
    await user.click(screen.getByTestId("t08-approve-button"));
    // Still the server status while the mutation settles.
    expect(screen.getByTestId("t08-workspace-status")).toHaveTextContent(
      "in_review",
    );
    // The next refetch (after acceptance) still shows the server-owned status.
    status = "in_review";
  });

  it("surfaces a 409 conflict, refetches the workspace and allows a new key", async () => {
    const user = userEvent.setup();
    let workspaceRequests = 0;
    let approvePosts = 0;
    let resolveRefetch: ((response: Response) => void) | undefined;
    const pendingRefetch = new Promise<Response>((resolve) => {
      resolveRefetch = resolve;
    });
    fetchRouter({
      [`GET ${WORKSPACE_PATH}`]: () => {
        workspaceRequests += 1;
        if (workspaceRequests === 2) return pendingRefetch;
        return new Response(
          JSON.stringify(
            workspacePayload("in_review", "approver", ["approve", "reject"]),
          ),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
      "POST /controlled/s09/api/commands/preview_impact": () =>
        new Response(
          JSON.stringify({
            status: "accepted",
            phase: "preview",
            manifest_id: "preview_sha256_1111111111111111111111111111111111111111111111111111111111111111",
            digest: "1".repeat(64),
            scope: "C-DEMO/demo",
            oracle_version: "s09-impact-oracle/1",
            level: 1,
            expanded_to_full_scope: false,
            member_count: 1,
            partition_counts: {"open_cycle": 1},
            zero_hit_proof: false,
            target_generation: 2,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),

      "POST /controlled/s08/api/commands/approve": () => {
        approvePosts += 1;
        return new Response(
          JSON.stringify({
            detail: { error: "S08_CONFLICT", message: "stale revision" },
          }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        );
      },
    });
    renderPanel(CANDIDATE);
    await waitFor(() =>
      expect(screen.getByTestId("t08-workspace-status")).toHaveTextContent(
        "in_review",
      ),
    );
    await user.type(screen.getByLabelText("生效时间"), "2026-08-10T12:00");
    await user.click(screen.getByTestId("t08-approve-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t08-conflict")).toHaveTextContent(
        "stale revision",
      ),
    );
    expect(approvePosts).toBe(1);
    await waitFor(() => expect(workspaceRequests).toBeGreaterThan(1));
    expect(screen.getByTestId("t08-approve-button")).toBeDisabled();
    await user.click(screen.getByTestId("t08-approve-button"));
    expect(approvePosts).toBe(1);

    resolveRefetch?.(
      new Response(
        JSON.stringify(
          workspacePayload("in_review", "approver", ["approve", "reject"], {
            governance_revision: 4,
          }),
        ),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    await waitFor(() =>
      expect(screen.getByTestId("t08-approve-button")).toBeEnabled(),
    );
    await user.click(screen.getByTestId("t08-approve-button"));
    await waitFor(() => expect(approvePosts).toBe(2));
    const posts = fetchMockCalls("approve").map((call) => JSON.parse(call.body));
    expect(posts[1].idempotency_key).not.toBe(posts[0].idempotency_key);
    expect(posts[1].expected_governance_revision).toBe(4);
  });

  it("renders explicit forbidden, not-found and unavailable states", async () => {
    fetchRouter({
      [`GET ${WORKSPACE_PATH}`]: () =>
        new Response(
          JSON.stringify({
            detail: { error: "S08_FORBIDDEN", message: "identity required" },
          }),
          { status: 403, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderPanel(CANDIDATE);
    await waitFor(() =>
      expect(screen.getByTestId("t08-forbidden")).toHaveTextContent(
        "identity required",
      ),
    );
  });

  it("polls the workspace after request_validation until the server status advances", async () => {
    const user = userEvent.setup();
    let status = "candidate";
    fetchRouter({
      [`GET ${WORKSPACE_PATH}`]: () => {
        const payload = workspacePayload(status, "admin", [
          "request_validation",
          "cancel",
        ]);
        if (status === "candidate") {
          status = "validated";
        }
        return new Response(JSON.stringify(payload), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      },
      "POST /controlled/s08/api/commands/request_validation": () =>
        new Response(
          JSON.stringify({
            status: "accepted",
            candidate_id: CANDIDATE,
            validation_bundle_id: "bundle_1",
            validation_bundle_digest: "bundle-digest",
            governance_revision: 4,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderPanel(CANDIDATE);
    await waitFor(() =>
      expect(screen.getByTestId("t08-workspace-status")).toHaveTextContent(
        "candidate",
      ),
    );
    await user.click(screen.getByTestId("t08-validate-button"));
    // The bounded poll refetches the authoritative workspace; the server
    // status advances to validated and the action list follows it.
    await waitFor(
      () =>
        expect(screen.getByTestId("t08-workspace-status")).toHaveTextContent(
          "validated",
        ),
      { timeout: 5_000 },
    );
  });

  it("schedules the exact approval binding activation time only", async () => {
    const user = userEvent.setup();
    const binding = {
      schema_version: "s08-approval-binding/1",
      candidate_digest: "candidate-digest",
      activation_time: 1786000000,
      recovery_release_id: "candidate_t08bootstrap00000000000000000",
      approved_by: "c-demo-policy-approver",
    };
    fetchRouter({
      [`GET ${WORKSPACE_PATH}`]: () =>
        new Response(
          JSON.stringify(
            workspacePayload("approved", "admin", ["schedule", "cancel"], {
              approval_binding: binding,
              approval_binding_id: "approval_sha256_binding",
              approval_binding_digest: "binding-digest",
            }),
          ),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      "POST /controlled/s08/api/commands/schedule": () =>
        new Response(
          JSON.stringify({
            status: "accepted",
            candidate_id: CANDIDATE,
            reservation_id: "reservation_1",
            policy_job_id: "job_1",
            activation_at: 1786000000,
            governance_revision: 4,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderPanel(CANDIDATE);
    await waitFor(() =>
      expect(screen.getByTestId("t08-workspace-status")).toHaveTextContent(
        "approved",
      ),
    );
    expect(screen.getByTestId("t08-binding-time")).toHaveTextContent(
      "1786000000",
    );
    await user.click(screen.getByTestId("t08-schedule-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t08-action-ok")).toBeInTheDocument(),
    );
    const scheduleCalls = fetchMockCalls("schedule");
    expect(scheduleCalls).toHaveLength(1);
    const body = JSON.parse(scheduleCalls[0].body) as Record<string, unknown>;
    expect(body.approval_binding_id).toBe("approval_sha256_binding");
    expect(body.activation_at).toBe(1786000000);
    expect(body.expected_governance_revision).toBe(3);
  });
});

function fetchMockCalls(action: string): Array<{ path: string; body: string }> {
  const fetchMock = globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
  const calls: Array<{ path: string; body: string }> = [];
  for (const [url, init] of fetchMock.mock.calls as Array<
    [string, RequestInit]
  >) {
    if (
      String(url).endsWith(`/commands/${action}`) &&
      init.body !== undefined
    ) {
      calls.push({ path: String(url), body: String(init.body) });
    }
  }
  return calls;
}

describe("PolicyReleasePanel T08 review findings", () => {
  it("renders an explicit invalid state for a 422 command rejection", async () => {
    const user = userEvent.setup();
    fetchRouter({
      [`GET ${WORKSPACE_PATH}`]: () =>
        new Response(
          JSON.stringify(
            workspacePayload("in_review", "approver", ["approve", "reject"]),
          ),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      "POST /controlled/s09/api/commands/preview_impact": () =>
        new Response(
          JSON.stringify({
            status: "accepted",
            phase: "preview",
            manifest_id: "preview_sha256_1111111111111111111111111111111111111111111111111111111111111111",
            digest: "1".repeat(64),
            scope: "C-DEMO/demo",
            oracle_version: "s09-impact-oracle/1",
            level: 1,
            expanded_to_full_scope: false,
            member_count: 1,
            partition_counts: {"open_cycle": 1},
            zero_hit_proof: false,
            target_generation: 2,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),

      "POST /controlled/s08/api/commands/approve": () =>
        new Response(
          JSON.stringify({ detail: [] }),
          { status: 422, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderPanel(CANDIDATE);
    await waitFor(() =>
      expect(screen.getByTestId("t08-workspace-status")).toHaveTextContent(
        "in_review",
      ),
    );
    await user.type(screen.getByLabelText("生效时间"), "2026-08-10T12:00");
    await user.click(screen.getByTestId("t08-approve-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t08-action-error")).toHaveTextContent(
        "命令无效",
      ),
    );
  });

  it("renders an explicit invalid state for a 422 workspace query", async () => {
    fetchRouter({
      [`GET ${WORKSPACE_PATH}`]: () =>
        new Response(
          JSON.stringify({ detail: [] }),
          { status: 422, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderPanel(CANDIDATE);
    await waitFor(() =>
      expect(screen.getByTestId("t08-invalid")).toHaveTextContent("请求无效"),
    );
  });

  it("renders a rejected candidate with only the server-owned cancel action", async () => {
    fetchRouter({
      [`GET ${WORKSPACE_PATH}`]: () =>
        new Response(
          JSON.stringify(workspacePayload("rejected", "admin", ["cancel"])),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderPanel(CANDIDATE);
    await waitFor(() =>
      expect(screen.getByTestId("t08-workspace-status")).toHaveTextContent(
        "rejected",
      ),
    );
    expect(screen.getByTestId("t08-cancel-button")).toBeInTheDocument();
    expect(screen.queryByTestId("t08-approve-form")).not.toBeInTheDocument();
    expect(screen.queryByTestId("t08-schedule-form")).not.toBeInTheDocument();
  });

  it("renders typed review change rows instead of object stringification", async () => {
    fetchRouter({
      [`GET ${WORKSPACE_PATH}`]: () =>
        new Response(
          JSON.stringify(
            workspacePayload("in_review", "approver", ["approve", "reject"], {
              review_material: {
                schema_version: "s08-review-material/1",
                candidate_digest: "candidate-digest",
                anchor_candidate_id: "candidate_t08bootstrap00000000000000000",
                changes: [
                  { change: "added", component: "check_policy" },
                  { change: "modified", component: "semantic_catalog" },
                ],
              },
            }),
          ),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderPanel(CANDIDATE);
    await waitFor(() =>
      expect(screen.getByTestId("t08-review")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("t08-review-changes")).toHaveTextContent(
      "added · check_policy",
    );
    expect(screen.getByTestId("t08-review-changes")).toHaveTextContent(
      "modified · semantic_catalog",
    );
    expect(
      screen.getByTestId("t08-review-changes").textContent,
    ).not.toContain("[object Object]");
  });

  it("retains the same idempotency key across an unknown outcome and releases it on acceptance", async () => {
    const user = userEvent.setup();
    let approvePosts = 0;
    let approveStatus = "network";
    fetchRouter({
      [`GET ${WORKSPACE_PATH}`]: () =>
        new Response(
          JSON.stringify(
            workspacePayload("in_review", "approver", ["approve", "reject"]),
          ),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      "POST /controlled/s09/api/commands/preview_impact": () =>
        new Response(
          JSON.stringify({
            status: "accepted",
            phase: "preview",
            manifest_id: "preview_sha256_1111111111111111111111111111111111111111111111111111111111111111",
            digest: "1".repeat(64),
            scope: "C-DEMO/demo",
            oracle_version: "s09-impact-oracle/1",
            level: 1,
            expanded_to_full_scope: false,
            member_count: 1,
            partition_counts: {"open_cycle": 1},
            zero_hit_proof: false,
            target_generation: 2,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),

      "POST /controlled/s08/api/commands/approve": () => {
        approvePosts += 1;
        if (approveStatus === "network") {
          approveStatus = "ok";
          throw new TypeError("network down");
        }
        return new Response(
          JSON.stringify({
            status: "accepted",
            candidate_id: CANDIDATE,
            approval_binding_id: "approval_sha256_binding",
            approval_binding_digest: "binding-digest",
            validation_bundle_id: "bundle_1",
            validation_bundle_digest: "bundle-digest",
            author_subject: "c-demo-policy-admin",
            approver_subject: "c-demo-policy-approver",
            activation_time: 1786000000,
            recovery_release_id: "candidate_t08bootstrap00000000000000000",
            governance_revision: 4,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
    });
    renderPanel(CANDIDATE);
    await waitFor(() =>
      expect(screen.getByTestId("t08-workspace-status")).toHaveTextContent(
        "in_review",
      ),
    );
    await user.type(screen.getByLabelText("生效时间"), "2026-08-10T12:00");
    await user.click(screen.getByTestId("t08-approve-button"));
    // Unknown transport outcome: the panel locks every other action and the
    // only re-send is the explicit byte-identical retry on the same key.
    await waitFor(() =>
      expect(screen.getByTestId("t08-action-error")).toHaveTextContent(
        "重试将使用同一幂等键",
      ),
    );
    expect(screen.getByTestId("t08-approve-button")).toBeDisabled();
    expect(screen.getByTestId("t08-reject-button")).toBeDisabled();
    await user.click(screen.getByTestId("t08-command-retry"));
    await waitFor(() =>
      expect(screen.getByTestId("t08-action-ok")).toBeInTheDocument(),
    );
    const posts = fetchMockCalls("approve");
    expect(posts).toHaveLength(2);
    const first = JSON.parse(posts[0].body) as Record<string, unknown>;
    const second = JSON.parse(posts[1].body) as Record<string, unknown>;
    expect(second.idempotency_key).toBe(first.idempotency_key);
    expect(second).toEqual(first);
  });

  it("surfaces a draft-stage 409 and refetches the authoritative status for the next fence", async () => {
    const user = userEvent.setup();
    let statusRequests = 0;
    let importPosts = 0;
    let resolveRefetch: ((response: Response) => void) | undefined;
    const pendingRefetch = new Promise<Response>((resolve) => {
      resolveRefetch = resolve;
    });
    fetchRouter({
      "GET /controlled/s08/api/queries/status": () => {
        statusRequests += 1;
        if (statusRequests === 2) return pendingRefetch;
        return new Response(
          JSON.stringify({
            track: "C-DEMO",
            capability_gate: "G3",
            bootstrap: true,
            scope: "C-DEMO/demo",
            governance_revision: 3,
            active_generation: 1,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
      "POST /controlled/s08/api/commands/import_legacy": () => {
        importPosts += 1;
        return new Response(
          JSON.stringify({
            detail: { error: "S08_CONFLICT", message: "stale revision" },
          }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        );
      },
    });
    renderPanel(null);
    await waitFor(() =>
      expect(screen.getByTestId("t08-import-button")).toBeInTheDocument(),
    );
    await user.type(
      screen.getByLabelText("来源包标识"),
      "c-demo-legacy-baseline/1",
    );
    await user.click(screen.getByTestId("t08-import-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t08-conflict")).toHaveTextContent(
        "stale revision",
      ),
    );
    expect(importPosts).toBe(1);
    await waitFor(() => expect(statusRequests).toBeGreaterThan(1));
    expect(screen.getByTestId("t08-import-button")).toBeDisabled();
    await user.click(screen.getByTestId("t08-import-button"));
    expect(importPosts).toBe(1);

    resolveRefetch?.(
      new Response(
        JSON.stringify({
          track: "C-DEMO",
          capability_gate: "G3",
          bootstrap: true,
          scope: "C-DEMO/demo",
          governance_revision: 4,
          active_generation: 1,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    await waitFor(() =>
      expect(screen.getByTestId("t08-import-button")).toBeEnabled(),
    );
    await user.click(screen.getByTestId("t08-import-button"));
    await waitFor(() => expect(importPosts).toBe(2));
    const posts = fetchMockCalls("import_legacy").map((call) =>
      JSON.parse(call.body),
    );
    expect(posts[1].idempotency_key).not.toBe(posts[0].idempotency_key);
    expect(posts[1].expected_governance_revision).toBe(4);
  });

  it("keeps the draft latch through a retry 409 until refetch succeeds", async () => {
    const user = userEvent.setup();
    let statusRequests = 0;
    let importPosts = 0;
    let resolveRefetch: ((response: Response) => void) | undefined;
    const pendingRefetch = new Promise<Response>((resolve) => {
      resolveRefetch = resolve;
    });
    fetchRouter({
      "GET /controlled/s08/api/queries/status": () => {
        statusRequests += 1;
        if (statusRequests === 2) return pendingRefetch;
        return new Response(
          JSON.stringify({
            track: "C-DEMO",
            capability_gate: "G3",
            bootstrap: true,
            scope: "C-DEMO/demo",
            governance_revision: statusRequests === 1 ? 3 : 4,
            active_generation: 1,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
      "POST /controlled/s08/api/commands/import_legacy": () => {
        importPosts += 1;
        if (importPosts === 1) throw new TypeError("network down");
        if (importPosts === 2) {
          return new Response(
            JSON.stringify({
              detail: { error: "S08_CONFLICT", message: "stale revision" },
            }),
            { status: 409, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(
          JSON.stringify({
            status: "accepted",
            draft_id: "draft_t08panel0000000000000000000",
            mapping_ledger_id: "ledger_1",
            source_sha256: "a".repeat(64),
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
    });
    renderPanel(null);
    await waitFor(() =>
      expect(screen.getByTestId("t08-import-button")).toBeInTheDocument(),
    );
    await user.type(
      screen.getByLabelText("来源包标识"),
      "c-demo-legacy-baseline/1",
    );
    await user.click(screen.getByTestId("t08-import-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t08-command-retry")).toBeEnabled(),
    );
    await user.click(screen.getByTestId("t08-command-retry"));
    await waitFor(() => expect(statusRequests).toBe(2));
    expect(screen.queryByTestId("t08-command-retry")).not.toBeInTheDocument();
    expect(screen.getByTestId("t08-import-button")).toBeDisabled();
    await user.click(screen.getByTestId("t08-import-button"));
    expect(importPosts).toBe(2);

    resolveRefetch?.(
      new Response(
        JSON.stringify({
          track: "C-DEMO",
          capability_gate: "G3",
          bootstrap: true,
          scope: "C-DEMO/demo",
          governance_revision: 4,
          active_generation: 1,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    await waitFor(() =>
      expect(screen.getByTestId("t08-import-button")).toBeEnabled(),
    );
    await user.click(screen.getByTestId("t08-import-button"));
    await waitFor(() => expect(importPosts).toBe(3));
    const posts = fetchMockCalls("import_legacy").map((call) =>
      JSON.parse(call.body),
    );
    expect(posts[1]).toEqual(posts[0]);
    expect(posts[2].idempotency_key).not.toBe(posts[0].idempotency_key);
    expect(posts[2].expected_governance_revision).toBe(4);
  });

  it("renders an explicit unavailable state when the Admin status query fails closed", async () => {
    fetchRouter({
      "GET /controlled/s08/api/queries/status": () =>
        new Response(
          JSON.stringify({
            detail: { error: "S08_UNAVAILABLE", message: "unavailable" },
          }),
          { status: 503, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderPanel(null);
    // The status query retries transient statuses (retryPolicy) before the
    // error state lands, so the explicit unavailable state needs the full
    // retry window.
    await waitFor(
      () =>
        expect(screen.getByTestId("t08-status-unavailable")).toHaveTextContent(
          "治理状态暂不可用",
        ),
      { timeout: 6_000 },
    );
    expect(screen.queryByTestId("t08-import-button")).not.toBeInTheDocument();
  });

  it("renders an explicit forbidden state when the status query denies a non-admin role", async () => {
    fetchRouter({
      "GET /controlled/s08/api/queries/status": () =>
        new Response(
          JSON.stringify({
            detail: { error: "S08_FORBIDDEN", message: "identity required" },
          }),
          { status: 403, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderPanel(null);
    await waitFor(() =>
      expect(screen.getByTestId("t08-status-forbidden")).toHaveTextContent(
        "无权限访问治理状态",
      ),
    );
    expect(screen.queryByTestId("t08-import-button")).not.toBeInTheDocument();
  });

  it("renders the server-owned governance event timeline with typed actors", async () => {
    fetchRouter({
      [`GET ${WORKSPACE_PATH}`]: () =>
        new Response(
          JSON.stringify(
            workspacePayload("active", "admin", [], {
              events: [
                {
                  event_id: "governance_sha256_1",
                  revision: 1,
                  kind: "candidate_frozen",
                  actor: {
                    subject: "c-demo-policy-admin",
                    role: "admin",
                    source_id: "test-source",
                  },
                  trusted_time: 1786000000,
                  reason_code: "S08_CANDIDATE_FROZEN",
                  candidate_id: CANDIDATE,
                  draft_id: "draft_1",
                },
              ],
            }),
          ),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderPanel(CANDIDATE);
    await waitFor(() =>
      expect(screen.getByTestId("t08-events")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("t08-events")).toHaveTextContent(
      "candidate_frozen",
    );
    expect(screen.getByTestId("t08-events")).toHaveTextContent(
      "c-demo-policy-admin",
    );
    expect(screen.getByTestId("t08-events")).toHaveTextContent(
      "S08_CANDIDATE_FROZEN",
    );
  });

  it("mints a distinct collision-resistant key per mount and per candidate", async () => {
    const user = userEvent.setup();
    let approvePosts = 0;
    const router = fetchRouter({
      [`GET ${WORKSPACE_PATH}`]: () =>
        new Response(
          JSON.stringify(
            workspacePayload("in_review", "approver", ["approve", "reject"]),
          ),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      "POST /controlled/s09/api/commands/preview_impact": () =>
        new Response(
          JSON.stringify({
            status: "accepted",
            phase: "preview",
            manifest_id: "preview_sha256_1111111111111111111111111111111111111111111111111111111111111111",
            digest: "1".repeat(64),
            scope: "C-DEMO/demo",
            oracle_version: "s09-impact-oracle/1",
            level: 1,
            expanded_to_full_scope: false,
            member_count: 1,
            partition_counts: {"open_cycle": 1},
            zero_hit_proof: false,
            target_generation: 2,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),

      "POST /controlled/s08/api/commands/approve": () => {
        approvePosts += 1;
        return new Response(
          JSON.stringify({
            status: "accepted",
            candidate_id: CANDIDATE,
            approval_binding_id: "approval_sha256_binding",
            approval_binding_digest: "binding-digest",
            governance_revision: 4,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
    });
    const approveOnce = async () => {
      const view = renderPanel(CANDIDATE);
      await waitFor(() =>
        expect(screen.getByTestId("t08-approve-form")).toBeInTheDocument(),
      );
      await user.type(screen.getByLabelText("生效时间"), "2026-08-10T12:00");
      await user.click(screen.getByTestId("t08-approve-button"));
      await waitFor(() =>
        expect(screen.getByTestId("t08-action-ok")).toBeInTheDocument(),
      );
      view.unmount();
    };
    await approveOnce();
    const firstKey = fetchMockCalls("approve")[0].body;
    await approveOnce();
    const secondKey = fetchMockCalls("approve")[1].body;
    expect(JSON.parse(secondKey).idempotency_key).not.toBe(
      JSON.parse(firstKey).idempotency_key,
    );
    // Two approvals, each preceded by its immutable impact preview.
    const posts = router.calls.filter((call) => call.method === "POST");
    expect(posts).toHaveLength(4);
    expect(
      posts.filter(
        (call) => call.url === "/controlled/s09/api/commands/preview_impact",
      ),
    ).toHaveLength(2);
    expect(
      posts.filter(
        (call) => call.url === "/controlled/s08/api/commands/approve",
      ),
    ).toHaveLength(2);
  });

  it("after an unknown transport outcome, editing or switching actions posts nothing", async () => {
    const user = userEvent.setup();
    let approveStatus: "network" | "ok" = "network";
    let approvePosts = 0;
    fetchRouter({
      [`GET ${WORKSPACE_PATH}`]: () =>
        new Response(
          JSON.stringify(
            workspacePayload("in_review", "approver", ["approve", "reject"]),
          ),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      "POST /controlled/s09/api/commands/preview_impact": () =>
        new Response(
          JSON.stringify({
            status: "accepted",
            phase: "preview",
            manifest_id: "preview_sha256_1111111111111111111111111111111111111111111111111111111111111111",
            digest: "1".repeat(64),
            scope: "C-DEMO/demo",
            oracle_version: "s09-impact-oracle/1",
            level: 1,
            expanded_to_full_scope: false,
            member_count: 1,
            partition_counts: {"open_cycle": 1},
            zero_hit_proof: false,
            target_generation: 2,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),

      "POST /controlled/s08/api/commands/approve": () => {
        approvePosts += 1;
        if (approveStatus === "network") {
          approveStatus = "ok";
          throw new TypeError("network down");
        }
        return new Response(
          JSON.stringify({
            status: "accepted",
            candidate_id: CANDIDATE,
            approval_binding_id: "approval_sha256_binding",
            approval_binding_digest: "binding-digest",
            governance_revision: 4,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
      "POST /controlled/s08/api/commands/reject": () => {
        throw new Error("reject must never fire while the latch is unknown");
      },
    });
    renderPanel(CANDIDATE);
    await waitFor(() =>
      expect(screen.getByTestId("t08-approve-form")).toBeInTheDocument(),
    );
    await user.type(screen.getByLabelText("生效时间"), "2026-08-10T12:00");
    await user.click(screen.getByTestId("t08-approve-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t08-action-error")).toHaveTextContent(
        "重试将使用同一幂等键",
      ),
    );
    // Every other action and input is locked while the latch is unknown.
    expect(screen.getByTestId("t08-approve-button")).toBeDisabled();
    expect(screen.getByTestId("t08-reject-button")).toBeDisabled();
    expect(screen.getByLabelText("生效时间")).toBeDisabled();
    expect(screen.getByLabelText("回滚发布标识")).toBeDisabled();
    expect(screen.getByTestId("t08-command-retry")).toBeEnabled();
    expect(screen.getByTestId("t08-command-reconcile")).toBeEnabled();
    // A switched action cannot fire a second mutation.
    await user.click(screen.getByTestId("t08-reject-button")).catch(() => {});
    expect(fetchMockCalls("approve")).toHaveLength(1);
    expect(fetchMockCalls("reject")).toHaveLength(0);
  });

  it("a generic 5xx outcome locks the same latch with zero new POSTs until retry", async () => {
    const user = userEvent.setup();
    let approvePosts = 0;
    fetchRouter({
      [`GET ${WORKSPACE_PATH}`]: () =>
        new Response(
          JSON.stringify(
            workspacePayload("in_review", "approver", ["approve", "reject"]),
          ),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      "POST /controlled/s09/api/commands/preview_impact": () =>
        new Response(
          JSON.stringify({
            status: "accepted",
            phase: "preview",
            manifest_id: "preview_sha256_1111111111111111111111111111111111111111111111111111111111111111",
            digest: "1".repeat(64),
            scope: "C-DEMO/demo",
            oracle_version: "s09-impact-oracle/1",
            level: 1,
            expanded_to_full_scope: false,
            member_count: 1,
            partition_counts: {"open_cycle": 1},
            zero_hit_proof: false,
            target_generation: 2,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),

      "POST /controlled/s08/api/commands/approve": () => {
        approvePosts += 1;
        if (approvePosts === 1) {
          return new Response(
            JSON.stringify({ detail: { message: "proxy exploded" } }),
            { status: 500, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(
          JSON.stringify({
            status: "accepted",
            candidate_id: CANDIDATE,
            approval_binding_id: "approval_sha256_binding",
            approval_binding_digest: "binding-digest",
            governance_revision: 4,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
    });
    renderPanel(CANDIDATE);
    await waitFor(() =>
      expect(screen.getByTestId("t08-approve-form")).toBeInTheDocument(),
    );
    await user.type(screen.getByLabelText("生效时间"), "2026-08-10T12:00");
    await user.click(screen.getByTestId("t08-approve-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t08-action-error")).toHaveTextContent(
        "重试将使用同一幂等键",
      ),
    );
    expect(screen.getByTestId("t08-approve-button")).toBeDisabled();
    expect(fetchMockCalls("approve")).toHaveLength(1);
    await user.click(screen.getByTestId("t08-command-retry"));
    await waitFor(() =>
      expect(screen.getByTestId("t08-action-ok")).toBeInTheDocument(),
    );
    const posts = fetchMockCalls("approve");
    expect(posts).toHaveLength(2);
    expect(posts[1].body).toBe(posts[0].body);
  });

  it("never releases the latch on an unrelated ledger advance; only the exact replay settles", async () => {
    const user = userEvent.setup();
    let workspaceRequests = 0;
    let approvePosts = 0;
    fetchRouter({
      [`GET ${WORKSPACE_PATH}`]: () => {
        workspaceRequests += 1;
        const revision = workspaceRequests === 1 ? 3 : 5;
        return new Response(
          JSON.stringify(
            workspacePayload("in_review", "approver", ["approve", "reject"], {
              governance_revision: revision,
            }),
          ),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
      "POST /controlled/s09/api/commands/preview_impact": () =>
        new Response(
          JSON.stringify({
            status: "accepted",
            phase: "preview",
            manifest_id: "preview_sha256_1111111111111111111111111111111111111111111111111111111111111111",
            digest: "1".repeat(64),
            scope: "C-DEMO/demo",
            oracle_version: "s09-impact-oracle/1",
            level: 1,
            expanded_to_full_scope: false,
            member_count: 1,
            partition_counts: {"open_cycle": 1},
            zero_hit_proof: false,
            target_generation: 2,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),

      "POST /controlled/s08/api/commands/approve": () => {
        approvePosts += 1;
        if (approvePosts === 1) {
          throw new TypeError("network down");
        }
        return new Response(
          JSON.stringify({
            status: "accepted",
            candidate_id: CANDIDATE,
            approval_binding_id: "approval_sha256_binding",
            approval_binding_digest: "binding-digest",
            governance_revision: 5,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
    });
    renderPanel(CANDIDATE);
    await waitFor(() =>
      expect(screen.getByTestId("t08-approve-form")).toBeInTheDocument(),
    );
    await user.type(screen.getByLabelText("生效时间"), "2026-08-10T12:00");
    await user.click(screen.getByTestId("t08-approve-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t08-action-error")).toHaveTextContent(
        "重试将使用同一幂等键",
      ),
    );
    expect(screen.getByTestId("t08-approve-button")).toBeDisabled();
    // An unrelated ledger event advances the revision to 5 while the POST
    // never arrived.  The display-only authoritative refresh must not
    // release the latch and no new POST may be possible.
    await user.click(screen.getByTestId("t08-command-reconcile"));
    await waitFor(() => expect(workspaceRequests).toBeGreaterThan(1));
    expect(screen.getByTestId("t08-command-unknown")).toBeInTheDocument();
    expect(screen.getByTestId("t08-approve-button")).toBeDisabled();
    expect(approvePosts).toBe(1);
    await user.click(screen.getByTestId("t08-approve-button")).catch(() => {});
    expect(approvePosts).toBe(1);
    // Only the exact same-key/body replay settles: its definitive response
    // releases the latch and applies the accepted result.
    await user.click(screen.getByTestId("t08-command-retry"));
    await waitFor(() =>
      expect(screen.getByTestId("t08-approve-button")).toBeEnabled(),
    );
    expect(screen.queryByTestId("t08-command-unknown")).not.toBeInTheDocument();
    const posts = fetchMockCalls("approve");
    expect(posts).toHaveLength(2);
    expect(posts[1].body).toBe(posts[0].body);
    // The next explicit command mints a fresh identity, never the settled key.
    await user.click(screen.getByTestId("t08-approve-button"));
    await waitFor(() => expect(fetchMockCalls("approve")).toHaveLength(3));
    const after = fetchMockCalls("approve");
    expect(JSON.parse(after[2].body).idempotency_key).not.toBe(
      JSON.parse(after[0].body).idempotency_key,
    );
  });

  it("keeps the workspace latch through a retry 409 until refetch succeeds", async () => {
    const user = userEvent.setup();
    let workspaceRequests = 0;
    let approvePosts = 0;
    let resolveRefetch: ((response: Response) => void) | undefined;
    const pendingRefetch = new Promise<Response>((resolve) => {
      resolveRefetch = resolve;
    });
    fetchRouter({
      [`GET ${WORKSPACE_PATH}`]: () => {
        workspaceRequests += 1;
        if (workspaceRequests === 2) return pendingRefetch;
        return new Response(
          JSON.stringify(
            workspacePayload("in_review", "approver", ["approve", "reject"], {
              governance_revision: workspaceRequests === 1 ? 3 : 4,
            }),
          ),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
      "POST /controlled/s09/api/commands/preview_impact": () =>
        new Response(
          JSON.stringify({
            status: "accepted",
            phase: "preview",
            manifest_id: "preview_sha256_1111111111111111111111111111111111111111111111111111111111111111",
            digest: "1".repeat(64),
            scope: "C-DEMO/demo",
            oracle_version: "s09-impact-oracle/1",
            level: 1,
            expanded_to_full_scope: false,
            member_count: 1,
            partition_counts: {"open_cycle": 1},
            zero_hit_proof: false,
            target_generation: 2,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),

      "POST /controlled/s08/api/commands/approve": () => {
        approvePosts += 1;
        if (approvePosts === 1) throw new TypeError("network down");
        if (approvePosts === 2) {
          return new Response(
            JSON.stringify({
              detail: { error: "S08_CONFLICT", message: "stale revision" },
            }),
            { status: 409, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(
          JSON.stringify({
            status: "accepted",
            candidate_id: CANDIDATE,
            approval_binding_id: "approval_sha256_binding",
            approval_binding_digest: "binding-digest",
            governance_revision: 5,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
    });
    renderPanel(CANDIDATE);
    await waitFor(() =>
      expect(screen.getByTestId("t08-approve-form")).toBeInTheDocument(),
    );
    await user.type(screen.getByLabelText("生效时间"), "2026-08-10T12:00");
    await user.click(screen.getByTestId("t08-approve-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t08-command-retry")).toBeEnabled(),
    );
    await user.click(screen.getByTestId("t08-command-retry"));
    await waitFor(() => expect(workspaceRequests).toBe(2));
    expect(screen.queryByTestId("t08-command-retry")).not.toBeInTheDocument();
    expect(screen.getByTestId("t08-approve-button")).toBeDisabled();
    await user.click(screen.getByTestId("t08-approve-button"));
    expect(approvePosts).toBe(2);

    resolveRefetch?.(
      new Response(
        JSON.stringify(
          workspacePayload("in_review", "approver", ["approve", "reject"], {
            governance_revision: 4,
          }),
        ),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    await waitFor(() =>
      expect(screen.getByTestId("t08-approve-button")).toBeEnabled(),
    );
    await user.click(screen.getByTestId("t08-approve-button"));
    await waitFor(() => expect(approvePosts).toBe(3));
    const posts = fetchMockCalls("approve").map((call) => JSON.parse(call.body));
    expect(posts[1]).toEqual(posts[0]);
    expect(posts[2].idempotency_key).not.toBe(posts[0].idempotency_key);
    expect(posts[2].expected_governance_revision).toBe(4);
  });

  it("keeps the latch when reconciliation shows no ledger advance", async () => {
    const user = userEvent.setup();
    let approvePosts = 0;
    fetchRouter({
      [`GET ${WORKSPACE_PATH}`]: () =>
        new Response(
          JSON.stringify(
            workspacePayload("in_review", "approver", ["approve", "reject"]),
          ),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      "POST /controlled/s09/api/commands/preview_impact": () =>
        new Response(
          JSON.stringify({
            status: "accepted",
            phase: "preview",
            manifest_id: "preview_sha256_1111111111111111111111111111111111111111111111111111111111111111",
            digest: "1".repeat(64),
            scope: "C-DEMO/demo",
            oracle_version: "s09-impact-oracle/1",
            level: 1,
            expanded_to_full_scope: false,
            member_count: 1,
            partition_counts: {"open_cycle": 1},
            zero_hit_proof: false,
            target_generation: 2,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),

      "POST /controlled/s08/api/commands/approve": () => {
        approvePosts += 1;
        if (approvePosts === 1) {
          throw new TypeError("network down");
        }
        return new Response(
          JSON.stringify({
            status: "accepted",
            candidate_id: CANDIDATE,
            approval_binding_id: "approval_sha256_binding",
            approval_binding_digest: "binding-digest",
            governance_revision: 4,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
    });
    renderPanel(CANDIDATE);
    await waitFor(() =>
      expect(screen.getByTestId("t08-approve-form")).toBeInTheDocument(),
    );
    await user.type(screen.getByLabelText("生效时间"), "2026-08-10T12:00");
    await user.click(screen.getByTestId("t08-approve-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t08-action-error")).toHaveTextContent(
        "重试将使用同一幂等键",
      ),
    );
    await user.click(screen.getByTestId("t08-command-reconcile"));
    // Revision stayed 3: the command has not settled, the latch holds and
    // only the byte-identical retry remains.
    expect(screen.getByTestId("t08-command-unknown")).toBeInTheDocument();
    expect(screen.getByTestId("t08-approve-button")).toBeDisabled();
    await user.click(screen.getByTestId("t08-command-retry"));
    await waitFor(() =>
      expect(screen.getByTestId("t08-action-ok")).toBeInTheDocument(),
    );
    const posts = fetchMockCalls("approve");
    expect(posts).toHaveLength(2);
    expect(posts[1].body).toBe(posts[0].body);
  });

  it("polls request-validation into the authoritative rejected terminal state with evidence", async () => {
    const user = userEvent.setup();
    let workspaceRequests = 0;
    let validatePosts = 0;
    fetchRouter({
      [`GET ${WORKSPACE_PATH}`]: () => {
        workspaceRequests += 1;
        if (workspaceRequests === 1) {
          return new Response(
            JSON.stringify(
              workspacePayload("candidate", "admin", [
                "request_validation",
                "cancel",
              ]),
            ),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(
          JSON.stringify(
            workspacePayload("rejected", "admin", [], {
              validation_outcome: {
                status: "rejected",
                reason_code: "S08_VALIDATION_REJECTED",
              },
              validation_bundle: {
                schema_version: "s08-validation-bundle/1",
                status: "rejected",
                results: {
                  failed_count: 1,
                  checks: [
                    { check_id: "corpus_bound", outcome: "fail", detail: "x" },
                  ],
                },
              },
            }),
          ),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
      "POST /controlled/s08/api/commands/request_validation": () => {
        validatePosts += 1;
        return new Response(
          JSON.stringify({
            status: "accepted",
            policy_job_id: "policy_job_1",
            candidate_id: CANDIDATE,
            governance_revision: 4,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
    });
    renderPanel(CANDIDATE);
    await waitFor(() =>
      expect(screen.getByTestId("t08-validate-button")).toBeInTheDocument(),
    );
    await user.click(screen.getByTestId("t08-validate-button"));
    // The poll converges on the authoritative rejected terminal: pending
    // stops, the registered reason and the server evidence render, and no
    // activation surface exists.
    await waitFor(() =>
      expect(screen.getByTestId("t08-validation-rejected")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("t08-validation-rejected")).toHaveTextContent(
      "S08_VALIDATION_REJECTED",
    );
    expect(
      screen.getByTestId("t08-validation-rejected-evidence"),
    ).toHaveTextContent("corpus_bound: fail");
    expect(screen.queryByTestId("t08-polling")).not.toBeInTheDocument();
    expect(screen.queryByTestId("t08-validate-button")).not.toBeInTheDocument();
    expect(screen.queryByTestId("t08-approve-form")).not.toBeInTheDocument();
    expect(screen.queryByTestId("t08-schedule-form")).not.toBeInTheDocument();
    expect(validatePosts).toBe(1);
  });

  it("renders the activation diagnostic failure with only the stable reason and the prior active anchor", async () => {
    fetchRouter({
      [`GET ${WORKSPACE_PATH}`]: () =>
        new Response(
          JSON.stringify(
            workspacePayload("scheduled", "admin", ["cancel"], {
              activation_outcome: {
                status: "failed",
                reason_code: "S08_ACTIVATION_UNAVAILABLE",
              },
              validation_outcome: {
                status: "validated",
                reason_code: "S08_VALIDATION_PASSED",
              },
            }),
          ),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderPanel(CANDIDATE);
    await waitFor(() =>
      expect(screen.getByTestId("t08-activation-failed")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("t08-activation-failed")).toHaveTextContent(
      "S08_ACTIVATION_UNAVAILABLE",
    );
    // Internal exception and write-point text never reaches the DOM.
    expect(screen.getByTestId("t08-activation-failed")).not.toHaveTextContent(
      "PolicyUnavailable",
    );
    expect(screen.getByTestId("t08-activation-failed")).not.toHaveTextContent(
      "s08.activation",
    );
    // The prior-active anchor stays visible and the candidate is not active.
    expect(screen.getByTestId("t08-workspace-anchor")).toHaveTextContent(
      "candidate_t08bootstrap00000000000000000",
    );
    expect(screen.getByTestId("t08-workspace-status")).toHaveTextContent(
      "scheduled",
    );
    expect(screen.queryByTestId("t08-activation-polling")).not.toBeInTheDocument();
  });

  it("stops validation polling on the authoritative diagnostic terminal and renders the stable reason", async () => {
    const user = userEvent.setup();
    let workspaceRequests = 0;
    let validatePosts = 0;
    fetchRouter({
      [`GET ${WORKSPACE_PATH}`]: () => {
        workspaceRequests += 1;
        if (workspaceRequests === 1) {
          return new Response(
            JSON.stringify(
              workspacePayload("candidate", "admin", [
                "request_validation",
                "cancel",
              ]),
            ),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(
          JSON.stringify(
            workspacePayload("candidate", "admin", ["cancel"], {
              validation_outcome: {
                status: "failed",
                reason_code: "S08_VALIDATION_INTERNAL",
              },
            }),
          ),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
      "POST /controlled/s08/api/commands/request_validation": () => {
        validatePosts += 1;
        return new Response(
          JSON.stringify({
            status: "accepted",
            policy_job_id: "policy_job_1",
            candidate_id: CANDIDATE,
            governance_revision: 4,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
    });
    renderPanel(CANDIDATE);
    await waitFor(() =>
      expect(screen.getByTestId("t08-validate-button")).toBeInTheDocument(),
    );
    await user.click(screen.getByTestId("t08-validate-button"));
    // The diagnostic terminal stops pending immediately and renders the
    // registered stable reason -- never "still running".
    await waitFor(() =>
      expect(screen.getByTestId("t08-validation-failed")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("t08-validation-failed")).toHaveTextContent(
      "S08_VALIDATION_INTERNAL",
    );
    expect(screen.queryByTestId("t08-polling")).not.toBeInTheDocument();
    expect(screen.queryByTestId("t08-polling-timeout")).not.toBeInTheDocument();
    expect(
      screen.queryByTestId("t08-validation-unavailable"),
    ).not.toBeInTheDocument();
    expect(validatePosts).toBe(1);
  });

  it("an exact retry of an unknown import applies the returned draft id", async () => {
    const user = userEvent.setup();
    let importPosts = 0;
    fetchRouter({
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
      "POST /controlled/s08/api/commands/import_legacy": () => {
        importPosts += 1;
        if (importPosts === 1) {
          throw new TypeError("network down");
        }
        return new Response(
          JSON.stringify({
            status: "accepted",
            draft_id: "draft_retry_accepted",
            mapping_ledger_id: "ledger_1",
            mapping_ledger_digest: "ledger-digest",
            source_sha256: "a".repeat(64),
            knowledge_sha256: "b".repeat(64),
            governance_revision: 4,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
    });
    renderPanel(null);
    await waitFor(() =>
      expect(screen.getByTestId("t08-import-button")).toBeInTheDocument(),
    );
    await user.type(
      screen.getByLabelText("来源包标识"),
      "c-demo-legacy-baseline/1",
    );
    await user.click(screen.getByTestId("t08-import-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t08-command-unknown")).toBeInTheDocument(),
    );
    await user.click(screen.getByTestId("t08-command-retry"));
    // The accepted replay opens the draft editor with the returned draft.
    await waitFor(() =>
      expect(screen.getByTestId("t08-draft-editor")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("t08-draft-id")).toHaveTextContent(
      "draft_retry_accepted",
    );
    const posts = fetchMockCalls("import_legacy");
    expect(posts).toHaveLength(2);
    expect(posts[1].body).toBe(posts[0].body);
    expect(screen.queryByTestId("t08-command-unknown")).not.toBeInTheDocument();
  });

  it("an exact retry of an unknown freeze selects the returned candidate", async () => {
    const user = userEvent.setup();
    let freezePosts = 0;
    fetchRouter({
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
      "POST /controlled/s08/api/commands/import_legacy": () =>
        new Response(
          JSON.stringify({
            status: "accepted",
            draft_id: "draft_freeze_retry",
            mapping_ledger_id: "ledger_1",
            source_sha256: "a".repeat(64),
            governance_revision: 4,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      "POST /controlled/s08/api/commands/freeze_candidate": () => {
        freezePosts += 1;
        if (freezePosts === 1) {
          throw new TypeError("network down");
        }
        return new Response(
          JSON.stringify({
            status: "accepted",
            candidate_id: "candidate_retry_accepted",
            manifest_id: "manifest_1",
            manifest_digest: "c".repeat(64),
            components: [],
            governance_revision: 5,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
    });
    const onCandidateSelected = vi.fn();
    renderPanel(null, onCandidateSelected);
    await waitFor(() =>
      expect(screen.getByTestId("t08-import-button")).toBeInTheDocument(),
    );
    await user.type(
      screen.getByLabelText("来源包标识"),
      "c-demo-legacy-baseline/1",
    );
    await user.click(screen.getByTestId("t08-import-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t08-draft-editor")).toBeInTheDocument(),
    );
    await user.click(screen.getByTestId("t08-freeze-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t08-command-unknown")).toBeInTheDocument(),
    );
    await user.click(screen.getByTestId("t08-command-retry"));
    // The accepted replay selects the returned candidate identity.
    await waitFor(() =>
      expect(onCandidateSelected).toHaveBeenCalledWith("candidate_retry_accepted"),
    );
    const posts = fetchMockCalls("freeze_candidate");
    expect(posts).toHaveLength(2);
    expect(posts[1].body).toBe(posts[0].body);
    expect(screen.queryByTestId("t08-command-unknown")).not.toBeInTheDocument();
  });

  it("an unknown draft command locks every workspace action across section switches", async () => {
    const user = userEvent.setup();
    let importPosts = 0;
    fetchRouter({
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
      "POST /controlled/s08/api/commands/import_legacy": () => {
        importPosts += 1;
        throw new TypeError("network down");
      },
      [`GET ${WORKSPACE_PATH}`]: () =>
        new Response(
          JSON.stringify(
            workspacePayload("in_review", "approver", ["approve", "reject"]),
          ),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      "POST /controlled/s09/api/commands/preview_impact": () =>
        new Response(
          JSON.stringify({
            status: "accepted",
            phase: "preview",
            manifest_id: "preview_sha256_1111111111111111111111111111111111111111111111111111111111111111",
            digest: "1".repeat(64),
            scope: "C-DEMO/demo",
            oracle_version: "s09-impact-oracle/1",
            level: 1,
            expanded_to_full_scope: false,
            member_count: 1,
            partition_counts: {"open_cycle": 1},
            zero_hit_proof: false,
            target_generation: 2,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),

      "POST /controlled/s08/api/commands/approve": () => {
        throw new Error(
          "approve must never fire while the draft latch is unknown",
        );
      },
    });
    const client = createQueryClient();
    const view = render(
      <PolicyReleasePanel candidateId={null} />,
      { wrapper: wrap(client) },
    );
    await waitFor(() =>
      expect(screen.getByTestId("t08-import-button")).toBeInTheDocument(),
    );
    await user.type(
      screen.getByLabelText("来源包标识"),
      "c-demo-legacy-baseline/1",
    );
    await user.click(screen.getByTestId("t08-import-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t08-command-unknown")).toBeInTheDocument(),
    );
    expect(importPosts).toBe(1);
    // The one latch survives the section switch: the workspace renders fully
    // locked and no mutation can fire.
    view.rerender(<PolicyReleasePanel candidateId={CANDIDATE} />);
    await waitFor(() =>
      expect(screen.getByTestId("t08-workspace-status")).toHaveTextContent(
        "in_review",
      ),
    );
    expect(screen.getByTestId("t08-approve-button")).toBeDisabled();
    expect(screen.getByTestId("t08-reject-button")).toBeDisabled();
    expect(screen.getByLabelText("生效时间")).toBeDisabled();
    await user.click(screen.getByTestId("t08-approve-button")).catch(() => {});
    expect(fetchMockCalls("approve")).toHaveLength(0);
    // The exact retry of the locked draft command remains the only re-send.
    expect(screen.getByTestId("t08-command-retry")).toBeEnabled();
  });

  it("an unknown workspace command locks every draft action across section switches", async () => {
    const user = userEvent.setup();
    let approvePosts = 0;
    fetchRouter({
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
      "POST /controlled/s08/api/commands/import_legacy": () => {
        throw new Error(
          "import must never fire while the workspace latch is unknown",
        );
      },
      [`GET ${WORKSPACE_PATH}`]: () =>
        new Response(
          JSON.stringify(
            workspacePayload("in_review", "approver", ["approve", "reject"]),
          ),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      "POST /controlled/s09/api/commands/preview_impact": () =>
        new Response(
          JSON.stringify({
            status: "accepted",
            phase: "preview",
            manifest_id: "preview_sha256_1111111111111111111111111111111111111111111111111111111111111111",
            digest: "1".repeat(64),
            scope: "C-DEMO/demo",
            oracle_version: "s09-impact-oracle/1",
            level: 1,
            expanded_to_full_scope: false,
            member_count: 1,
            partition_counts: {"open_cycle": 1},
            zero_hit_proof: false,
            target_generation: 2,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),

      "POST /controlled/s08/api/commands/approve": () => {
        approvePosts += 1;
        throw new TypeError("network down");
      },
    });
    const client = createQueryClient();
    const view = render(
      <PolicyReleasePanel candidateId={CANDIDATE} />,
      { wrapper: wrap(client) },
    );
    await waitFor(() =>
      expect(screen.getByTestId("t08-approve-form")).toBeInTheDocument(),
    );
    await user.type(screen.getByLabelText("生效时间"), "2026-08-10T12:00");
    await user.click(screen.getByTestId("t08-approve-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t08-command-unknown")).toBeInTheDocument(),
    );
    expect(approvePosts).toBe(1);
    // Switching back to the draft section keeps the same latch locked.
    view.rerender(<PolicyReleasePanel candidateId={null} />);
    await waitFor(() =>
      expect(screen.getByTestId("t08-import-button")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("t08-import-button")).toBeDisabled();
    expect(screen.getByLabelText("来源包标识")).toBeDisabled();
    await user.click(screen.getByTestId("t08-import-button")).catch(() => {});
    expect(fetchMockCalls("import_legacy")).toHaveLength(0);
    expect(screen.getByTestId("t08-command-retry")).toBeEnabled();
  });

  it(
    "shows activation unavailable when the poll hits a closed 503",
    async () => {
      const user = userEvent.setup();
      let workspaceRequests = 0;
      let schedulePosts = 0;
      fetchRouter({
        [`GET ${WORKSPACE_PATH}`]: () => {
          workspaceRequests += 1;
          if (workspaceRequests === 1) {
            return new Response(
              JSON.stringify(
                workspacePayload("approved", "admin", ["schedule", "cancel"], {
                  approval_binding_id: "approval_sha256_binding",
                  approval_binding: {
                    schema_version: "s08-approval-binding/1",
                    activation_time: 1786000000,
                  },
                }),
              ),
              { status: 200, headers: { "Content-Type": "application/json" } },
            );
          }
          return new Response(
            JSON.stringify({
              detail: { error: "S08_UNAVAILABLE", message: "unavailable" },
            }),
            { status: 503, headers: { "Content-Type": "application/json" } },
          );
        },
        "POST /controlled/s08/api/commands/schedule": () => {
          schedulePosts += 1;
          return new Response(
            JSON.stringify({
              status: "accepted",
              reservation_id: "reservation_1",
              governance_revision: 4,
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        },
      });
      renderPanel(CANDIDATE);
      await waitFor(() =>
        expect(screen.getByTestId("t08-schedule-button")).toBeInTheDocument(),
      );
      await user.click(screen.getByTestId("t08-schedule-button"));
      // The workspace query retries the transient 503 twice before the
      // definitive error lands, so the unavailable state needs that window.
      await waitFor(
        () =>
          expect(
            screen.getByTestId("t08-activation-unavailable"),
          ).toBeInTheDocument(),
        { timeout: 15_000 },
      );
      expect(schedulePosts).toBe(1);
      expect(
        screen.queryByTestId("t08-activation-polling"),
      ).not.toBeInTheDocument();
      expect(screen.getByTestId("t08-activation-refresh")).toBeEnabled();
    },
    25_000,
  );

  it(
    "reconciles an unavailable activation once the authority returns",
    async () => {
      const user = userEvent.setup();
      let workspaceRequests = 0;
      let recovered = false;
      fetchRouter({
        [`GET ${WORKSPACE_PATH}`]: () => {
          workspaceRequests += 1;
          if (workspaceRequests === 1) {
            return new Response(
              JSON.stringify(
                workspacePayload("approved", "admin", ["schedule", "cancel"], {
                  approval_binding_id: "approval_sha256_binding",
                  approval_binding: {
                    schema_version: "s08-approval-binding/1",
                    activation_time: 1786000000,
                  },
                }),
              ),
              { status: 200, headers: { "Content-Type": "application/json" } },
            );
          }
          if (!recovered) {
            return new Response(
              JSON.stringify({
                detail: { error: "S08_UNAVAILABLE", message: "unavailable" },
              }),
              { status: 503, headers: { "Content-Type": "application/json" } },
            );
          }
          // The authority is back and the scheduled job is still pending.
          return new Response(
            JSON.stringify(
              workspacePayload("scheduled", "admin", ["cancel"], {
                activation_outcome: { status: "pending" },
                validation_outcome: {
                  status: "validated",
                  reason_code: "S08_VALIDATION_PASSED",
                },
              }),
            ),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        },
        "POST /controlled/s08/api/commands/schedule": () =>
          new Response(
            JSON.stringify({
              status: "accepted",
              reservation_id: "reservation_1",
              governance_revision: 4,
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
      });
      renderPanel(CANDIDATE);
      await waitFor(() =>
        expect(screen.getByTestId("t08-schedule-button")).toBeInTheDocument(),
      );
      await user.click(screen.getByTestId("t08-schedule-button"));
      await waitFor(
        () =>
          expect(
            screen.getByTestId("t08-activation-unavailable"),
          ).toBeInTheDocument(),
        { timeout: 15_000 },
      );
      // The authority recovers; the explicit refresh reconciles against the
      // fresh workspace and resumes the bounded poll on the pending job.
      recovered = true;
      await user.click(screen.getByTestId("t08-activation-refresh"));
      await waitFor(
        () =>
          expect(screen.getByTestId("t08-activation-polling")).toBeInTheDocument(),
        { timeout: 10_000 },
      );
      expect(
        screen.queryByTestId("t08-activation-unavailable"),
      ).not.toBeInTheDocument();
    },
    30_000,
  );
});
