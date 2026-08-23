import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import T15DeliveryPanel from "./T15DeliveryPanel";
import { fetchRouter, renderWithQuery } from "../test-utils";
import {
  S13_APPLICATION_ID,
  S13_OPERATION_ID,
  s13QueryPayload,
} from "../test-fixtures/s13";

const DELIVERY_PATH = `/controlled/s13/delivery/${S13_APPLICATION_ID}`;

function json(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    headers: { "Content-Type": "application/json" },
  });
}

function error(status: number, code: string): Response {
  return new Response(
    JSON.stringify({ detail: { error: code, message: "request failed" } }),
    { status, headers: { "Content-Type": "application/json" } },
  );
}

describe("T15DeliveryPanel", () => {
  it.each([null, ""] as const)(
    "renders the four empty regions and issues zero requests for %s",
    (applicationId) => {
      const router = fetchRouter({});
      const rendered = renderWithQuery(
        <T15DeliveryPanel applicationId={applicationId} />,
      );

      expect(screen.getByTestId("s13-no-application")).toHaveAttribute(
        "role",
        "status",
      );
      for (const name of [
        "Verification Completed",
        "Verification Routing",
        "Delivery Obligation",
        "Delivery Receipt",
      ]) {
        expect(screen.getByRole("heading", { name })).toBeInTheDocument();
      }
      expect(rendered.baseElement.querySelectorAll(".panel .panel")).toHaveLength(0);
      expect(router.calls).toHaveLength(0);
    },
  );

  it("renders immutable routing provenance, obligation, and pending receipt separately", async () => {
    const router = fetchRouter({
      [`GET ${DELIVERY_PATH}`]: () => json(s13QueryPayload()),
    });
    renderWithQuery(<T15DeliveryPanel applicationId={S13_APPLICATION_ID} />);

    await waitFor(() =>
      expect(screen.getByTestId("s13-verification-completed")).toHaveTextContent(
        "completed",
      ),
    );
    expect(screen.getByTestId("s13-phase")).toHaveTextContent(
      "Verification Completed",
    );
    expect(screen.getByTestId("s13-route")).toHaveTextContent("human_complete");
    expect(screen.getByTestId("s13-attribution-kind")).toHaveTextContent(
      "human",
    );
    const history = screen.getByTestId("s13-routing-history-entry");
    expect(within(history).getByText("lifecycle_event_t15_0001")).toBeVisible();
    expect(within(history).getByText("decision_t15_0001")).toBeVisible();
    expect(within(history).getByText("review_work_t15_0001")).toBeVisible();
    expect(within(history).getByText("checker-t15")).toBeVisible();
    expect(within(history).getAllByText(S13_OPERATION_ID)).toHaveLength(1);

    expect(screen.getByTestId("s13-obligation-id")).toBeVisible();
    expect(screen.getByTestId("s13-operation-id")).toHaveTextContent(
      S13_OPERATION_ID,
    );
    expect(screen.getByTestId("s13-payload-ref")).toBeVisible();
    expect(screen.getByTestId("s13-payload-digest")).toBeVisible();
    expect(screen.getByTestId("s13-delivery-status")).toHaveTextContent(
      "pending",
    );
    expect(screen.getByTestId("s13-attempt-count")).toHaveTextContent("0");
    expect(router.calls.filter((call) => call.method === "POST")).toHaveLength(
      0,
    );
    expect(
      screen.queryByRole("button", { name: /process|reconcile|compensate/i }),
    ).not.toBeInTheDocument();
  });

  it.each(["failed", "unavailable", "received", "future_status_xyz"])(
    "preserves delivery status %s verbatim",
    async (deliveryStatus) => {
      fetchRouter({
        [`GET ${DELIVERY_PATH}`]: () =>
          json(s13QueryPayload({ delivery_status: deliveryStatus })),
      });
      renderWithQuery(<T15DeliveryPanel applicationId={S13_APPLICATION_ID} />);

      await waitFor(() =>
        expect(screen.getByTestId("s13-delivery-status")).toHaveTextContent(
          deliveryStatus,
        ),
      );
    },
  );

  it("keeps an absent obligation distinct from receipt progress", async () => {
    fetchRouter({
      [`GET ${DELIVERY_PATH}`]: () =>
        json(
          s13QueryPayload({
            obligation: null,
            routing_history: [],
            delivery_status: "none",
            verification_completed: false,
            phase: "Manual Review",
            route: "",
          }),
        ),
    });
    renderWithQuery(<T15DeliveryPanel applicationId={S13_APPLICATION_ID} />);

    await waitFor(() =>
      expect(screen.getByTestId("s13-obligation-none")).toBeVisible(),
    );
    expect(screen.getByTestId("s13-routing-history-empty")).toBeVisible();
    expect(screen.getByTestId("s13-delivery-status")).toHaveTextContent("none");
    expect(screen.getByTestId("s13-verification-completed")).toHaveTextContent(
      "not completed",
    );
  });

  it("renders one live loading status", () => {
    fetchRouter({
      [`GET ${DELIVERY_PATH}`]: () => new Promise<Response>(() => {}),
    });
    renderWithQuery(<T15DeliveryPanel applicationId={S13_APPLICATION_ID} />);

    expect(screen.getByRole("status")).toBe(
      screen.getByTestId("s13-delivery-loading"),
    );
  });

  it.each([
    [403, "S13_FORBIDDEN", "s13-error-forbidden"],
    [404, "S13_NOT_FOUND", "s13-error-not-found"],
    [503, "S13_UNAVAILABLE", "s13-error-unavailable"],
  ] as const)(
    "clears facts and renders one alert for %s %s",
    async (status, code, testId) => {
      fetchRouter({
        [`GET ${DELIVERY_PATH}`]: () => error(status, code),
      });
      renderWithQuery(<T15DeliveryPanel applicationId={S13_APPLICATION_ID} />);

      await waitFor(() => expect(screen.getByTestId(testId)).toBeVisible(), {
        timeout: 5_000,
      });
      expect(screen.getAllByRole("alert")).toHaveLength(1);
      expect(screen.getByTestId("s13-error-code")).toHaveTextContent(code);
      expect(screen.getByTestId("s13-reload")).toBeEnabled();
      expect(screen.queryByTestId("s13-obligation-id")).not.toBeInTheDocument();
      expect(screen.queryByTestId("s13-routing-history")).not.toBeInTheDocument();
    },
  );

  it("reloads the authoritative query from the keyboard-operable control", async () => {
    let calls = 0;
    fetchRouter({
      [`GET ${DELIVERY_PATH}`]: () => {
        calls += 1;
        return calls === 1
          ? error(404, "S13_NOT_FOUND")
          : json(s13QueryPayload());
      },
    });
    renderWithQuery(<T15DeliveryPanel applicationId={S13_APPLICATION_ID} />);

    await waitFor(() => expect(screen.getByTestId("s13-reload")).toBeEnabled());
    await userEvent.click(screen.getByTestId("s13-reload"));
    await waitFor(() =>
      expect(screen.getByTestId("s13-verification-completed")).toHaveTextContent(
        "completed",
      ),
    );
    expect(calls).toBe(2);
  });

  it("uses the exact domain terms without decision language", async () => {
    fetchRouter({
      [`GET ${DELIVERY_PATH}`]: () => json(s13QueryPayload()),
    });
    renderWithQuery(<T15DeliveryPanel applicationId={S13_APPLICATION_ID} />);
    await screen.findByTestId("s13-routing-section");

    const text = document.body.textContent ?? "";
    expect(text).not.toMatch(/loan approval|loan rejection|credit decision/i);
    expect(text).not.toMatch(/disbursement decision/i);
  });
});
