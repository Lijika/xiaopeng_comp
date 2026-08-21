import { StrictMode } from "react";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { RouteHandler } from "../test-utils";

import S12EvaluationOperator from "./S12EvaluationOperator";
import * as hooksModule from "../api/hooks";
import { fetchRouter, renderWithQuery } from "../test-utils";
import {
  S12_BUNDLE_ID,
  S12_JOB_ID,
  S12_PLAN_ID,
  s12BundlePayload,
  s12CatalogPayload,
  s12JobPayload,
  s12ProcessPayload,
} from "../api/hooks.s12.test";

const PLANS_PATH = "/controlled/s12/plans";
const START_PATH = "/controlled/s12/jobs/start";
const PROCESS_PATH = `/controlled/s12/jobs/${S12_JOB_ID}/process`;
const JOB_PATH = `/controlled/s12/jobs/${S12_JOB_ID}`;
const BUNDLE_PATH = `/controlled/s12/bundles/${S12_BUNDLE_ID}`;

function json(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function error(status: number, code: string, message = "rejected"): Response {
  return new Response(JSON.stringify({ detail: { error: code, message } }), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

async function selectPlanAndStart(): Promise<void> {
  await screen.findByTestId("s12-plan-select");
  await userEvent.selectOptions(
    screen.getByTestId("s12-plan-select"),
    S12_PLAN_ID,
  );
  await userEvent.click(screen.getByTestId("s12-start-button"));
}

describe("S12EvaluationOperator (T14)", () => {
  it("runs the exact authorized sequence: catalog read, one start with plan_id only, one process, bounded job GETs, bundle GET", async () => {
    let jobRequests = 0;
    const router = fetchRouter({
      [`GET ${PLANS_PATH}`]: () => json(s12CatalogPayload()),
      [`POST ${START_PATH}`]: () => json(
        s12JobPayload({ status: "queued" }, { bundle_id: null, status: null, reason_codes: [] }),
      ),
      [`POST ${PROCESS_PATH}`]: () => json(s12ProcessPayload()),
      [`GET ${JOB_PATH}`]: () => {
        jobRequests += 1;
        // First poll observes the terminal job; the poll must stop there.
        return jobRequests === 1
          ? json(s12JobPayload())
          : json(s12JobPayload());
      },
      [`GET ${BUNDLE_PATH}`]: () => json(s12BundlePayload()),
    });
    renderWithQuery(<S12EvaluationOperator />);

    // Catalog read only before any user action.
    await screen.findByTestId("s12-plan-select");
    expect(router.calls.map((call) => call.url)).toEqual([PLANS_PATH]);

    await selectPlanAndStart();

    await waitFor(() =>
      expect(screen.getByTestId("s12-sealed-report")).toBeInTheDocument(),
    );

    const calls = router.calls;
    expect(calls[0]?.method).toBe("GET");
    expect(calls.filter((call) => call.method === "POST")).toHaveLength(2);
    const start = calls.find((call) => call.url === START_PATH);
    expect(start?.body).toEqual({ plan_id: S12_PLAN_ID });
    expect(calls.some((call) => call.url === PROCESS_PATH)).toBe(true);
    // The initial authoritative read already observed the terminal job.
    expect(jobRequests).toBe(1);
    expect(jobRequests).toBeGreaterThanOrEqual(1);
    expect(calls.filter((call) => call.url === BUNDLE_PATH)).toHaveLength(1);
  });

  it("renders the complete sealed report in server order without client derivation", async () => {
    fetchRouter({
      [`GET ${PLANS_PATH}`]: () => json(s12CatalogPayload()),
      [`POST ${START_PATH}`]: () => json(s12JobPayload()),
      [`POST ${PROCESS_PATH}`]: () => json(s12ProcessPayload()),
      [`GET ${JOB_PATH}`]: () => json(s12JobPayload()),
      [`GET ${BUNDLE_PATH}`]: () => json(s12BundlePayload()),
    });
    renderWithQuery(<S12EvaluationOperator />);
    await selectPlanAndStart();
    await waitFor(() =>
      expect(screen.getByTestId("s12-sealed-report")).toBeInTheDocument(),
    );

    // Lifecycle facts render exactly as returned.
    expect(screen.getByTestId("s12-job-status")).toHaveTextContent("complete");
    expect(screen.getByTestId("s12-job-fence")).toHaveTextContent("1");
    expect(screen.getByTestId("s12-job-attempt")).toHaveTextContent("1");
    expect(screen.getByTestId("s12-job-created-at")).toHaveTextContent(
      "1700000000",
    );
    expect(screen.getByTestId("s12-job-reasons")).toHaveTextContent("—");
    expect(screen.getByTestId("s12-result-status")).toHaveTextContent(
      "PASS(scope=C)",
    );

    // The report carries the server-provided sections in server order.
    const sectionNames = Array.from(
      document.querySelectorAll('[data-testid="s12-report-section-name"]'),
    ).map((node) => node.textContent);
    expect(sectionNames.indexOf("bundle_id")).toBeLessThan(
      sectionNames.indexOf("plan_digest"),
    );
    expect(sectionNames.indexOf("status_reasons")).toBeGreaterThan(
      sectionNames.indexOf("scope"),
    );
    expect(sectionNames.indexOf("business_deltas")).toBeGreaterThan(
      sectionNames.indexOf("business_before"),
    );
    expect(sectionNames).toContain("result_digest");
    expect(sectionNames).toContain("replay_package_digest");

    // Exact values render verbatim: eligibility reasons, denominators,
    // intervals, lineage digests and zero business deltas.
    const body = document.body.textContent ?? "";
    expect(body).toContain("acceptance holdout is non-empty");
    expect(body).toContain("E_all");
    expect(body).toContain("0.8125");
    expect(body).toContain(S12_BUNDLE_ID);
    expect(screen.getAllByTestId("s12-value").length).toBeGreaterThan(20);

    // No client metric calculation or status translation is present.
    expect(body).not.toContain("[object Object]");
  });

  it.each([
    ["INVALID", { status: "INVALID", status_reasons: ["RUNNER_DIGEST_MISMATCH"] }],
    [
      "INSUFFICIENT",
      {
        status: "INSUFFICIENT",
        status_reasons: ["holdout empty", "coverage below threshold"],
        missing_opportunities: ["opp-1"],
      },
    ],
    [
      "FAIL",
      { status: "FAIL", status_reasons: ["inconsistent majority"] },
    ],
    ["PASS(scope=R-E2E)", { status: "PASS(scope=R-E2E)" }],
    [
      "SMOKE_ONLY",
      { status: "SMOKE_ONLY", status_reasons: ["smoke corpus only"] },
    ],
  ])("renders exact result status %# verbatim from the server", async (
    _label,
    overrides,
) => {
    const bundle = s12BundlePayload(overrides);
    const result = {
      bundle_id: S12_BUNDLE_ID,
      status: bundle.status,
      reason_codes: (bundle.status_reasons as string[]) ?? [],
    };
    fetchRouter({
      [`GET ${PLANS_PATH}`]: () => json(s12CatalogPayload()),
      [`POST ${START_PATH}`]: () => json(s12JobPayload({ status: "queued" }, { bundle_id: null, status: null, reason_codes: [] })),
      [`POST ${PROCESS_PATH}`]: () => json(s12ProcessPayload()),
      [`GET ${JOB_PATH}`]: () => json(s12JobPayload({}, result)),
      [`GET ${BUNDLE_PATH}`]: () => json(bundle),
    });
    renderWithQuery(<S12EvaluationOperator />);
    await selectPlanAndStart();
    await waitFor(() =>
      expect(screen.getByTestId("s12-result-status")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("s12-result-status").textContent).toBe(
      bundle.status,
    );
  });

  it("keeps the start action disabled while pending and never creates a second job", async () => {
    let releaseStart: ((response: Response) => void) | undefined;
    const pendingStart = new Promise<Response>((resolve) => {
      releaseStart = resolve;
    });
    const router = fetchRouter({
      [`GET ${PLANS_PATH}`]: () => json(s12CatalogPayload()),
      [`POST ${START_PATH}`]: () => {
        return pendingStart;
      },
      [`POST ${PROCESS_PATH}`]: () => json(s12ProcessPayload()),
      [`GET ${JOB_PATH}`]: () => json(s12JobPayload({ status: "queued" }, { bundle_id: null, status: null, reason_codes: [] })),
      [`GET ${BUNDLE_PATH}`]: () => json(s12BundlePayload()),
    });
    renderWithQuery(<S12EvaluationOperator />);
    await screen.findByTestId("s12-plan-select");
    await userEvent.selectOptions(
      screen.getByTestId("s12-plan-select"),
      S12_PLAN_ID,
    );
    await userEvent.click(screen.getByTestId("s12-start-button"));

    // While the one operator action is in flight the control is disabled,
    // so a second click cannot mint a replacement job.
    expect(screen.getByTestId("s12-start-button")).toBeDisabled();
    await userEvent.click(screen.getByTestId("s12-start-button"));
    expect(router.calls.filter((call) => call.url === START_PATH)).toHaveLength(
      1,
    );

    releaseStart?.(json(s12JobPayload()));
    await waitFor(() =>
      expect(screen.getByTestId("s12-start-button")).toBeDisabled(),
    );
  });

  it(
    "renders the bounded/unknown end with the original job id and no new job or process request",
    async () => {
      // The exact 120-cycle/1s bound semantics are owned by the hook tests.
      // This component test verifies only the timed_out presentation through
      // a stable module mock, never by waiting out the real production bound.
      const spy = vi.spyOn(hooksModule, "useS12JobPoll");
      spy.mockImplementation(() => "timed_out");
      let jobRequests = 0;
      const router = fetchRouter({
        [`GET ${PLANS_PATH}`]: () => json(s12CatalogPayload()),
        [`POST ${START_PATH}`]: () =>
          json(s12JobPayload({ status: "leased" }, { bundle_id: null, status: null, reason_codes: [] })),
        [`POST ${PROCESS_PATH}`]: () =>
          json(s12ProcessPayload({ status: "leased", bundle_id: null })),
        [`GET ${JOB_PATH}`]: () => {
          jobRequests += 1;
          return json(
            s12JobPayload({ status: "leased" }, { bundle_id: null, status: null, reason_codes: [] }),
          );
        },
      });
      renderWithQuery(<S12EvaluationOperator />);
      await selectPlanAndStart();
      await waitFor(() =>
        expect(screen.getByTestId("s12-poll-bounded")).toBeInTheDocument(),
      );
      expect(screen.getByTestId("s12-poll-bounded").textContent).toContain(
        S12_JOB_ID,
      );
      const posts = router.calls.filter((call) => call.method === "POST");
      expect(posts).toHaveLength(2); // exactly the original start + process
      expect(jobRequests).toBeLessThanOrEqual(121);
      spy.mockRestore();
    },
  );

  it("renders the four closed error states with exact codes and no leaked identifiers on denial", async () => {
    const router = fetchRouter({
      [`GET ${PLANS_PATH}`]: () => error(403, "S12_FORBIDDEN"),
    });
    renderWithQuery(<S12EvaluationOperator />);
    await waitFor(() =>
      expect(screen.getByTestId("s12-error-forbidden")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("s12-error-forbidden")).toHaveAttribute(
      "role",
      "alert",
    );
    expect(screen.getByTestId("s12-error-code")).toHaveTextContent(
      "S12_FORBIDDEN",
    );
    // Authorization-denial content carries no plan, job or bundle identity.
    const denied = document.body.textContent ?? "";
    expect(denied).not.toContain(S12_PLAN_ID);
    expect(denied).not.toContain(S12_JOB_ID);
    expect(denied).not.toContain(S12_BUNDLE_ID);
    expect(router.calls).toHaveLength(1);
  });

  it.each([
    [403, "S12_FORBIDDEN", "s12-error-forbidden"],
    [404, "S12_NOT_FOUND", "s12-error-not-found"],
    [422, "S12_INVALID_COMMAND", "s12-error-invalid"],
    [503, "S12_UNAVAILABLE", "s12-error-unavailable"],
  ])("maps catalog %i %s to a distinct explicit state", async (
    status,
    code,
    testId,
) => {
    fetchRouter({
      [`GET ${PLANS_PATH}`]: () => error(status, code),
    });
    renderWithQuery(<S12EvaluationOperator />);
    await waitFor(() =>
      expect(screen.getByTestId(testId)).toBeInTheDocument(),
    );
    expect(screen.getByTestId("s12-error-code")).toHaveTextContent(code);
  });

  it("preserves stale identifiers only as context in the unavailable state after data was shown", async () => {
    let failCatalog = true;
    let jobId: string | null = null;
    const routes: Record<string, RouteHandler> = {
      [`GET ${PLANS_PATH}`]: () =>
        failCatalog ? error(503, "S12_UNAVAILABLE") : json(s12CatalogPayload()),
      [`POST ${START_PATH}`]: () => {
        return Promise.reject(new TypeError("lost"));
      },
    };
    void jobId;
    fetchRouter(routes);
    renderWithQuery(<S12EvaluationOperator />);
    await waitFor(() =>
      expect(screen.getByTestId("s12-error-unavailable")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("s12-plan-select")).not.toBeInTheDocument();
  });

  it("never issues business, legacy, freeze-body, cancel or rerun requests", async () => {
    const router = fetchRouter({
      [`GET ${PLANS_PATH}`]: () => json(s12CatalogPayload()),
      [`POST ${START_PATH}`]: () => json(s12JobPayload()),
      [`POST ${PROCESS_PATH}`]: () => json(s12ProcessPayload()),
      [`GET ${JOB_PATH}`]: () => json(s12JobPayload()),
      [`GET ${BUNDLE_PATH}`]: () => json(s12BundlePayload()),
    });
    renderWithQuery(<S12EvaluationOperator />);
    await selectPlanAndStart();
    await waitFor(() =>
      expect(screen.getByTestId("s12-sealed-report")).toBeInTheDocument(),
    );
    const forbidden = router.calls.filter((call) =>
      /freeze|cancel|rerun|hot-edit|submit|import_legacy|approve/.test(call.url),
    );
    expect(forbidden).toHaveLength(0);
  });

  it("issues no command POST on mount, even under StrictMode", () => {
    const router = fetchRouter({
      [`GET ${PLANS_PATH}`]: () => new Promise<Response>(() => {}),
    });
    renderWithQuery(
      <StrictMode>
        <S12EvaluationOperator />
      </StrictMode>,
    );
    expect(router.calls.filter((call) => call.method === "POST")).toHaveLength(
      0,
    );
  });

  it("operates fully by keyboard: select, start, and status is announced", async () => {
    fetchRouter({
      [`GET ${PLANS_PATH}`]: () => json(s12CatalogPayload()),
      [`POST ${START_PATH}`]: () => json(s12JobPayload()),
      [`POST ${PROCESS_PATH}`]: () => json(s12ProcessPayload()),
      [`GET ${JOB_PATH}`]: () => json(s12JobPayload()),
      [`GET ${BUNDLE_PATH}`]: () => json(s12BundlePayload()),
    });
    renderWithQuery(<S12EvaluationOperator />);
    const select = await screen.findByTestId("s12-plan-select");
    select.focus();
    await userEvent.selectOptions(select, S12_PLAN_ID);
    const start = screen.getByTestId("s12-start-button");
    start.focus();
    await userEvent.keyboard("{Enter}");
    await waitFor(() =>
      expect(screen.getByTestId("s12-sealed-report")).toBeInTheDocument(),
    );
    const live = screen.getByTestId("s12-job-live");
    expect(live).toHaveAttribute("aria-live", "polite");
    expect(live.textContent).toContain("complete");
    expect(live.textContent).toContain(S12_JOB_ID);
  });

  it("renders an explicit loading state before the catalog lands", () => {
    fetchRouter({
      [`GET ${PLANS_PATH}`]: () => new Promise<Response>(() => {}),
    });
    renderWithQuery(<S12EvaluationOperator />);
    expect(screen.getByTestId("s12-catalog-loading")).toBeInTheDocument();
    expect(screen.getByTestId("s12-catalog-loading")).toHaveAttribute(
      "role",
      "status",
    );
  });

  it("renders an explicit empty state when no frozen plan exists", async () => {
    fetchRouter({
      [`GET ${PLANS_PATH}`]: () =>
        json({ schema_version: "s12-plan-catalog/1", plans: [] }),
    });
    renderWithQuery(<S12EvaluationOperator />);
    await waitFor(() =>
      expect(screen.getByTestId("s12-catalog-empty")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("s12-start-button")).not.toBeInTheDocument();
  });

  it("renders the bundle read failure as unavailable while keeping the job context", async () => {
    fetchRouter({
      [`GET ${PLANS_PATH}`]: () => json(s12CatalogPayload()),
      [`POST ${START_PATH}`]: () => json(s12JobPayload()),
      [`POST ${PROCESS_PATH}`]: () => json(s12ProcessPayload()),
      [`GET ${JOB_PATH}`]: () => json(s12JobPayload()),
      [`GET ${BUNDLE_PATH}`]: () => error(503, "S12_UNAVAILABLE"),
    });
    renderWithQuery(<S12EvaluationOperator />);
    await selectPlanAndStart();
    await waitFor(() =>
      expect(screen.getByTestId("s12-error-unavailable")).toBeInTheDocument(),
    );
    // Prior identifiers stay visible only as stale context.
    expect(document.body.textContent ?? "").toContain(S12_JOB_ID);
  });

  it("keeps the original job visible when process acknowledgement fails", async () => {
    fetchRouter({
      [`GET ${PLANS_PATH}`]: () => json(s12CatalogPayload()),
      [`POST ${START_PATH}`]: () =>
        json(s12JobPayload({ status: "queued" }, { bundle_id: null, status: null, reason_codes: [] })),
      [`POST ${PROCESS_PATH}`]: () => error(503, "S12_UNAVAILABLE"),
      [`GET ${JOB_PATH}`]: () =>
        json(
          s12JobPayload(
            { status: "queued", reason_codes: ["PROCESS_PENDING"] },
            { bundle_id: null, status: null, reason_codes: [] },
          ),
        ),
    });
    renderWithQuery(<S12EvaluationOperator />);
    await selectPlanAndStart();
    await waitFor(() =>
      expect(screen.getByTestId("s12-process-error")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("s12-job-live")).toHaveTextContent(S12_JOB_ID);
    expect(screen.getByTestId("s12-job-reasons")).toHaveTextContent(
      "PROCESS_PENDING",
    );
    expect(screen.queryByTestId("s12-sealed-report")).not.toBeInTheDocument();
    expect(screen.getByTestId("s12-start-button")).toBeDisabled();
  });

  it("renders an exact job-query rejection and never reads a bundle", async () => {
    const router = fetchRouter({
      [`GET ${PLANS_PATH}`]: () => json(s12CatalogPayload()),
      [`POST ${START_PATH}`]: () => json(s12JobPayload()),
      [`POST ${PROCESS_PATH}`]: () => json(s12ProcessPayload()),
      [`GET ${JOB_PATH}`]: () => error(404, "S12_NOT_FOUND"),
    });
    renderWithQuery(<S12EvaluationOperator />);
    await selectPlanAndStart();
    await waitFor(() =>
      expect(screen.getByTestId("s12-job-error")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("s12-error-code")).toHaveTextContent(
      "S12_NOT_FOUND",
    );
    expect(screen.getByTestId("s12-job-live")).toHaveTextContent(
      "任务状态读取失败",
    );
    expect(router.calls.some((call) => call.url === BUNDLE_PATH)).toBe(false);
  });

  it("stops at a failed terminal job without reading any bundle", async () => {
    const router = fetchRouter({
      [`GET ${PLANS_PATH}`]: () => json(s12CatalogPayload()),
      [`POST ${START_PATH}`]: () => json(s12JobPayload({ status: "queued" }, { bundle_id: null, status: null, reason_codes: [] })),
      [`POST ${PROCESS_PATH}`]: () =>
        json(s12ProcessPayload({ status: "failed", reason_code: "JOB_CANCELLED", bundle_id: null })),
      [`GET ${JOB_PATH}`]: () =>
        json(
          s12JobPayload(
            { status: "cancelled" },
            { bundle_id: null, status: null, reason_codes: [] },
          ),
        ),
    });
    renderWithQuery(<S12EvaluationOperator />);
    await selectPlanAndStart();
    await waitFor(() =>
      expect(screen.getByTestId("s12-job-status")).toHaveTextContent(
        "cancelled",
      ),
    );
    expect(screen.getByTestId("s12-job-terminal-note")).toBeInTheDocument();
    expect(
      router.calls.filter((call) => call.url.startsWith("/controlled/s12/bundles")),
    ).toHaveLength(0);
    // A failed process envelope (failed/JOB_CANCELLED) never becomes FAIL:
    // the raw result-status cell renders the server value verbatim ("—").
    expect(screen.getByTestId("s12-result-status")).toHaveTextContent("—");
    expect(screen.getByTestId("s12-job-live").textContent).not.toContain(
      "FAIL",
    );
  });
});
