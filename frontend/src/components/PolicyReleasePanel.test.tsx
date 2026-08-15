import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import PolicyReleasePanel, {
  GovernanceWorkspacePanel,
} from "./PolicyReleasePanel";
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
            governance_revision: 4,
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
    await explicitPreview(user);
    await user.click(screen.getByTestId("t08-approve-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t08-action-ok")).toBeInTheDocument(),
    );

    const posts = router.calls.filter((call) => call.method === "POST");
    // The approval first previews the immutable impact, then binds its
    // exact manifest identity and the preview's returned revision.
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
    // The approval fences on the revision the preview itself returned (the
    // preview appends one immutable fact), never the pre-preview workspace
    // revision.
    expect(body.expected_governance_revision).toBe(4);
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
            governance_revision: 5,
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
    await explicitPreview(user);
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
      expect(screen.getByTestId("t08-preview-button")).toBeEnabled(),
    );
    await explicitPreview(user);
    await user.click(screen.getByTestId("t08-approve-button"));
    await waitFor(() => expect(approvePosts).toBe(2));
    const posts = fetchMockCalls("approve").map((call) => JSON.parse(call.body));
    expect(posts[1].idempotency_key).not.toBe(posts[0].idempotency_key);
    // The stale preview was discarded on the conflict; the fresh preview
    // after the refetch fences the new approval on its returned revision.
    expect(posts[1].expected_governance_revision).toBe(5);
  });

  it("approval stays disabled until an accepted preview is visibly rendered and no approval POST can precede it", async () => {
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
            governance_revision: 4,
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
      expect(screen.getByTestId("t08-approve-form")).toBeInTheDocument(),
    );
    // Approval is fenced before any accepted preview DTO is rendered: no
    // approval POST can ever precede an explicit preview (P-1).
    expect(screen.getByTestId("t08-approve-button")).toBeDisabled();
    await user.click(screen.getByTestId("t08-approve-button"));
    expect(approvePosts).toBe(0);
    expect(router.calls.filter((call) => call.method === "POST")).toHaveLength(
      0,
    );
    // The explicit preview renders the server DTO with live status
    // semantics (S-4) and unlocks approval.
    await user.click(screen.getByTestId("t08-preview-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t08-preview")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("t08-preview")).toHaveAttribute("role", "status");
    await user.type(screen.getByLabelText("生效时间"), "2026-08-10T12:00");
    expect(screen.getByTestId("t08-approve-button")).toBeEnabled();
    await user.click(screen.getByTestId("t08-approve-button"));
    await waitFor(() => expect(approvePosts).toBe(1));
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

/** The explicit preview step every approval requires (P-1): approval stays
 * disabled until an accepted preview DTO is visibly rendered, so tests that
 * approve must first run the independent preview button. */
async function explicitPreview(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByTestId("t08-preview-button"));
  await waitFor(() =>
    expect(screen.getByTestId("t08-preview")).toBeInTheDocument(),
  );
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
    await explicitPreview(user);
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
    await explicitPreview(user);
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
      await explicitPreview(user);
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
    await explicitPreview(user);
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
    await explicitPreview(user);
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
    await explicitPreview(user);
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
      expect(screen.queryByTestId("t08-command-unknown")).not.toBeInTheDocument(),
    );
    await waitFor(() =>
      expect(screen.getByTestId("t08-preview-button")).toBeEnabled(),
    );
    const posts = fetchMockCalls("approve");
    expect(posts).toHaveLength(2);
    expect(posts[1].body).toBe(posts[0].body);
    // The next explicit command mints a fresh identity, never the settled key.
    await explicitPreview(user);
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
    await explicitPreview(user);
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
      expect(screen.getByTestId("t08-preview-button")).toBeEnabled(),
    );
    await explicitPreview(user);
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
    await explicitPreview(user);
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
    await explicitPreview(user);
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

  function previewPayload(overrides: Record<string, unknown> = {}) {
    return {
      status: "accepted",
      phase: "preview",
      manifest_id:
        "preview_sha256_2222222222222222222222222222222222222222222222222222222222222222",
      digest: "2".repeat(64),
      scope: "C-DEMO/demo",
      oracle_version: "s09-impact-oracle/1",
      level: 1,
      expanded_to_full_scope: false,
      member_count: 1,
      partition_counts: { open_cycle: 1 },
      zero_hit_proof: false,
      target_generation: 2,
      governance_revision: 5,
      ...overrides,
    };
  }

  it("approver previews impact explicitly, renders the full server DTO and approval binds the exact preview revision", async () => {
    const user = userEvent.setup();
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
          JSON.stringify(
            previewPayload({
              expanded_to_full_scope: true,
              member_count: 3,
              partition_counts: { open_cycle: 2, verification_completed: 1 },
            }),
          ),
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
            governance_revision: 6,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderPanel(CANDIDATE);
    await waitFor(() =>
      expect(screen.getByTestId("t08-approve-form")).toBeInTheDocument(),
    );

    // The independent explicit preview button renders the complete server
    // DTO and sends no approval.
    await user.click(screen.getByTestId("t08-preview-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t08-preview")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("t08-preview-manifest")).toHaveTextContent(
      "preview_sha256_2222222222222222222222222222222222222222222222222222222222222222",
    );
    expect(screen.getByTestId("t08-preview-members")).toHaveTextContent("3");
    expect(screen.getByTestId("t08-preview-expansion")).toHaveTextContent(
      "已扩张到完整范围",
    );
    expect(screen.getByTestId("t08-preview-generation")).toHaveTextContent("2");
    let previewPosts = router.calls.filter(
      (call) =>
        call.method === "POST" && call.url.includes("preview_impact"),
    );
    expect(previewPosts).toHaveLength(1);
    expect(
      router.calls.filter(
        (call) => call.method === "POST" && call.url.endsWith("/approve"),
      ),
    ).toHaveLength(0);

    // The approval reuses the exact previewed manifest and fences on the
    // revision the preview returned; zero duplicate preview POSTs.
    await user.type(screen.getByLabelText("生效时间"), "2026-08-10T12:00");
    await user.click(screen.getByTestId("t08-approve-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t08-action-ok")).toBeInTheDocument(),
    );
    previewPosts = router.calls.filter(
      (call) =>
        call.method === "POST" && call.url.includes("preview_impact"),
    );
    expect(previewPosts).toHaveLength(1);
    const approveBodies = router.calls.filter(
      (call) =>
        call.method === "POST" && call.url.endsWith("/approve"),
    );
    expect(approveBodies).toHaveLength(1);
    const approveBody = approveBodies[0].body as Record<string, unknown>;
    expect(approveBody.preview_manifest_id).toBe(
      "preview_sha256_2222222222222222222222222222222222222222222222222222222222222222",
    );
    expect(approveBody.expected_governance_revision).toBe(5);
    const previewKey = (
      previewPosts[0].body as Record<string, unknown>
    ).idempotency_key;
    expect(approveBody.idempotency_key).not.toBe(previewKey);
  });

  it("a 409 on the preview surfaces the conflict, refetches and requires a fresh preview", async () => {
    const user = userEvent.setup();
    let previewPosts = 0;
    let workspaceRequests = 0;
    let resolveRefetch: ((response: Response) => void) | undefined;
    const pendingRefetch = new Promise<Response>((resolve) => {
      resolveRefetch = resolve;
    });
    const router = fetchRouter({
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
      "POST /controlled/s09/api/commands/preview_impact": () => {
        previewPosts += 1;
        if (previewPosts === 1) {
          return new Response(
            JSON.stringify({
              detail: { error: "S08_CONFLICT", message: "stale governance revision" },
            }),
            { status: 409, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(JSON.stringify(previewPayload()), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      },
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
            governance_revision: 6,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderPanel(CANDIDATE);
    await waitFor(() =>
      expect(screen.getByTestId("t08-approve-form")).toBeInTheDocument(),
    );
    await user.type(screen.getByLabelText("生效时间"), "2026-08-10T12:00");
    await user.click(screen.getByTestId("t08-preview-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t08-conflict")).toHaveTextContent(
        "stale governance revision",
      ),
    );
    // The stale preview was never approved: no approval POST and the
    // latched surface stays locked until the authoritative refetch lands.
    expect(
      router.calls.filter(
        (call) => call.method === "POST" && call.url.endsWith("/approve"),
      ),
    ).toHaveLength(0);
    expect(screen.getByTestId("t08-approve-button")).toBeDisabled();
    expect(previewPosts).toBe(1);
    await user.click(screen.getByTestId("t08-approve-button"));
    expect(previewPosts).toBe(1);

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
      expect(screen.getByTestId("t08-preview-button")).toBeEnabled(),
    );
    // A fresh explicit preview is required before the next approval and
    // binds the new preview revision.
    await explicitPreview(user);
    await user.click(screen.getByTestId("t08-approve-button"));
    await waitFor(() => expect(previewPosts).toBe(2));
    await waitFor(() =>
      expect(screen.getByTestId("t08-action-ok")).toBeInTheDocument(),
    );
    const approveBodies = router.calls.filter(
      (call) =>
        call.method === "POST" && call.url.endsWith("/approve"),
    );
    expect(approveBodies).toHaveLength(1);
    expect(
      (approveBodies[0].body as Record<string, unknown>)
        .expected_governance_revision,
    ).toBe(5);
  });

  it("renders an explicit forbidden state for the preview with zero approval POSTs", async () => {
    const user = userEvent.setup();
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
            detail: { error: "S08_FORBIDDEN", message: "identity required" },
          }),
          { status: 403, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderPanel(CANDIDATE);
    await waitFor(() =>
      expect(screen.getByTestId("t08-approve-form")).toBeInTheDocument(),
    );
    await user.type(screen.getByLabelText("生效时间"), "2026-08-10T12:00");
    await user.click(screen.getByTestId("t08-preview-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t08-action-error")).toHaveTextContent(
        "无权限执行此操作",
      ),
    );
    expect(
      router.calls.filter(
        (call) => call.method === "POST" && call.url.endsWith("/approve"),
      ),
    ).toHaveLength(0);
  });

  it("renders an explicit unavailable state for the preview with zero approval POSTs", async () => {
    const user = userEvent.setup();
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
            detail: {
              error: "S08_UNAVAILABLE",
              message: "Governance authority is unavailable",
            },
          }),
          { status: 503, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderPanel(CANDIDATE);
    await waitFor(() =>
      expect(screen.getByTestId("t08-approve-form")).toBeInTheDocument(),
    );
    await user.type(screen.getByLabelText("生效时间"), "2026-08-10T12:00");
    await user.click(screen.getByTestId("t08-preview-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t08-action-error")).toHaveTextContent(
        "治理服务暂不可用",
      ),
    );
    expect(
      router.calls.filter(
        (call) => call.method === "POST" && call.url.endsWith("/approve"),
      ),
    ).toHaveLength(0);
  });

  it("retains the same idempotency key for an unknown preview result and replays byte-identical bytes", async () => {
    const user = userEvent.setup();
    let previewAttempts = 0;
    const router = fetchRouter({
      [`GET ${WORKSPACE_PATH}`]: () =>
        new Response(
          JSON.stringify(
            workspacePayload("in_review", "approver", ["approve", "reject"]),
          ),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      "POST /controlled/s09/api/commands/preview_impact": () => {
        previewAttempts += 1;
        if (previewAttempts === 1) {
          return Promise.reject(new TypeError("network down"));
        }
        return new Response(JSON.stringify(previewPayload()), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      },
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
            governance_revision: 6,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderPanel(CANDIDATE);
    await waitFor(() =>
      expect(screen.getByTestId("t08-approve-form")).toBeInTheDocument(),
    );
    await user.type(screen.getByLabelText("生效时间"), "2026-08-10T12:00");
    await user.click(screen.getByTestId("t08-preview-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t08-command-unknown")).toBeInTheDocument(),
    );
    const previewCalls = () =>
      router.calls.filter(
        (call) =>
          call.method === "POST" && call.url.includes("preview_impact"),
      );
    expect(previewCalls()).toHaveLength(1);

    // The exact retry replays the same key and identical serialized bytes.
    await user.click(screen.getByTestId("t08-command-retry"));
    await waitFor(() => expect(previewCalls()).toHaveLength(2));
    const [first, second] = previewCalls().map((call) =>
      JSON.stringify(call.body),
    );
    expect(second).toBe(first);
    const previewKeys = previewCalls().map(
      (call) => (call.body as Record<string, unknown>).idempotency_key,
    );
    expect(new Set(previewKeys).size).toBe(1);

    // The accepted preview still requires the explicit approval click;
    // the approval binds the exact preview with a fresh key.
    await waitFor(() =>
      expect(screen.getByTestId("t08-preview")).toBeInTheDocument(),
    );
    await user.click(screen.getByTestId("t08-approve-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t08-action-ok")).toBeInTheDocument(),
    );
    const approveBodies = router.calls.filter(
      (call) =>
        call.method === "POST" && call.url.endsWith("/approve"),
    );
    expect(approveBodies).toHaveLength(1);
    const approveBody = approveBodies[0].body as Record<string, unknown>;
    expect(approveBody.preview_manifest_id).toBe(
      "preview_sha256_2222222222222222222222222222222222222222222222222222222222222222",
    );
    expect(approveBody.expected_governance_revision).toBe(5);
  });
});

