import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import AttachmentVersionPanel from "./AttachmentVersionPanel";
import { fetchRouter, renderWithQuery } from "../test-utils";

const REQUEST_ID = "supplement_request_t04integrator00000000000000000000";
const VIEW_PATH = `/controlled/s02/api/queries/supplement-requests/${REQUEST_ID}`;
const SUBMIT_PATH = "/controlled/s02/api/commands/submit-attachment-version";

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function projectionPayload(overrides: Record<string, unknown> = {}) {
  return {
    schema_version: "supplement-request-integrator/1",
    request_id: REQUEST_ID,
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
      required_fact_kinds: ["attachment", "page", "producer", "vin_observation"],
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
    ...overrides,
  };
}

function receiptPayload(overrides: Record<string, unknown> = {}) {
  return {
    disposition: "accepted",
    reason_code: null,
    responsible_party: null,
    recovery_action: null,
    retryable: false,
    application_id: "app_t04integrator",
    receipt_id: "receipt_t04integrator",
    job_id: null,
    lifecycle_revision: 8,
    evidence_revision: 2,
    replayed: false,
    envelope_version: "registered-observation-envelope/1",
    schema_version: "1.0.0",
    semantic_version: "1.0.0",
    envelope_id: "envelope_t04integrator",
    stream_id: "stream_t04integrator",
    source_revision: 1,
    source_revision_id: "revision_t04integrator",
    envelope_fingerprint: "f".repeat(64),
    adapter_id: "s06-detection-adapter",
    adapter_version: "1",
    source_registration_digest: "d".repeat(64),
    artifact_manifest_digest: "e".repeat(64),
    fact_counts: {},
    gate_results: [],
    tenant_id: "c-demo",
    source_system_id: "s06-material-source",
    claim_label: null,
    real_cross_document_opportunities: 0,
    performance_status: "not_estimable",
    request_id: REQUEST_ID,
    request_status: "open",
    batch_id: "batch_t04integrator",
    batch_closed: false,
    request_progress_revision: 1,
    attachment_id: "attachment_t04v2",
    attachment_version: 2,
    supersedes_attachment_id: "attachment_t04v1",
    fulfilled: false,
    phase: "Awaiting Evidence",
    route: "awaiting_evidence",
    recovery_target: null,
    ...overrides,
  };
}

async function mountOpenProjection(
  overrides: Record<string, unknown> = {},
  viewHandler?: () => Response | Promise<Response>,
) {
  const router = fetchRouter({
    [`GET ${VIEW_PATH}`]: () => viewHandler?.() ?? jsonResponse(projectionPayload(overrides)),
    [`POST ${SUBMIT_PATH}`]: () => jsonResponse(receiptPayload()),
  });
  window.history.pushState(
    null,
    "",
    `/controlled/s02/react?request=${encodeURIComponent(REQUEST_ID)}`,
  );
  renderWithQuery(<AttachmentVersionPanel />);
  await waitFor(() =>
    expect(screen.getByTestId("integrator-projection-status")).toBeInTheDocument(),
  );
  return router;
}

