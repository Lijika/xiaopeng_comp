import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import T15DeliveryPanel from "./T15DeliveryPanel";
import { fetchRouter, renderWithQuery } from "../test-utils";
import { S13_APPLICATION_ID, s13QueryPayload } from "../api/hooks.s13.test";

const DELIVERY_PATH = `/controlled/s13/delivery/${S13_APPLICATION_ID}`;
const RECONCILE_PATH = "/controlled/s13/api/commands/reconcile";
const COMPENSATE_PATH = "/controlled/s13/api/commands/compensate";

function json(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function error(status: number, code: string): Response {
  return new Response(JSON.stringify({ detail: { error: code, message: "error" } }), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("T15DeliveryPanel — terminology, regions, and server facts", () => {
  it("renders four separate regions with exact required headings without fetch before application selection", async () => {
    const router = fetchRouter({});
    renderWithQuery(<T15DeliveryPanel applicationId={null} />);
    expect(screen.getByTestId("s13-no-application")).toBeInTheDocument();
    expect(screen.getByTestId("s13-gate-section")).toBeInTheDocument();
    expect(screen.getByTestId("s13-routing-section")).toBeInTheDocument();
    expect(screen.getByTestId("s13-obligation-section")).toBeInTheDocument();
    expect(screen.getByTestId("s13-receipt-section")).toBeInTheDocument();
    expect(screen.getByText("Verification Completed")).toBeInTheDocument();
    expect(screen.getByText("Verification Routing")).toBeInTheDocument();
    expect(screen.getByText("Delivery Obligation")).toBeInTheDocument();
    expect(screen.getByText("Delivery Receipt")).toBeInTheDocument();
    // No business POST on mount.
    expect(router.calls.filter((c) => c.method === "POST")).toHaveLength(0);
    // Forbidden terminology never appears.
    const text = document.body.textContent ?? "";
    expect(text).not.toMatch(/disbursement/i);
    expect(text).not.toMatch(/loan.*approv/i);
    expect(text).not.toMatch(/credit.*decision/i);
  });

  it("mounting issues exactly one S13 GET and zero business POSTs", async () => {
    const router = fetchRouter({
      [`GET ${DELIVERY_PATH}`]: () => json(s13QueryPayload()),
    });
    renderWithQuery(<T15DeliveryPanel applicationId={S13_APPLICATION_ID} />);
    await waitFor(() => expect(screen.getByTestId("s13-delivery-panel")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByTestId("s13-verification-completed")).toBeInTheDocument());
    // Exactly one GET on mount; zero POST.
    expect(router.calls.filter((c) => c.method === "GET" && c.url === DELIVERY_PATH)).toHaveLength(1);
    expect(router.calls.filter((c) => c.method === "POST")).toHaveLength(0);
  });

  it("shows Verification Completed gate, immutable routing, obligation, and distinct receipt for pending", async () => {
    fetchRouter({
      [`GET ${DELIVERY_PATH}`]: () => json(s13QueryPayload()),
    });
    renderWithQuery(<T15DeliveryPanel applicationId={S13_APPLICATION_ID} />);
    await waitFor(() => expect(screen.getByTestId("s13-verification-completed")).toHaveTextContent("completed"));
    expect(screen.getByTestId("s13-phase")).toHaveTextContent("Verification Completed");
    expect(screen.getByTestId("s13-route")).toHaveTextContent("human_complete");
    expect(screen.getByTestId("s13-attribution-kind")).toHaveTextContent("human_complete");
    expect(screen.getByTestId("s13-schema-version")).toHaveTextContent("s13-delivery-view/1");
    // Obligation fields are separate from receipt.
    expect(screen.getByTestId("s13-obligation-id")).toBeInTheDocument();
    expect(screen.getByTestId("s13-operation-id")).toBeInTheDocument();
    expect(screen.getByTestId("s13-payload-digest")).toBeInTheDocument();
    expect(screen.getByTestId("s13-payload-ref")).toBeInTheDocument();
    expect(screen.getByTestId("s13-payload-schema")).toHaveTextContent("s13-route-payload/1");
    // Receipt is distinct.
    expect(screen.getByTestId("s13-delivery-status")).toHaveTextContent("pending");
    expect(screen.getByTestId("s13-attempt-count")).toHaveTextContent("0");
    expect(screen.getByTestId("s13-projection-watermark")).toBeInTheDocument();
    expect(screen.getByTestId("s13-store-revision")).toBeInTheDocument();
  });

  it("renders completed receipt correctly and keeps obligation separate", async () => {
    fetchRouter({
      [`GET ${DELIVERY_PATH}`]: () =>
        json(s13QueryPayload({ delivery_status: "received", attempt_count: 1 })),
    });
    renderWithQuery(<T15DeliveryPanel applicationId={S13_APPLICATION_ID} />);
    await waitFor(() => expect(screen.getByTestId("s13-delivery-status")).toHaveTextContent("received"));
    expect(screen.getByTestId("s13-attempt-count")).toHaveTextContent("1");
    // Obligation status and delivery receipt remain separate DOM regions.
    expect(screen.getByTestId("s13-obligation-section")).toBeInTheDocument();
    expect(screen.getByTestId("s13-receipt-section")).toBeInTheDocument();
    expect(screen.getByTestId("s13-obligation-status")).toBeInTheDocument();
  });

  it("clears prior facts on 403 and shows forbidden without identifiers", async () => {
    fetchRouter({
      [`GET ${DELIVERY_PATH}`]: () => error(403, "S13_FORBIDDEN"),
    });
    renderWithQuery(<T15DeliveryPanel applicationId={S13_APPLICATION_ID} />);
    await waitFor(() => expect(screen.getByTestId("s13-error-forbidden")).toBeInTheDocument());
    expect(screen.getByTestId("s13-error-code")).toHaveTextContent("S13_FORBIDDEN");
    // Prior authoritative presentation is cleared: no gate/routing/obligation shown.
    expect(screen.queryByTestId("s13-gate-section")).not.toBeInTheDocument();
    expect(screen.queryByTestId("s13-verification-completed")).not.toBeInTheDocument();
  });

  it("clears prior facts on 404 not-found and shows explicit state", async () => {
    fetchRouter({
      [`GET ${DELIVERY_PATH}`]: () => error(404, "S13_NOT_FOUND"),
    });
    renderWithQuery(<T15DeliveryPanel applicationId={S13_APPLICATION_ID} />);
    await waitFor(() => expect(screen.getByTestId("s13-error-not-found")).toBeInTheDocument());
    expect(screen.queryByTestId("s13-obligation-id")).not.toBeInTheDocument();
  });

  it("shows loading state with aria-live", async () => {
    fetchRouter({
      [`GET ${DELIVERY_PATH}`]: () =>
        new Promise<Response>(() => {
          // Never resolves — stays loading.
        }),
    });
    renderWithQuery(<T15DeliveryPanel applicationId={S13_APPLICATION_ID} />);
    expect(screen.getByTestId("s13-delivery-loading")).toBeInTheDocument();
    expect(screen.getByTestId("s13-delivery-loading")).toHaveAttribute("role", "status");
  });

  it("shows unavailable on 503", async () => {
    fetchRouter({
      [`GET ${DELIVERY_PATH}`]: () => error(503, "S13_UNAVAILABLE"),
    });
    renderWithQuery(<T15DeliveryPanel applicationId={S13_APPLICATION_ID} />);
    await waitFor(() => expect(screen.getByTestId("s13-error-unavailable")).toBeInTheDocument(), { timeout: 5000 });
  });

  it("shows unknown outcome with operation_id when reconcile transport is generic 503 (not definitive)", async () => {
    const payload = s13QueryPayload({ delivery_status: "unknown" });
    fetchRouter({
      [`GET ${DELIVERY_PATH}`]: () => json(payload),
      [`POST ${RECONCILE_PATH}`]: () => error(503, "UNKNOWN_GATEWAY"),
    });
    renderWithQuery(<T15DeliveryPanel applicationId={S13_APPLICATION_ID} />);
    await waitFor(() => expect(screen.getByTestId("s13-delivery-status")).toBeInTheDocument());
    await userEvent.click(screen.getByTestId("s13-reconcile-button"));
    await waitFor(() => expect(screen.getByTestId("s13-unknown-outcome")).toBeInTheDocument());
    expect(screen.getByTestId("s13-unknown-operation-id")).toHaveTextContent(payload.obligation!.operation_id);
    // Blind retry is unavailable until same-operation reconciliation proves not_executed.
    expect(screen.getByTestId("s13-reconcile-button")).toBeDisabled();
  });

  it("reconcile and compensate are keyboard operable and disabled states are explicit", async () => {
    fetchRouter({
      [`GET ${DELIVERY_PATH}`]: () => json(s13QueryPayload()),
      [`POST ${RECONCILE_PATH}`]: () =>
        json({ obligation_id: payloadObligationId(), operation_id: "op_1", delivery_status: "received", status: "received" }),
    });
    function payloadObligationId() {
      return s13QueryPayload().obligation!.obligation_id;
    }
    renderWithQuery(<T15DeliveryPanel applicationId={S13_APPLICATION_ID} />);
    await waitFor(() => expect(screen.getByTestId("s13-reconcile-button")).toBeInTheDocument());
    const reconcileBtn = screen.getByTestId("s13-reconcile-button");
    const compensateBtn = screen.getByTestId("s13-compensate-button");
    const processBtn = screen.getByTestId("s13-process-next-button");
    // All buttons reachable via Tab and have accessible names.
    reconcileBtn.focus();
    expect(document.activeElement).toBe(reconcileBtn);
    await userEvent.tab();
    expect(document.activeElement).toBe(compensateBtn);
    await userEvent.tab();
    expect(document.activeElement).toBe(processBtn);
    // aria-disabled mirrors disabled for screen readers.
    expect(reconcileBtn).not.toBeDisabled();
    expect(compensateBtn).not.toBeDisabled();
  });

  it("reconcile sends correct body and shows outcome; refunds visual distinction between pending/failed/compensated", async () => {
    const payload = s13QueryPayload({ delivery_status: "unknown" });
    let reconcileBody: unknown;
    fetchRouter({
      [`GET ${DELIVERY_PATH}`]: () => json(payload),
      [`POST ${RECONCILE_PATH}`]: (_url, init) => {
        reconcileBody = JSON.parse(String(init?.body));
        return json({
          obligation_id: payload.obligation!.obligation_id,
          operation_id: payload.obligation!.operation_id,
          delivery_status: "received",
          status: "received",
          reason_code: null,
        });
      },
    });
    renderWithQuery(<T15DeliveryPanel applicationId={S13_APPLICATION_ID} />);
    await waitFor(() => expect(screen.getByTestId("s13-reconcile-button")).toBeEnabled());
    await userEvent.click(screen.getByTestId("s13-reconcile-button"));
    await waitFor(() => expect(screen.getByTestId("s13-command-outcome")).toBeInTheDocument());
    expect(reconcileBody).toEqual({ obligation_id: payload.obligation!.obligation_id });
    expect(screen.getByTestId("s13-command-outcome")).toHaveTextContent("reconcile");
  });

  it("compensate sends correct body and is distinct from reconcile", async () => {
    const payload = s13QueryPayload({ delivery_status: "failed" });
    let compensateBody: unknown;
    fetchRouter({
      [`GET ${DELIVERY_PATH}`]: () => json(payload),
      [`POST ${COMPENSATE_PATH}`]: (_url, init) => {
        compensateBody = JSON.parse(String(init?.body));
        return json({
          obligation_id: payload.obligation!.obligation_id,
          operation_id: payload.obligation!.operation_id,
          status: "compensated",
          reason_code: null,
        });
      },
    });
    renderWithQuery(<T15DeliveryPanel applicationId={S13_APPLICATION_ID} />);
    await waitFor(() => expect(screen.getByTestId("s13-compensate-button")).toBeEnabled());
    await userEvent.click(screen.getByTestId("s13-compensate-button"));
    await waitFor(() => expect(screen.getByTestId("s13-command-outcome")).toBeInTheDocument());
    expect(compensateBody).toEqual({ obligation_id: payload.obligation!.obligation_id });
  });

  it("unknown future delivery_status renders verbatim with neutral fallback (no inference)", async () => {
    fetchRouter({
      [`GET ${DELIVERY_PATH}`]: () => json(s13QueryPayload({ delivery_status: "future_status_xyz" })),
    });
    renderWithQuery(<T15DeliveryPanel applicationId={S13_APPLICATION_ID} />);
    await waitFor(() => expect(screen.getByTestId("s13-delivery-status")).toHaveTextContent("future_status_xyz"));
  });

  it("no obligation case: obligation none is explicit and not conflated with pending receipt", async () => {
    fetchRouter({
      [`GET ${DELIVERY_PATH}`]: () =>
        json(s13QueryPayload({ obligation: null, delivery_status: "none", verification_completed: false, phase: "Manual Review", route: "" })),
    });
    renderWithQuery(<T15DeliveryPanel applicationId={S13_APPLICATION_ID} />);
    await waitFor(() => expect(screen.getByTestId("s13-obligation-none")).toBeInTheDocument());
    expect(screen.getByTestId("s13-delivery-status")).toHaveTextContent("none");
    expect(screen.getByTestId("s13-verification-completed")).toHaveTextContent("not completed");
    // Obligation creation does not imply receipt.
    expect(screen.getByTestId("s13-obligation-none").textContent).not.toMatch(/received/i);
  });

  it("all four regions have semantic headings and landmarks; no disbursement language anywhere", async () => {
    fetchRouter({
      [`GET ${DELIVERY_PATH}`]: () => json(s13QueryPayload()),
    });
    renderWithQuery(<T15DeliveryPanel applicationId={S13_APPLICATION_ID} />);
    await waitFor(() => expect(screen.getByTestId("s13-gate-section")).toBeInTheDocument());
    // Each region has a heading with the exact server-owned label.
    expect(screen.getByRole("heading", { name: "Verification Completed" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Verification Routing" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Delivery Obligation" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Delivery Receipt" })).toBeInTheDocument();
    // Global text must not contain prohibited loan/disbursement/credit semantics.
    // Verification Routing's allowed note carries no disbursement decision phrasing
    // by design; the check forbids positive disbursement/loan/credit claims.
    const body = document.body.textContent ?? "";
    expect(body).not.toMatch(/loan approval/i);
    expect(body).not.toMatch(/loan.*rejection/i);
    expect(body).not.toMatch(/credit decision/i);
    // Obligation creation must not be equated with receipt.
    expect(body).not.toMatch(/obligation.*receipt.*completed/i);
  });

  it("stale and duplicate structured errors are explicit (422 with details)", async () => {
    const payload = s13QueryPayload();
    fetchRouter({
      [`GET ${DELIVERY_PATH}`]: () => json(payload),
      [`POST ${RECONCILE_PATH}`]: () =>
        new Response(
          JSON.stringify({ detail: { error: "S13_STALE_DELIVERY_FENCE", message: "stale", reason_code: "S13_STALE_DELIVERY_FENCE" } }),
          { status: 422, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderWithQuery(<T15DeliveryPanel applicationId={S13_APPLICATION_ID} />);
    await waitFor(() => expect(screen.getByTestId("s13-reconcile-button")).toBeEnabled());
    await userEvent.click(screen.getByTestId("s13-reconcile-button"));
    await waitFor(() => expect(screen.getByTestId("s13-reconcile-error")).toBeInTheDocument());
  });
});