describe("GovernanceWorkspacePanel T09", () => {
  const HOLD = {
    hold_id: "governance_hold0000000000000000001",
    event_id: "governance_event0000000000000000001",
    reason_code: "S09_TEST_HOLD",
    scope: "C-DEMO/demo",
    hold_scope: "open_cycle",
    imposed_by: "c-demo-policy-operator",
    imposed_at: 1786000000,
    authority_revision: 3,
    evidence_digest: null,
    recovery_criterion_id: "s09-hold-recovery-criterion/1",
    recovery_criterion_digest: "c".repeat(64),
  };

  function workspacePayload9(
    role: "admin" | "approver" | "operator" | "auditor",
    actions: string[],
    overrides: Record<string, unknown> = {},
  ) {
    return {
      track: "C-DEMO",
      capability_gate: "G3",
      scope: "C-DEMO/demo",
      governance_revision: 3,
      actor_role: role,
      actions,
      active_release: {
        active_generation: 2,
        candidate_id: "candidate_t09release00000000000000000",
        manifest_id: "manifest_2",
        manifest_digest: "2".repeat(64),
        activation_event_id: "governance_act2",
        approval_binding_id: "approval_sha256_b",
        validation_bundle_id: "bundle_2",
        validation_bundle_digest: "bundle-digest-2",
        recovery_release_id: "candidate_t08bootstrap00000000000000000",
        activated_at: 1786000000,
        bootstrap: false,
        final_impact_digest: "f".repeat(64),
        final_impact_manifest_id: "manifest_final",
        final_impact_member_count: 1,
      },
      recovery_anchor: {
        release_candidate_id: "candidate_t08bootstrap00000000000000000",
      },
      holds: [],
      events: [],
      audit_events: [],
      ...overrides,
    };
  }

  function renderGovernance() {
    return render(<GovernanceWorkspacePanel />, {
      wrapper: wrap(createQueryClient()),
    });
  }

  const WORKSPACE_PATH9 = "/controlled/s09/api/queries/workspace";

  it("renders the initial loading surface with live status semantics until the workspace lands", async () => {
    let resolveWorkspace: ((response: Response) => void) | undefined;
    const pending = new Promise<Response>((resolve) => {
      resolveWorkspace = resolve;
    });
    fetchRouter({
      [`GET ${WORKSPACE_PATH9}`]: () => pending,
    });
    renderGovernance();
    await waitFor(() =>
      expect(screen.getByTestId("t09-loading")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("t09-loading")).toHaveAttribute("role", "status");
    resolveWorkspace?.(
      new Response(
        JSON.stringify(workspacePayload9("auditor", [])),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    await waitFor(() =>
      expect(screen.getByTestId("t09-workspace")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("t09-loading")).not.toBeInTheDocument();
  });

  it("operator imposes a scoped hold with the exact reason and scope and the server hold renders with its criterion", async () => {
    const user = userEvent.setup();
    let holds: unknown[] = [];
    const router = fetchRouter({
      [`GET ${WORKSPACE_PATH9}`]: () =>
        new Response(
          JSON.stringify(
            workspacePayload9(
              "operator",
              ["impose_hold", "propose_rollback"],
              { holds },
            ),
          ),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      "POST /controlled/s09/api/commands/impose_hold": () =>
        new Response(
          JSON.stringify({
            status: "accepted",
            hold_id: HOLD.hold_id,
            hold_scope: HOLD.hold_scope,
            reason_code: HOLD.reason_code,
            recovery_criterion_id: HOLD.recovery_criterion_id,
            recovery_criterion_digest: HOLD.recovery_criterion_digest,
            governance_event_id: HOLD.event_id,
            governance_revision: 4,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderGovernance();
    await waitFor(() =>
      expect(screen.getByTestId("t09-impose-form")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("t09-role")).toHaveTextContent("operator");
    expect(screen.getByTestId("t09-holds-empty")).toBeInTheDocument();

    await user.type(screen.getByLabelText("冻结原因码"), "S09_TEST_HOLD");
    // The scope input is prefilled with the served open-cycle scope; the
    // submit carries it untouched.
    // The server hold fact is published by the command; the workspace
    // refetch after acceptance must observe it.
    holds = [HOLD];
    await user.click(screen.getByTestId("t09-impose-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t09-action-ok")).toBeInTheDocument(),
    );
    const posts = router.calls.filter((call) => call.method === "POST");
    expect(posts).toHaveLength(1);
    const body = posts[0].body as Record<string, unknown>;
    expect(body.reason_code).toBe("S09_TEST_HOLD");
    expect(body.hold_scope).toBe("open_cycle");
    expect(body.expected_governance_revision).toBe(3);
    expect(typeof body.idempotency_key).toBe("string");

    // The authoritative refetch renders the server hold: exact scope,
    // reason, actor and the fixed recovery criterion.
    await waitFor(() =>
      expect(screen.getByTestId("t09-hold")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("t09-hold-scope")).toHaveTextContent("open_cycle");
    expect(screen.getByTestId("t09-hold-reason")).toHaveTextContent(
      "S09_TEST_HOLD",
    );
    expect(screen.getByTestId("t09-hold-actor")).toHaveTextContent(
      "c-demo-policy-operator",
    );
    expect(screen.getByTestId("t09-hold-criterion")).toHaveTextContent(
      "s09-hold-recovery-criterion/1",
    );
    // The hold renders the authority revision it fences on and its
    // explicit non-expiring state: active until an explicit recovery.
    expect(screen.getByTestId("t09-hold-authority-revision")).toHaveTextContent(
      "3",
    );
    expect(screen.getByTestId("t09-hold-status")).toHaveTextContent(
      "持续至显式恢复",
    );
    expect(
      screen.queryByTestId("t09-holds-empty"),
    ).not.toBeInTheDocument();
  });

  it("a stale 409 on the hold surfaces the conflict and re-fences on the refetched revision", async () => {
    const user = userEvent.setup();
    let workspaceRequests = 0;
    let holdPosts = 0;
    let resolveRefetch: ((response: Response) => void) | undefined;
    const pendingRefetch = new Promise<Response>((resolve) => {
      resolveRefetch = resolve;
    });
    const router = fetchRouter({
      [`GET ${WORKSPACE_PATH9}`]: () => {
        workspaceRequests += 1;
        if (workspaceRequests === 2) return pendingRefetch;
        return new Response(
          JSON.stringify(
            workspacePayload9("operator", ["impose_hold"], {
              governance_revision: workspaceRequests === 1 ? 3 : 4,
            }),
          ),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
      "POST /controlled/s09/api/commands/impose_hold": () => {
        holdPosts += 1;
        return new Response(
          JSON.stringify({
            detail: { error: "S08_CONFLICT", message: "stale revision" },
          }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        );
      },
    });
    renderGovernance();
    await waitFor(() =>
      expect(screen.getByTestId("t09-impose-form")).toBeInTheDocument(),
    );
    await user.type(screen.getByLabelText("冻结原因码"), "S09_TEST_HOLD");
    await user.click(screen.getByTestId("t09-impose-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t09-conflict")).toHaveTextContent(
        "stale revision",
      ),
    );
    expect(holdPosts).toBe(1);
    // The latch stays locked until the authoritative refetch lands: the
    // button is disabled and a click sends nothing.
    expect(screen.getByTestId("t09-impose-button")).toBeDisabled();
    await user.click(screen.getByTestId("t09-impose-button"));
    expect(holdPosts).toBe(1);

    resolveRefetch?.(
      new Response(
        JSON.stringify(
          workspacePayload9("operator", ["impose_hold"], {
            governance_revision: 4,
          }),
        ),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    await waitFor(() =>
      expect(screen.getByTestId("t09-impose-button")).toBeEnabled(),
    );
    await user.click(screen.getByTestId("t09-impose-button"));
    await waitFor(() => expect(holdPosts).toBe(2));
    const posts = router.calls.filter((call) => call.method === "POST");
    const second = posts[1].body as Record<string, unknown>;
    expect(second.expected_governance_revision).toBe(4);
    expect(second.idempotency_key).not.toBe(
      (posts[0].body as Record<string, unknown>).idempotency_key,
    );
  });

  it("a 409 followed by a failed authoritative refetch renders a reload control and keeps commands fenced until a successful reload", async () => {
    const user = userEvent.setup();
    let workspaceRequests = 0;
    let holdPosts = 0;
    const router = fetchRouter({
      [`GET ${WORKSPACE_PATH9}`]: () => {
        workspaceRequests += 1;
        if (workspaceRequests >= 2 && workspaceRequests <= 4) {
          // The conflict refetch plus its transient retries all fail
          // closed; only the explicit reload succeeds.
          return new Response(
            JSON.stringify({
              detail: { error: "S08_UNAVAILABLE", message: "temporarily down" },
            }),
            { status: 503, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(
          JSON.stringify(
            workspacePayload9("operator", ["impose_hold"], {
              governance_revision: workspaceRequests >= 5 ? 4 : 3,
            }),
          ),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      },
      "POST /controlled/s09/api/commands/impose_hold": () => {
        holdPosts += 1;
        return new Response(
          JSON.stringify({
            detail: { error: "S08_CONFLICT", message: "stale revision" },
          }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        );
      },
    });
    renderGovernance();
    await waitFor(() =>
      expect(screen.getByTestId("t09-impose-form")).toBeInTheDocument(),
    );
    await user.type(screen.getByLabelText("冻结原因码"), "S09_TEST_HOLD");
    await user.click(screen.getByTestId("t09-impose-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t09-conflict")).toHaveTextContent(
        "stale revision",
      ),
    );
    // The failed authoritative refetch (plus its transient retries) renders
    // the explicit reload control (P-2): the healthy cached workspace facts
    // stay visible and every command remains fenced on the stale revision.
    await waitFor(
      () => expect(screen.getByTestId("t09-conflict-reload")).toBeInTheDocument(),
      { timeout: 8_000 },
    );
    expect(screen.getByTestId("t09-workspace")).toBeInTheDocument();
    expect(screen.getByTestId("t09-impose-button")).toBeDisabled();
    await user.click(screen.getByTestId("t09-impose-button"));
    expect(holdPosts).toBe(1);
    // The explicit reload succeeds, settles the old command identity and
    // re-fences the next command on the new server revision.
    await user.click(screen.getByTestId("t09-conflict-reload-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t09-impose-button")).toBeEnabled(),
    );
    await user.click(screen.getByTestId("t09-impose-button"));
    await waitFor(() => expect(holdPosts).toBe(2));
    const posts = router.calls.filter((call) => call.method === "POST");
    expect(
      (posts[1].body as Record<string, unknown>).expected_governance_revision,
    ).toBe(4);
  });

  it("renders no mutation surface for a role without actions", async () => {
    fetchRouter({
      [`GET ${WORKSPACE_PATH9}`]: () =>
        new Response(
          JSON.stringify(workspacePayload9("admin", [])),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderGovernance();
    await waitFor(() =>
      expect(screen.getByTestId("t09-role")).toHaveTextContent("admin"),
    );
    expect(screen.queryByTestId("t09-impose-form")).not.toBeInTheDocument();
    expect(screen.getByTestId("t09-action-list")).toHaveTextContent("—");
  });

  it("approver under an active hold sees the server recovery action and the hold criterion", async () => {
    fetchRouter({
      [`GET ${WORKSPACE_PATH9}`]: () =>
        new Response(
          JSON.stringify(
            workspacePayload9("approver", ["recover_hold"], {
              holds: [HOLD],
            }),
          ),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderGovernance();
    await waitFor(() =>
      expect(screen.getByTestId("t09-role")).toHaveTextContent("approver"),
    );
    expect(screen.getByTestId("t09-action-list")).toHaveTextContent(
      "recover_hold",
    );
    expect(screen.getByTestId("t09-hold")).toBeInTheDocument();
    expect(screen.getByTestId("t09-hold-criterion")).toHaveTextContent(
      "s09-hold-recovery-criterion/1",
    );
    expect(screen.queryByTestId("t09-impose-form")).not.toBeInTheDocument();
  });

  it("auditor sees the reconciliation members and the partial disposition state", async () => {
    fetchRouter({
      [`GET ${WORKSPACE_PATH9}`]: () =>
        new Response(
          JSON.stringify(workspacePayload9("auditor", [])),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      "GET /controlled/s01/api/queries/impact-dispositions/reconciliation": () =>
        new Response(
          JSON.stringify({
            final_impact_digest: "f".repeat(64),
            member_count: 2,
            unconsumed_count: 1,
            outstanding_count: 1,
            projection_watermark: 5,
            members: [
              {
                application_id: "app_t09recon000000000000000000",
                cycle: 1,
                partition: "open_cycle",
                disposition: "applied",
                target_generation: 2,
                reevaluation_job_id: "job_t09recon000000000000000000",
                reevaluation_job_count: 1,
              },
              {
                application_id: "app_t09recon000000000000000001",
                cycle: 1,
                partition: "open_cycle",
                disposition: "outstanding",
                target_generation: 2,
                reevaluation_job_id: null,
                reevaluation_job_count: 0,
              },
            ],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderGovernance();
    await waitFor(
      () => expect(screen.getByTestId("t09-recon-members")).toBeInTheDocument(),
      { timeout: 8_000 },
    );
    expect(screen.getByTestId("t09-recon-members")).toHaveTextContent(
      "app_t09recon000000000000000000",
    );
    expect(screen.getByTestId("t09-recon-members")).toHaveTextContent(
      "outstanding",
    );
    // A partial reconciliation is an explicit limitation, never hidden.
    expect(screen.getByTestId("t09-recon-partial")).toHaveTextContent(
      "存在未消费",
    );
  });

  it("renders explicit unavailable states for the workspace and the reconciliation separately", async () => {
    // Workspace authority unavailable.
    fetchRouter({
      [`GET ${WORKSPACE_PATH9}`]: () =>
        new Response(
          JSON.stringify({
            detail: {
              error: "S08_UNAVAILABLE",
              message: "Governance authority is unavailable",
            },
          }),
          { status: 503, headers: { "Content-Type": "application/json" } },
        ),
    });
    const firstView = renderGovernance();
    await waitFor(
      () => expect(screen.getByTestId("t09-unavailable")).toBeInTheDocument(),
      // The shared query policy retries transient 503s twice with backoff;
      // the closed unavailable state appears once the retries are spent.
      { timeout: 8_000 },
    );
    firstView.unmount();

    // Workspace healthy but the reconciliation projection is unavailable.
    const { unmount } = render(
      <GovernanceWorkspacePanel />,
      { wrapper: wrap(createQueryClient()) },
    );
    fetchRouter({
      [`GET ${WORKSPACE_PATH9}`]: () =>
        new Response(
          JSON.stringify(workspacePayload9("auditor", [])),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      "GET /controlled/s01/api/queries/impact-dispositions/reconciliation": () =>
        new Response(
          JSON.stringify({
            detail: {
              error: "S01_UNAVAILABLE",
              message: "Controlled S01 is unavailable",
            },
          }),
          { status: 503, headers: { "Content-Type": "application/json" } },
        ),
    });
    await waitFor(
      () =>
        expect(
          screen.getByTestId("t09-recon-unavailable"),
        ).toBeInTheDocument(),
      { timeout: 8_000 },
    );
    expect(
      screen.queryByTestId("t09-unavailable"),
    ).not.toBeInTheDocument();
    unmount();
  }, 30_000);

  it.each([403, 404, 500, 503])(
    "reconciliation %i keeps the healthy workspace and distinguishes the state",
    async (status) => {
      fetchRouter({
        [`GET ${WORKSPACE_PATH9}`]: () =>
          new Response(
            JSON.stringify(workspacePayload9("auditor", [])),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        "GET /controlled/s01/api/queries/impact-dispositions/reconciliation": () =>
          new Response(
            JSON.stringify({
              detail: { error: `ERR_${status}`, message: "reconciliation" },
            }),
            { status, headers: { "Content-Type": "application/json" } },
          ),
      });
      renderGovernance();
      await waitFor(() =>
        expect(screen.getByTestId("t09-workspace")).toBeInTheDocument(),
      );
      const expected =
        status === 403
          ? "t09-recon-forbidden"
          : status === 404
            ? "t09-recon-pending"
            : "t09-recon-unavailable";
      // Transient statuses are retried by the shared query policy; the
      // closed state appears once the retries are spent.
      await waitFor(
        () => expect(screen.getByTestId(expected)).toBeInTheDocument(),
        { timeout: 8_000 },
      );
      // The healthy workspace facts stay visible beside the reconciliation
      // state; a reload is offered for every transient state but never for
      // the deterministic 403 denial.
      expect(screen.getByTestId("t09-role")).toHaveTextContent("auditor");
      expect(screen.getByTestId("t09-revision")).toHaveTextContent("3");
      const reload = screen.queryByTestId("t09-recon-reload");
      if (status === 403) {
        expect(reload).not.toBeInTheDocument();
      } else {
        expect(reload).toBeInTheDocument();
      }
    },
  );

  it("renders the minimized Security Audit records only for the auditor role", async () => {
    const AUDIT_RECORD = {
      event_id: "audit_t09http000000000000000000000000001",
      action: "s08_impose_hold",
      subject: "c-demo-policy-operator",
      role: "operator",
      result: "accepted",
      reason_code: "S09_HOLD_IMPOSED",
      event_time: 1786000000,
      hold_id: "governance_hold0000000000000000001",
    };
    fetchRouter({
      [`GET ${WORKSPACE_PATH9}`]: () =>
        new Response(
          JSON.stringify(
            workspacePayload9("auditor", [], {
              audit_events: [AUDIT_RECORD],
            }),
          ),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderGovernance();
    await waitFor(() =>
      expect(screen.getByTestId("t09-audit")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("t09-audit-record")).toHaveTextContent(
      "s08_impose_hold · c-demo-policy-operator · operator · accepted · S09_HOLD_IMPOSED · 冻结 governance_hold0000000000000000001",
    );
  });

  it("never renders an audit section for non-auditor roles", async () => {
    fetchRouter({
      [`GET ${WORKSPACE_PATH9}`]: () =>
        new Response(
          JSON.stringify(
            workspacePayload9("operator", ["impose_hold"]),
          ),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderGovernance();
    await waitFor(() =>
      expect(screen.getByTestId("t09-role")).toHaveTextContent("operator"),
    );
    expect(screen.queryByTestId("t09-audit")).not.toBeInTheDocument();
  });

  it("operator proposes a rollback with the server known-good release and hands the new candidate to the S08 workspace", async () => {
    const user = userEvent.setup();
    let events: unknown[] = [];
    const router = fetchRouter({
      [`GET ${WORKSPACE_PATH9}`]: () =>
        new Response(
          JSON.stringify(
            workspacePayload9(
              "operator",
              ["impose_hold", "propose_rollback"],
              { holds: [HOLD], events },
            ),
          ),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      "POST /controlled/s09/api/commands/propose_rollback": () =>
        new Response(
          JSON.stringify({
            status: "accepted",
            candidate_id: "candidate_t09rollback00000000000000000",
            manifest_id: "manifest_rollback",
            manifest_digest: "3".repeat(64),
            validation_bundle_id: "bundle_rollback",
            validation_bundle_digest: "bundle-digest-rollback",
            rollback_target_id: "candidate_t08bootstrap00000000000000000",
            compatibility: {
              compatible: true,
              reason_code: "S09_ROLLBACK_COMPATIBLE",
            },
            governance_revision: 4,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderGovernance();
    await waitFor(() =>
      expect(screen.getByTestId("t09-rollback-form")).toBeInTheDocument(),
    );
    // The release input is prefilled with the server's recorded known-good
    // recovery anchor; the submit carries it untouched.
    expect(
      (screen.getByLabelText("回滚发布标识") as HTMLInputElement).value,
    ).toBe("candidate_t08bootstrap00000000000000000");
    await user.type(screen.getByLabelText("回滚原因码"), "S09_TEST_ROLLBACK");
    // The rollback command appends the immutable rollback_proposed fact;
    // the workspace refetch after acceptance must observe it.
    events = [
      {
        event_id: "governance_event0000000000000000009",
        revision: 9,
        kind: "rollback_proposed",
        actor: { subject: "c-demo-policy-operator", role: "operator" },
        trusted_time: 1786000000,
        reason_code: "S09_ROLLBACK_PROPOSED",
        candidate_id: null,
        manifest_id: "manifest_rollback",
        activation_event_id: null,
        active_generation: null,
        hold_id: null,
        release_candidate_id: "candidate_t08bootstrap00000000000000000",
      },
    ];
    await user.click(screen.getByTestId("t09-rollback-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t09-rollback-result")).toBeInTheDocument(),
    );
    // The asynchronous compatibility result carries live status semantics.
    expect(screen.getByTestId("t09-rollback-result")).toHaveAttribute(
      "role",
      "status",
    );
    const posts = router.calls.filter((call) => call.method === "POST");
    expect(posts).toHaveLength(1);
    const body = posts[0].body as Record<string, unknown>;
    expect(body.release_candidate_id).toBe(
      "candidate_t08bootstrap00000000000000000",
    );
    expect(body.reason_code).toBe("S09_TEST_ROLLBACK");
    expect(body.expected_governance_revision).toBe(3);
    // The compatibility verdict and the new candidate render; the handoff
    // link opens the existing S08 candidate workspace.
    expect(screen.getByTestId("t09-rollback-compatibility")).toHaveTextContent(
      "兼容",
    );
    expect(screen.getByTestId("t09-rollback-candidate")).toHaveTextContent(
      "candidate_t09rollback00000000000000000",
    );
    const link = screen.getByTestId(
      "t09-rollback-link",
    ) as HTMLAnchorElement;
    expect(link.getAttribute("href")).toBe(
      "/controlled/s08/react?candidate=candidate_t09rollback00000000000000000",
    );
    // The append-only ledger renders the rollback_proposed event ref with
    // the exact release identity.
    await waitFor(() =>
      expect(screen.getByTestId("t09-events")).toHaveTextContent(
        "rollback_proposed",
      ),
    );
    expect(screen.getByTestId("t09-events")).toHaveTextContent(
      "candidate_t08bootstrap00000000000000000",
    );
  });

  it("an incompatible rollback verdict renders explicitly and keeps the hold", async () => {
    const user = userEvent.setup();
    fetchRouter({
      [`GET ${WORKSPACE_PATH9}`]: () =>
        new Response(
          JSON.stringify(
            workspacePayload9(
              "operator",
              ["impose_hold", "propose_rollback"],
              { holds: [HOLD] },
            ),
          ),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      "POST /controlled/s09/api/commands/propose_rollback": () =>
        new Response(
          JSON.stringify({
            status: "accepted",
            candidate_id: "candidate_t09rollback00000000000000000",
            manifest_id: "manifest_rollback",
            manifest_digest: "3".repeat(64),
            validation_bundle_id: "bundle_rollback",
            validation_bundle_digest: "bundle-digest-rollback",
            rollback_target_id: "candidate_t08bootstrap00000000000000000",
            compatibility: {
              compatible: false,
              reason_code: "ROLLBACK_INCOMPATIBLE_VALIDATION",
            },
            governance_revision: 4,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderGovernance();
    await waitFor(() =>
      expect(screen.getByTestId("t09-rollback-form")).toBeInTheDocument(),
    );
    await user.type(screen.getByLabelText("回滚原因码"), "S09_TEST_ROLLBACK");
    await user.click(screen.getByTestId("t09-rollback-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t09-rollback-result")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("t09-rollback-compatibility")).toHaveTextContent(
      "不兼容",
    );
    expect(screen.getByTestId("t09-rollback-compatibility")).toHaveTextContent(
      "ROLLBACK_INCOMPATIBLE_VALIDATION",
    );
    // No handoff for an incompatible rollback: only a governed forward fix.
    expect(screen.queryByTestId("t09-rollback-link")).not.toBeInTheDocument();
    // The hold stays in force and visible.
    expect(screen.getByTestId("t09-hold")).toBeInTheDocument();
  });

  it("approver recovers the hold with the exact active generation", async () => {
    const user = userEvent.setup();
    let holds: unknown[] = [HOLD];
    const router = fetchRouter({
      [`GET ${WORKSPACE_PATH9}`]: () =>
        new Response(
          JSON.stringify(
            workspacePayload9("approver", ["recover_hold"], { holds }),
          ),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      "POST /controlled/s09/api/commands/recover_hold": () =>
        new Response(
          JSON.stringify({
            status: "accepted",
            hold_id: HOLD.hold_id,
            hold_released_event_id: "governance_event0000000000000000002",
            recovery_generation: 2,
            governance_revision: 4,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderGovernance();
    await waitFor(() =>
      expect(screen.getByTestId("t09-recover-form")).toBeInTheDocument(),
    );
    // The generation is prefilled with the server's current active
    // generation and is read-only — recovery always uses the server's
    // current active generation, never a user-chosen one.
    expect(
      (screen.getByLabelText("恢复代次") as HTMLInputElement).value,
    ).toBe("2");
    expect(
      (screen.getByLabelText("恢复代次") as HTMLInputElement).readOnly,
    ).toBe(true);
    // The recovery command appends the release fact; the workspace refetch
    // after acceptance must observe the released hold union.
    holds = [];
    await user.click(screen.getByTestId("t09-recover-button"));
    await waitFor(() =>
      expect(screen.getByTestId("t09-action-ok")).toBeInTheDocument(),
    );
    const posts = router.calls.filter((call) => call.method === "POST");
    expect(posts).toHaveLength(1);
    const body = posts[0].body as Record<string, unknown>;
    expect(body.hold_id).toBe(HOLD.hold_id);
    expect(body.recovery_generation).toBe(2);
    expect(body.expected_governance_revision).toBe(3);

    // The authoritative refetch renders the released hold union.
    await waitFor(() =>
      expect(screen.getByTestId("t09-holds-empty")).toBeInTheDocument(),
    );
  });

  it("renders the immutable append-only event refs with the S09 identities", async () => {
    fetchRouter({
      [`GET ${WORKSPACE_PATH9}`]: () =>
        new Response(
          JSON.stringify(
            workspacePayload9("admin", [], {
              events: [
                {
                  event_id: "governance_event0000000000000000001",
                  revision: 1,
                  kind: "activated",
                  actor: { subject: "c-demo-policy-admin", role: "admin" },
                  trusted_time: 1786000000,
                  reason_code: "S08_ACTIVATED",
                  candidate_id: "candidate_t08bootstrap00000000000000000",
                  manifest_id: "manifest_bootstrap",
                  activation_event_id: "governance_event0000000000000000001",
                  active_generation: 1,
                  hold_id: null,
                  release_candidate_id: null,
                },
                {
                  event_id: "governance_event0000000000000000005",
                  revision: 5,
                  kind: "impact_previewed",
                  actor: { subject: "c-demo-policy-admin", role: "admin" },
                  trusted_time: 1786000000,
                  reason_code: "S09_IMPACT_PREVIEWED",
                  candidate_id: "candidate_t09release00000000000000000",
                  manifest_id: "manifest_2",
                  activation_event_id: null,
                  active_generation: null,
                  hold_id: null,
                  release_candidate_id: null,
                },
                {
                  event_id: "governance_event0000000000000000006",
                  revision: 6,
                  kind: "hold_imposed",
                  actor: { subject: "c-demo-policy-operator", role: "operator" },
                  trusted_time: 1786000000,
                  reason_code: "S09_HOLD_IMPOSED",
                  candidate_id: null,
                  manifest_id: null,
                  activation_event_id: null,
                  active_generation: null,
                  hold_id: HOLD.hold_id,
                  release_candidate_id: null,
                },
                {
                  event_id: "governance_event0000000000000000007",
                  revision: 7,
                  kind: "hold_released",
                  actor: { subject: "c-demo-policy-approver", role: "approver" },
                  trusted_time: 1786000000,
                  reason_code: "S09_HOLD_RELEASED",
                  candidate_id: null,
                  manifest_id: null,
                  activation_event_id: null,
                  active_generation: null,
                  hold_id: HOLD.hold_id,
                  release_candidate_id: null,
                },
                {
                  event_id: "governance_event0000000000000000008",
                  revision: 8,
                  kind: "rollback_proposed",
                  actor: { subject: "c-demo-policy-operator", role: "operator" },
                  trusted_time: 1786000000,
                  reason_code: "S09_ROLLBACK_PROPOSED",
                  candidate_id: null,
                  manifest_id: "manifest_rollback",
                  activation_event_id: null,
                  active_generation: null,
                  hold_id: null,
                  release_candidate_id: "candidate_t08bootstrap00000000000000000",
                },
              ],
            }),
          ),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderGovernance();
    await waitFor(() =>
      expect(screen.getByTestId("t09-events")).toBeInTheDocument(),
    );
    const events = screen.getByTestId("t09-events");
    expect(events).toHaveTextContent("activated");
    expect(events).toHaveTextContent("impact_previewed");
    expect(events).toHaveTextContent("hold_imposed");
    expect(events).toHaveTextContent("hold_released");
    expect(events).toHaveTextContent("rollback_proposed");
    expect(events).toHaveTextContent(HOLD.hold_id);
    expect(events).toHaveTextContent(
      "candidate_t08bootstrap00000000000000000",
    );
    expect(events).toHaveTextContent("c-demo-policy-operator");
    // Append-only identity: revisions are rendered in ascending order.
    const revisionTexts = Array.from(
      document.querySelectorAll('[data-testid="t09-event"]'),
    ).map((node) => node.textContent ?? "");
    const revisions = revisionTexts.map((text) =>
      Number(text.match(/修订 (\d+)/)?.[1]),
    );
    expect(revisions).toEqual([...revisions].sort((a, b) => a - b));
  });

  describe("F-SPEC-2 cached refetch currentness", () => {
    function renderGovernanceWithClient() {
      const client = createQueryClient();
      const view = render(<GovernanceWorkspacePanel />, {
        wrapper: wrap(client),
      });
      return { client, ...view };
    }

    function workspaceResponse(
      payload: unknown,
      status = 200,
    ): Response {
      return new Response(JSON.stringify(payload), {
        status,
        headers: { "Content-Type": "application/json" },
      });
    }

    function reconError(status: number): Response {
      return workspaceResponse(
        { detail: { error: `ERR_${status}`, message: "reconciliation" } },
        status,
      );
    }

    function reconciliationPayload() {
      return {
        final_impact_digest: "f".repeat(64),
        member_count: 1,
        unconsumed_count: 0,
        outstanding_count: 0,
        projection_watermark: 5,
        members: [
          {
            application_id: "app_t09recon000000000000000000",
            cycle: 1,
            partition: "open_cycle",
            disposition: "applied",
            target_generation: 2,
            reevaluation_job_id: "job_t09recon000000000000000000",
            reevaluation_job_count: 1,
          },
        ],
      };
    }

    it.each([403, 404])(
      "workspace cached %i hides the stale protected surface before any command",
      async (status) => {
        let calls = 0;
        fetchRouter({
          [`GET ${WORKSPACE_PATH9}`]: () => {
            calls += 1;
            if (calls === 1) {
              return workspaceResponse(
                workspacePayload9("auditor", [], {
                  audit_events: [
                    {
                      event_id: "audit_t09cached000000000000000000000001",
                      action: "s08_impose_hold",
                      subject: "c-demo-policy-operator",
                      role: "operator",
                      result: "accepted",
                      reason_code: "S09_HOLD_IMPOSED",
                      event_time: 1786000000,
                      hold_id: "governance_hold0000000000000000001",
                    },
                  ],
                }),
              );
            }
            return reconError(status);
          },
        });
        const { client } = renderGovernanceWithClient();
        await waitFor(() =>
          expect(screen.getByTestId("t09-audit")).toBeInTheDocument(),
        );
        await client.refetchQueries({ queryKey: ["s09"] });
        const expected = status === 403 ? "t09-forbidden" : "t09-not-found";
        await waitFor(() =>
          expect(screen.getByTestId(expected)).toBeInTheDocument(),
        );
        // The stale protected surface is gone: no workspace, no audit refs,
        // no command surface.
        expect(screen.queryByTestId("t09-workspace")).not.toBeInTheDocument();
        expect(screen.queryByTestId("t09-audit")).not.toBeInTheDocument();
        expect(screen.queryByTestId("t09-impose-form")).not.toBeInTheDocument();
      },
    );

    it("workspace cached 500 marks last-known, fences every mutation and recovers on reload", async () => {
      let calls = 0;
      fetchRouter({
        [`GET ${WORKSPACE_PATH9}`]: () => {
          calls += 1;
          if (calls === 1 || calls === 3) {
            return workspaceResponse(
              workspacePayload9(
                "operator",
                ["impose_hold", "propose_rollback"],
                calls === 3
                  ? { governance_revision: 4, holds: [] }
                  : { governance_revision: 3, holds: [] },
              ),
            );
          }
          return workspaceResponse(
            { detail: { error: "ERR_500", message: "server" } },
            500,
          );
        },
      });
      const { client } = renderGovernanceWithClient();
      await waitFor(() =>
        expect(screen.getByTestId("t09-impose-button")).toBeEnabled(),
      );
      await client.refetchQueries({ queryKey: ["s09"] });
      await waitFor(() =>
        expect(screen.getByTestId("t09-stale")).toBeInTheDocument(),
      );
      // The last-known facts stay visible but every mutation control is
      // fenced and an explicit reload is offered.
      expect(screen.getByTestId("t09-role")).toHaveTextContent("operator");
      expect(screen.getByTestId("t09-revision")).toHaveTextContent("3");
      expect(screen.getByTestId("t09-impose-button")).toBeDisabled();
      expect(screen.getByTestId("t09-rollback-button")).toBeDisabled();
      fireEvent.click(screen.getByTestId("t09-workspace-reload"));
      await waitFor(() =>
        expect(screen.queryByTestId("t09-stale")).not.toBeInTheDocument(),
      );
      // The successful reload restores the fresh authoritative revision.
      expect(screen.getByTestId("t09-revision")).toHaveTextContent("4");
      expect(screen.getByTestId("t09-impose-button")).toBeEnabled();
    });

    it("workspace cached 503 retries transiently then marks last-known, fences every mutation and recovers on reload", async () => {
      // 503 is a transient status in retryPolicy, so the query retries
      // (with backoff) before the error is definitive; the mock keeps
      // failing across every retry and only succeeds after the explicit
      // reload turns the state machine off.
      let calls = 0;
      let failRefetch = true;
      fetchRouter({
        [`GET ${WORKSPACE_PATH9}`]: () => {
          calls += 1;
          if (calls === 1 || !failRefetch) {
            return workspaceResponse(
              workspacePayload9(
                "operator",
                ["impose_hold", "propose_rollback"],
                calls === 1
                  ? { governance_revision: 3, holds: [] }
                  : { governance_revision: 4, holds: [] },
              ),
            );
          }
          return workspaceResponse(
            { detail: { error: "ERR_503", message: "server" } },
            503,
          );
        },
      });
      const { client } = renderGovernanceWithClient();
      await waitFor(() =>
        expect(screen.getByTestId("t09-impose-button")).toBeEnabled(),
      );
      await client.refetchQueries({ queryKey: ["s09"] });
      await waitFor(
        () => expect(screen.getByTestId("t09-stale")).toBeInTheDocument(),
        { timeout: 8_000 },
      );
      // The last-known facts stay visible but every mutation control is
      // fenced and an explicit reload is offered.
      expect(screen.getByTestId("t09-role")).toHaveTextContent("operator");
      expect(screen.getByTestId("t09-revision")).toHaveTextContent("3");
      expect(screen.getByTestId("t09-impose-button")).toBeDisabled();
      expect(screen.getByTestId("t09-rollback-button")).toBeDisabled();
      failRefetch = false;
      fireEvent.click(screen.getByTestId("t09-workspace-reload"));
      await waitFor(() =>
        expect(screen.queryByTestId("t09-stale")).not.toBeInTheDocument(),
      );
      // The successful reload restores the fresh authoritative revision.
      expect(screen.getByTestId("t09-revision")).toHaveTextContent("4");
      expect(screen.getByTestId("t09-impose-button")).toBeEnabled();
    });

    it("workspace cached transient failure keeps the auditor surface stale and restores it on reload", async () => {
      // The transient status is retried by retryPolicy before the error is
      // definitive, so the mock fails across every retry and only succeeds
      // after the explicit reload turns the state machine off.
      let calls = 0;
      let failRefetch = true;
      fetchRouter({
        [`GET ${WORKSPACE_PATH9}`]: () => {
          calls += 1;
          if (calls === 1 || !failRefetch) {
            return workspaceResponse(
              workspacePayload9("auditor", [], {
                governance_revision: calls === 1 ? 3 : 4,
                audit_events: [
                  {
                    event_id: "audit_t09stale0000000000000000000000001",
                    action: "s08_impose_hold",
                    subject: "c-demo-policy-operator",
                    role: "operator",
                    result: "accepted",
                    reason_code: "S09_HOLD_IMPOSED",
                    event_time: 1786000000,
                    hold_id: "governance_hold0000000000000000001",
                  },
                ],
              }),
            );
          }
          return workspaceResponse(
            { detail: { error: "ERR_503", message: "server" } },
            503,
          );
        },
      });
      const { client } = renderGovernanceWithClient();
      await waitFor(() =>
        expect(screen.getByTestId("t09-audit")).toBeInTheDocument(),
      );
      await client.refetchQueries({ queryKey: ["s09"] });
      await waitFor(
        () => expect(screen.getByTestId("t09-stale")).toBeInTheDocument(),
        { timeout: 8_000 },
      );
      // The cached auditor surface stays visible but is labelled stale.
      expect(screen.getByTestId("t09-role")).toHaveTextContent("auditor");
      expect(screen.getByTestId("t09-audit")).toBeInTheDocument();
      failRefetch = false;
      fireEvent.click(screen.getByTestId("t09-workspace-reload"));
      await waitFor(() =>
        expect(screen.queryByTestId("t09-stale")).not.toBeInTheDocument(),
      );
      // The successful reload restores the fresh authoritative revision
      // and the audit facts along with it.
      expect(screen.getByTestId("t09-revision")).toHaveTextContent("4");
      expect(screen.getByTestId("t09-audit")).toBeInTheDocument();
      expect(screen.getByTestId("t09-audit-record")).toHaveTextContent(
        "s08_impose_hold · c-demo-policy-operator · operator · accepted · S09_HOLD_IMPOSED · 冻结 governance_hold0000000000000000001",
      );
    });

    it("workspace cached transport failure marks last-known and fences mutations", async () => {
      let calls = 0;
      fetchRouter({
        [`GET ${WORKSPACE_PATH9}`]: () => {
          calls += 1;
          if (calls === 1) {
            return workspaceResponse(
              workspacePayload9("operator", ["impose_hold"]),
            );
          }
          return Promise.reject(new TypeError("network down"));
        },
      });
      const { client } = renderGovernanceWithClient();
      await waitFor(() =>
        expect(screen.getByTestId("t09-impose-button")).toBeEnabled(),
      );
      await client.refetchQueries({ queryKey: ["s09"] });
      await waitFor(
        () => expect(screen.getByTestId("t09-stale")).toBeInTheDocument(),
        { timeout: 8_000 },
      );
      expect(screen.getByTestId("t09-impose-button")).toBeDisabled();
      expect(screen.getByTestId("t09-workspace-reload")).toBeInTheDocument();
    });

    it.each([403, 404, 500, 503])(
      "reconciliation cached %i distinguishes the state from stale detail",
      async (status) => {
        let reconCalls = 0;
        let failRefetch = true;
        fetchRouter({
          [`GET ${WORKSPACE_PATH9}`]: () =>
            workspaceResponse(workspacePayload9("auditor", [])),
          "GET /controlled/s01/api/queries/impact-dispositions/reconciliation":
            () => {
              reconCalls += 1;
              if (reconCalls === 1 || !failRefetch) {
                return workspaceResponse(reconciliationPayload());
              }
              return reconError(status);
            },
        });
        const { client } = renderGovernanceWithClient();
        await waitFor(() =>
          expect(screen.getByTestId("t09-recon-members")).toBeInTheDocument(),
        );
        await client.refetchQueries({ queryKey: ["s09"] });
        const expected =
          status === 403
            ? "t09-recon-forbidden"
            : status === 404
              ? "t09-recon-pending"
              : "t09-recon-unavailable";
        await waitFor(
          () => expect(screen.getByTestId(expected)).toBeInTheDocument(),
          { timeout: 8_000 },
        );
        if (status === 403 || status === 404) {
          // Deterministic 403/404 states hide stale protected detail.
          expect(
            screen.queryByTestId("t09-recon-members"),
          ).not.toBeInTheDocument();
          if (status === 403) {
            expect(
              screen.queryByTestId("t09-recon-reload"),
            ).not.toBeInTheDocument();
          } else {
            // A pending projection remains recoverable through an explicit
            // refetch; success restores only the fresh server DTO.
            failRefetch = false;
            fireEvent.click(screen.getByTestId("t09-recon-reload"));
            await waitFor(() =>
              expect(
                screen.queryByTestId("t09-recon-pending"),
              ).not.toBeInTheDocument(),
            );
            expect(screen.getByTestId("t09-recon-members")).toBeInTheDocument();
          }
        } else {
          // Transient failures keep the last-known detail but label it.
          expect(screen.getByTestId("t09-recon-stale")).toBeInTheDocument();
          expect(
            screen.getByTestId("t09-recon-members"),
          ).toBeInTheDocument();
          // The explicit reload restores the fresh authoritative detail.
          failRefetch = false;
          fireEvent.click(screen.getByTestId("t09-recon-reload"));
          await waitFor(() =>
            expect(
              screen.queryByTestId("t09-recon-stale"),
            ).not.toBeInTheDocument(),
          );
          expect(
            screen.queryByTestId("t09-recon-unavailable"),
          ).not.toBeInTheDocument();
          expect(screen.getByTestId("t09-recon-members")).toBeInTheDocument();
        }
      },
      30_000,
    );

    it("a command success whose invalidation refetch fails never leaves unmarked cached authority", async () => {
      const user = userEvent.setup();
      let workspaceCalls = 0;
      fetchRouter({
        [`GET ${WORKSPACE_PATH9}`]: () => {
          workspaceCalls += 1;
          if (workspaceCalls === 1) {
            return workspaceResponse(
              workspacePayload9("operator", ["impose_hold"]),
            );
          }
          return workspaceResponse(
            { detail: { error: "ERR_500", message: "server" } },
            500,
          );
        },
        "POST /controlled/s09/api/commands/impose_hold": () =>
          workspaceResponse({
            status: "accepted",
            hold_id: HOLD.hold_id,
            hold_scope: HOLD.hold_scope,
            reason_code: HOLD.reason_code,
            recovery_criterion_id: HOLD.recovery_criterion_id,
            recovery_criterion_digest: HOLD.recovery_criterion_digest,
            governance_event_id: HOLD.event_id,
            governance_revision: 4,
          }),
      });
      renderGovernanceWithClient();
      await waitFor(() =>
        expect(screen.getByTestId("t09-impose-button")).toBeEnabled(),
      );
      await user.type(screen.getByLabelText("冻结原因码"), "S09_TEST_HOLD");
      await user.click(screen.getByTestId("t09-impose-button"));
      await waitFor(() =>
        expect(screen.getByTestId("t09-action-ok")).toBeInTheDocument(),
      );
      // The invalidation refetch after the accepted command fails: the
      // cached authority must carry the explicit last-known marker.
      await waitFor(() =>
        expect(screen.getByTestId("t09-stale")).toBeInTheDocument(),
      );
      expect(screen.getByTestId("t09-impose-button")).toBeDisabled();
    });
  });
});
