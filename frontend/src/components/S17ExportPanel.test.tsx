import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import S17ExportPanel from "./S17ExportPanel";
import { fetchRouter, renderWithQuery } from "../test-utils";

const REQUEST_ID = "s17req_test_00000001";
const PREVIEW_PATH = "/controlled/s17/api/exports/preview";
const REQUEST_PATH = `/controlled/s17/api/exports/${REQUEST_ID}`;
const APPROVE_PATH = `${REQUEST_PATH}/approve`;
const COMMIT_PATH = `${REQUEST_PATH}/commit`;
const PROCESS_PATH = "/controlled/s17/api/process";
const ACCESS_PATH = `${REQUEST_PATH}/access`;
const CONFIRM_PATH = `${REQUEST_PATH}/confirm`;
const REVOKE_PATH = `${REQUEST_PATH}/revoke`;
const RECEIPT_PATH = `${REQUEST_PATH}/receipt`;

afterEach(() => {
  window.history.replaceState(null, "", "/");
});

function queryPayload(status = "previewed") {
  return {
    schema_version: "s17-query/1",
    request_id: REQUEST_ID,
    status,
    preview_digest: "a".repeat(64),
    scope_fingerprint: "b".repeat(64),
    purpose: "audit_response",
    recipient_id: "recipient-1",
    classification: "confidential",
    expiry: 1_900_000_000,
    source_revisions: { s01: 3 },
    policy_digest: "c".repeat(64),
    package_id: status === "previewed" || status === "approved" || status === "queued" ? null : "pkg-1",
    package_digest: status === "delivered" || status === "accessed" || status === "confirmed" ? "d".repeat(64) : null,
    watermark_id: status === "delivered" || status === "accessed" || status === "confirmed" ? "e".repeat(64) : null,
    delivery_status: status === "delivered" || status === "accessed" || status === "confirmed" ? "delivered" : null,
    attempt: 1,
    operation_id: "op-1",
  };
}

function previewPayload() {
  return {
    status: "previewed",
    request_id: REQUEST_ID,
    preview_digest: "a".repeat(64),
    scope_fingerprint: "b".repeat(64),
    field_count: 1,
    artifact_count: 1,
    watermark_plan: { scheme: "s17-watermark/1" },
  };
}

