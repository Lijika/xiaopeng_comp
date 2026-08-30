import { cleanup, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import App from "./App";
import { fetchRouter, renderWithQuery } from "./test-utils";

beforeEach(() => {
  window.history.replaceState(null, "", "/controlled/s01");
  window.localStorage.clear();
});

describe("queue shell (App)", () => {
  it("mounts the human-review shell without the empty S01 queue", async () => {
    const router = fetchRouter({});
    renderWithQuery(<App />);
    await waitFor(() =>
      expect(screen.getByTestId("review-decision-panel")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("queue-panel")).not.toBeInTheDocument();
    expect(
      router.calls.filter((call) => call.url.includes("/queries/queue")),
    ).toHaveLength(0);
  });

  it("mounts the Integrator shell by pathname and never issues the S01 queue read", async () => {
    const router = fetchRouter({});
    window.history.pushState(null, "", "/controlled/s02/react");
    renderWithQuery(<App />);
    await waitFor(() =>
      expect(screen.getByTestId("supplement-empty")).toBeInTheDocument(),
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
    const router = fetchRouter({});
    window.history.pushState(null, "", "/controlled/s05/react");
    renderWithQuery(<App />);
    await waitFor(() =>
      expect(screen.getByTestId("approver-boundary-gate")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("approver-boundary-gate")).toHaveTextContent("S05");
    expect(screen.queryByTestId("queue-panel")).not.toBeInTheDocument();
    expect(
      router.calls.filter((call) => call.url.includes("/queries/queue")),
    ).toHaveLength(0);
  });

  it("mounts the Reviewer workbench on the canonical /controlled/s01 and its /controlled/s01/react alias", async () => {
    for (const pathname of ["/controlled/s01", "/controlled/s01/react"]) {
      window.history.pushState(null, "", pathname);
      const view = renderWithQuery(<App />);
      await waitFor(() =>
        expect(screen.getByTestId("review-decision-panel")).toBeInTheDocument(),
      );
      expect(screen.queryByTestId("integrator-panel")).not.toBeInTheDocument();
      expect(screen.getByTestId("boundary-track")).toHaveTextContent("C-DEMO");
      view.unmount();
    }
  });

  it("mounts the demo shell on the canonical root / and the /demo/react alias and never issues controlled reads", async () => {
    for (const pathname of ["/", "/demo/react"]) {
      const router = fetchRouter({});
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
      expect(screen.queryByTestId("queue-panel")).not.toBeInTheDocument();
      expect(
        router.calls.filter((call) => call.url.includes("/controlled/")),
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
    expect(router.calls.map((call) => call.url).filter((url) => url !== "/api/demo/directory" && url !== "/api/demo/case")).toEqual([
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
    expect(router.calls.map((call) => call.url).filter((url) => url !== "/api/demo/directory" && url !== "/api/demo/case")).toEqual([
      "/controlled/s12/plans",
    ]);
  });
});

describe("delivery console shell (T15)", () => {
  it("mounts the S13 shell on /controlled/s13 without querying S13 delivery", async () => {
    const router = fetchRouter({});
    window.history.pushState(null, "", "/controlled/s13");
    renderWithQuery(<App />);
    await waitFor(() =>
      expect(screen.getByTestId("s13-delivery-panel")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("s13-boundary-gate")).toHaveTextContent("S13");
    expect(screen.getByTestId("s13-no-application")).toBeInTheDocument();
    expect(
      router.calls.filter((call) => call.url.includes("/controlled/s13/delivery")),
    ).toHaveLength(0);
  });
});
describe("S14 lifecycle shells (App)", () => {
  it("mounts the cancellation workbench on the S14 canonical route and alias", () => {
    for (const pathname of ["/controlled/s14", "/controlled/s14/react"]) {
      window.history.pushState(null, "", `${pathname}?application=app_x`);
      renderWithQuery(<App />);
      expect(screen.getByTestId("s14-boundary-gate")).toHaveTextContent("S14");
      expect(screen.getByText("请先在核验页上传并核验一笔申请。")).toBeInTheDocument();
      cleanup();
    }
  });

  it("mounts the settlement console on its canonical route and alias", () => {
    for (const pathname of [
      "/controlled/s14/settlement",
      "/controlled/s14/settlement/react",
    ]) {
      window.history.pushState(null, "", `${pathname}?application=app_x`);
      renderWithQuery(<App />);
      expect(
        screen.getByTestId("s14-settlement-boundary-gate"),
      ).toHaveTextContent("S14");
      expect(screen.getByText("请先在核验页上传并核验一笔申请。")).toBeInTheDocument();
      cleanup();
    }
  });
});