describe("AttachmentVersionPanel (T04)", () => {
  it("asks for a request id when the URL carries none", () => {
    fetchRouter({});
    window.history.pushState(null, "", "/controlled/s02/react");
    renderWithQuery(<AttachmentVersionPanel />);
    expect(screen.getByTestId("integrator-no-request")).toHaveTextContent(
      "未指定补充材料请求",
    );
  });

  it("renders the minimized projection facts and never internal identifiers", async () => {
    const router = await mountOpenProjection();
    expect(screen.getByTestId("integrator-projection-status")).toHaveTextContent(
      "open",
    );
    expect(screen.getByTestId("integrator-projection-current")).toHaveTextContent(
      "是",
    );
    expect(screen.getByTestId("integrator-projection-material")).toHaveTextContent(
      "c-demo-financing-lease-vin/1",
    );
    expect(screen.getByTestId("integrator-projection-next-attachment")).toHaveTextContent(
      "2",
    );
    expect(screen.getByTestId("integrator-projection-next-revision")).toHaveTextContent(
      "1",
    );
    expect(screen.getByTestId("integrator-panel").textContent).not.toContain(
      "application_id",
    );
    expect(screen.getByTestId("integrator-panel").textContent).not.toContain(
      "finding_id",
    );
    expect(screen.getByTestId("integrator-panel").textContent).not.toContain(
      "requester_subject",
    );
    expect(router.calls).toHaveLength(1);
  });

  it("renders an explicit sanitized not-found state for the projection", async () => {
    const router = fetchRouter({
      [`GET ${VIEW_PATH}`]: () =>
        jsonResponse({ detail: { error: "S02_NOT_FOUND" } }, 404),
    });
    window.history.pushState(
      null,
      "",
      `/controlled/s02/react?request=${encodeURIComponent(REQUEST_ID)}`,
    );
    renderWithQuery(<AttachmentVersionPanel />);
    await waitFor(() =>
      expect(screen.getByTestId("integrator-projection-error")).toHaveTextContent(
        "请求未找到或无权访问",
      ),
    );
    expect(router.calls).toHaveLength(1);
    expect(screen.getByTestId("integrator-projection-error").textContent).not.toContain(
      "S02_NOT_FOUND",
    );
  });

  it("keeps submit disabled until a fresh successful projection loads and a valid envelope parses", async () => {
    await mountOpenProjection();
    const submit = () =>
      screen.getByRole("button", { name: "提交附件版本" }) as HTMLButtonElement;
    expect(submit().disabled).toBe(true);
    fireEvent.change(screen.getByTestId("integrator-envelope-input"), {
      target: { value: "{ not json" },
    });
    expect(screen.getByTestId("integrator-envelope-error")).toHaveTextContent(
      "不是有效 JSON",
    );
    expect(submit().disabled).toBe(true);
    fireEvent.change(screen.getByTestId("integrator-envelope-input"), {
      target: { value: JSON.stringify({ envelope_id: "envelope_t04integrator" }) },
    });
    expect(submit().disabled).toBe(false);
  });

  it("posts the exact parsed command with a semantic key and renders the receipt", async () => {
    const router = await mountOpenProjection();
    fireEvent.change(screen.getByTestId("integrator-envelope-input"), {
      target: { value: JSON.stringify({ envelope_id: "envelope_t04integrator" }) },
    });
    await userEvent.click(screen.getByRole("button", { name: "提交附件版本" }));
    await waitFor(() =>
      expect(screen.getByTestId("integrator-receipt")).toBeInTheDocument(),
    );
    const posts = router.calls.filter((call) => call.method === "POST");
    expect(posts).toHaveLength(1);
    expect(posts[0].body).toEqual({
      idempotency_key: expect.any(String),
      submission: { envelope_id: "envelope_t04integrator" },
    });
    expect(screen.getByTestId("integrator-receipt-disposition")).toHaveTextContent(
      "accepted",
    );
    expect(screen.getByTestId("integrator-receipt-request-status")).toHaveTextContent(
      "open",
    );
    // The known receipt refetches the projection; progress is never inferred
    // from the submitted batch.
    await waitFor(() =>
      expect(
        router.calls.filter((call) => call.method === "GET"),
      ).toHaveLength(2),
    );
  });

  it("locks edits and replays the exact command and key on an unknown transport outcome", async () => {
    let posts = 0;
    fetchRouter({
      [`GET ${VIEW_PATH}`]: () => jsonResponse(projectionPayload()),
      [`POST ${SUBMIT_PATH}`]: () => {
        posts += 1;
        return Promise.reject(new TypeError("fetch failed: connection reset"));
      },
    });
    window.history.pushState(
      null,
      "",
      `/controlled/s02/react?request=${encodeURIComponent(REQUEST_ID)}`,
    );
    renderWithQuery(<AttachmentVersionPanel />);
    await waitFor(() =>
      expect(screen.getByTestId("integrator-projection-status")).toBeInTheDocument(),
    );
    fireEvent.change(screen.getByTestId("integrator-envelope-input"), {
      target: { value: JSON.stringify({ envelope_id: "envelope_t04integrator" }) },
    });
    await userEvent.click(screen.getByRole("button", { name: "提交附件版本" }));
    await waitFor(() =>
      expect(screen.getByTestId("integrator-unknown")).toHaveTextContent(
        "结果未知",
      ),
    );
    expect(screen.getByRole("button", { name: "重试" })).toBeInTheDocument();
    const firstPosts = posts;
    expect(firstPosts).toBe(1);
    await userEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(posts).toBe(2));
    const bodyOf = (index: number) => {
      const mock = fetch as unknown as { mock: { calls: Array<[unknown, RequestInit | undefined]> } };
      const call = mock.mock.calls[index];
      return call[1]?.body !== undefined
        ? JSON.parse(String(call[1].body))
        : undefined;
    };
    expect(bodyOf(2)).toEqual(bodyOf(1));
  });

  it("rotates the key and requires an authoritative reload after a definitive rejection", async () => {
    const router = fetchRouter({
      [`GET ${VIEW_PATH}`]: () => jsonResponse(projectionPayload()),
      [`POST ${SUBMIT_PATH}`]: () =>
        jsonResponse(
          {
            detail: {
              error: "S02_REJECTED",
              reason_code: "intake.request_context_mismatch",
            },
          },
          409,
        ),
    });
    window.history.pushState(
      null,
      "",
      `/controlled/s02/react?request=${encodeURIComponent(REQUEST_ID)}`,
    );
    renderWithQuery(<AttachmentVersionPanel />);
    await waitFor(() =>
      expect(screen.getByTestId("integrator-projection-status")).toBeInTheDocument(),
    );
    fireEvent.change(screen.getByTestId("integrator-envelope-input"), {
      target: { value: JSON.stringify({ envelope_id: "envelope_t04integrator" }) },
    });
    await userEvent.click(screen.getByRole("button", { name: "提交附件版本" }));
    await waitFor(() =>
      expect(screen.getByTestId("integrator-reload-note")).toHaveTextContent(
        "intake.request_context_mismatch",
      ),
    );
    const submit = screen.getByRole("button", {
      name: "提交附件版本",
    }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    expect(router.calls.filter((call) => call.method === "POST")).toHaveLength(1);
  });

  it("disables submission for a terminal request and renders the stable status", async () => {
    await mountOpenProjection({
      status: "fulfilled",
      current: false,
    });
    expect(screen.getByTestId("integrator-projection-status")).toHaveTextContent(
      "fulfilled",
    );
    expect(screen.getByTestId("integrator-terminal-note")).toHaveTextContent(
      "该请求已结束",
    );
    const submit = screen.getByRole("button", {
      name: "提交附件版本",
    }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
  });
});