describe("S17ExportPanel", () => {
  it("lets the requester define and then freezes the exact request", async () => {
    const router = fetchRouter({
      [`POST ${PREVIEW_PATH}`]: () => router.jsonResponse(previewPayload()),
      [`GET ${REQUEST_PATH}`]: () => router.jsonResponse(queryPayload()),
    });
    renderWithQuery(<S17ExportPanel />);
    const user = userEvent.setup();
    await user.clear(screen.getByTestId("s17-purpose"));
    await user.type(screen.getByTestId("s17-purpose"), "audit_response");
    await user.clear(screen.getByTestId("s17-recipient"));
    await user.type(screen.getByTestId("s17-recipient"), "recipient-1");
    await user.clear(screen.getByTestId("s17-fields"));
    await user.type(screen.getByTestId("s17-fields"), "application_fingerprint");
    await user.clear(screen.getByTestId("s17-artifacts"));
    await user.type(screen.getByTestId("s17-artifacts"), "route_metadata");
    await user.click(screen.getByTestId("s17-preview-button"));
    await screen.findByTestId("s17-export-state");
    expect(router.calls.find((call) => call.url === PREVIEW_PATH)?.body).toMatchObject({
      purpose: "audit_response",
      recipient_id: "recipient-1",
      fields: ["application_fingerprint"],
      artifacts: ["route_metadata"],
    });
    expect(screen.getByTestId("s17-purpose")).toBeDisabled();
    expect(screen.getByTestId("s17-recipient")).toBeDisabled();
    expect(screen.getByTestId("s17-request-summary")).toHaveTextContent("audit_response");
  });

  it("requires a separate approver token and sends it through the dedicated header", async () => {
    const router = fetchRouter({
      [`POST ${PREVIEW_PATH}`]: () => router.jsonResponse(previewPayload()),
      [`GET ${REQUEST_PATH}`]: () => router.jsonResponse(queryPayload()),
      [`POST ${APPROVE_PATH}`]: () => router.jsonResponse({ status: "approved", request_id: REQUEST_ID }),
    });
    renderWithQuery(<S17ExportPanel />);
    const user = userEvent.setup();
    await user.click(screen.getByTestId("s17-preview-button"));
    await screen.findByTestId("s17-approver-token");
    expect(screen.getByTestId("s17-approve-button")).toBeDisabled();
    await user.type(screen.getByTestId("s17-approver-token"), "independent-token");
    await user.click(screen.getByTestId("s17-approve-button"));
    await waitFor(() => expect(router.calls.some((call) => call.url === APPROVE_PATH)).toBe(true));
    const approveCall = router.calls.find((call) => call.url === APPROVE_PATH);
    expect(approveCall?.body).not.toHaveProperty("approverToken");
    expect(screen.getByTestId("s17-approver-token")).toHaveValue("");
  });

  it("drives server generation, one-time access, confirmation and value-free receipt", async () => {
    let status = "previewed";
    const router = fetchRouter({
      [`POST ${PREVIEW_PATH}`]: () => router.jsonResponse(previewPayload()),
      [`GET ${REQUEST_PATH}`]: () => router.jsonResponse(queryPayload(status)),
      [`POST ${APPROVE_PATH}`]: () => { status = "approved"; return router.jsonResponse({ status, request_id: REQUEST_ID }); },
      [`POST ${COMMIT_PATH}`]: () => { status = "queued"; return router.jsonResponse({ status, request_id: REQUEST_ID, job_id: "job-1" }); },
      [`POST ${PROCESS_PATH}`]: () => { status = "delivered"; return router.jsonResponse({ status, request_id: REQUEST_ID, package_id: "pkg-1" }); },
      [`POST ${ACCESS_PATH}`]: () => { status = "accessed"; return router.jsonResponse({ status, request_id: REQUEST_ID, watermark_id: "e".repeat(64) }); },
      [`POST ${CONFIRM_PATH}`]: () => { status = "confirmed"; return router.jsonResponse({ status, request_id: REQUEST_ID }); },
      [`GET ${RECEIPT_PATH}`]: () => router.jsonResponse({ schema_version: "s17-receipt/1", receipt_id: "receipt-1", status, package_digest: "d".repeat(64), delivery_status: "confirmed", attempt: 1, expiry: 1_900_000_000, cleanup_result: "none", replayed: false }),
    });
    renderWithQuery(<S17ExportPanel />);
    const user = userEvent.setup();
    await user.click(screen.getByTestId("s17-preview-button"));
    await screen.findByTestId("s17-approver-token");
    await user.type(screen.getByTestId("s17-approver-token"), "approver-token");
    await user.click(screen.getByTestId("s17-approve-button"));
    await user.click(screen.getByTestId("s17-commit-confirm"));
    await user.click(screen.getByTestId("s17-commit-button"));
    await user.type(screen.getByTestId("s17-worker-token"), "worker-token");
    await user.click(screen.getByTestId("s17-process-button"));
    await waitFor(() => expect(screen.getByTestId("s17-delivery-status")).toHaveTextContent("delivered"));
    await user.type(screen.getByTestId("s17-delivery-token"), "one-time-token");
    await user.click(screen.getByTestId("s17-access-button"));
    await waitFor(() => expect(screen.getByTestId("s17-request-status")).toHaveTextContent("accessed"));
    await user.click(screen.getByTestId("s17-confirm-button"));
    await user.click(screen.getByTestId("s17-receipt-button"));
    await screen.findByTestId("s17-export-receipt");
    expect(screen.getByTestId("s17-receipt-cleanup")).toHaveTextContent("none");
    expect(screen.queryByText("one-time-token")).toBeNull();
  });

  it("renders typed denial and expiry errors without server messages", async () => {
    const router = fetchRouter({
      [`POST ${PREVIEW_PATH}`]: () => router.errorResponse(409, "S17_BLOCKED", "S17_TOKEN_EXPIRED"),
    });
    renderWithQuery(<S17ExportPanel />);
    await userEvent.click(screen.getByTestId("s17-preview-button"));
    await screen.findByTestId("s17-error");
    expect(screen.getByTestId("s17-error-code")).toHaveTextContent("S17_BLOCKED");
    expect(screen.getByTestId("s17-error-reason")).toHaveTextContent("S17_TOKEN_EXPIRED");
    expect(screen.queryByText("secret")).toBeNull();
  });

  it("shows authoritative revoke and generation cleanup states", async () => {
    let status = "previewed";
    const router = fetchRouter({
      [`POST ${PREVIEW_PATH}`]: () => router.jsonResponse(previewPayload()),
      [`GET ${REQUEST_PATH}`]: () => router.jsonResponse(queryPayload(status)),
      [`POST ${APPROVE_PATH}`]: () => { status = "approved"; return router.jsonResponse({ status, request_id: REQUEST_ID }); },
      [`POST ${COMMIT_PATH}`]: () => { status = "queued"; return router.jsonResponse({ status, request_id: REQUEST_ID }); },
      [`POST ${PROCESS_PATH}`]: () => { status = "failed"; return router.jsonResponse({ status, request_id: REQUEST_ID, reason_code: "S17_PROVIDER_UNAVAILABLE" }); },
      [`POST ${REVOKE_PATH}`]: () => { status = "revoked"; return router.jsonResponse({ status, request_id: REQUEST_ID }); },
      [`GET ${RECEIPT_PATH}`]: () => router.jsonResponse({ schema_version: "s17-receipt/1", receipt_id: "receipt-cleaned", status, delivery_status: null, package_digest: null, attempt: 1, expiry: 1_900_000_000, cleanup_result: "cleaned", replayed: false }),
    });
    renderWithQuery(<S17ExportPanel />);
    const user = userEvent.setup();
    await user.click(screen.getByTestId("s17-preview-button"));
    await screen.findByTestId("s17-approver-token");
    await user.type(screen.getByTestId("s17-approver-token"), "approver-token");
    await user.click(screen.getByTestId("s17-approve-button"));
    await user.click(screen.getByTestId("s17-commit-confirm"));
    await user.click(screen.getByTestId("s17-commit-button"));
    await user.type(screen.getByTestId("s17-worker-token"), "worker-token");
    await user.click(screen.getByTestId("s17-process-button"));
    await screen.findByTestId("s17-generation-failure");
    expect(screen.getByTestId("s17-generation-failure")).toHaveTextContent("S17_PROVIDER_UNAVAILABLE");
    await user.click(screen.getByTestId("s17-receipt-button"));
    await screen.findByTestId("s17-export-receipt");
    expect(screen.getByTestId("s17-receipt-cleanup")).toHaveTextContent("cleaned");
    await user.click(screen.getByTestId("s17-deny-button"));
    await waitFor(() => expect(screen.getByTestId("s17-approval-status")).toHaveTextContent("已拒绝"));
  });
});
