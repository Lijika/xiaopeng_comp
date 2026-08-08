import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type {
  DemoBatchCheckResponse,
  DemoEvaluationSummaryResponse,
  DemoFixtureOption,
} from "../api/client";
import { fetchRouter, renderWithQuery } from "../test-utils";
import DemoBatchSummaryPanel from "./DemoBatchSummaryPanel";

const FIXTURE_OK = "app_demo_step2_ok";
const FIXTURE_BAD_VIN = "app_demo_step2_bad_vin";

function options(): DemoFixtureOption[] {
  return [
    {
      fixture_id: FIXTURE_OK,
      title: "演示样例 1",
      description: "预置合成多单据校验样例",
      field_source: "synthetic",
      step2_sample_id: "JFL25P02L080310-01",
    },
    {
      fixture_id: FIXTURE_BAD_VIN,
      title: "演示样例 2",
      description: "预置合成多单据校验样例",
      field_source: "synthetic",
      step2_sample_id: "JFL25P02L086208-01",
    },
  ];
}

function fixturesPayload() {
  return { fixtures: options(), batch_max_n: 50 };
}

function completedBatchPayload(): DemoBatchCheckResponse {
  return {
    track: "C-DEMO",
    data_scope: "synthetic",
    requested: 2,
    completed: 2,
    failed: 0,
    outcome: "completed",
    totals: { consistent: 5, inconsistent: 1, uncertain: 0, skipped: 0 },
    results: [
      {
        fixture_id: FIXTURE_OK,
        outcome: "completed",
        application_id: "DEMO-STEP2-JFL25P02L080310-01-OK",
        summary: {
          consistent: 5,
          inconsistent: 0,
          uncertain: 0,
          skipped: 0,
          coverage: 1,
          total: 5,
          total_including_skipped: 5,
        },
        issues: [],
        error: null,
      },
      {
        fixture_id: FIXTURE_BAD_VIN,
        outcome: "completed",
        application_id: "DEMO-STEP2-JFL25P02L086208-01-BADVIN",
        summary: {
          consistent: 4,
          inconsistent: 1,
          uncertain: 0,
          skipped: 0,
          coverage: 1,
          total: 5,
          total_including_skipped: 5,
        },
        issues: [
          {
            rule_id: "R_VIN_CROSS",
            verdict: "inconsistent",
            message: "合同 VIN 与登记证 VIN 不一致",
            reason_codes: ["VIN_MISMATCH"],
          },
        ],
        error: null,
      },
    ],
  };
}

function partialBatchPayload(): DemoBatchCheckResponse {
  return {
    track: "C-DEMO",
    data_scope: "synthetic",
    requested: 2,
    completed: 1,
    failed: 1,
    outcome: "partial",
    totals: { consistent: 5, inconsistent: 0, uncertain: 0, skipped: 0 },
    results: [
      {
        fixture_id: FIXTURE_OK,
        outcome: "completed",
        application_id: "DEMO-STEP2-JFL25P02L080310-01-OK",
        summary: {
          consistent: 5,
          inconsistent: 0,
          uncertain: 0,
          skipped: 0,
          coverage: 1,
          total: 5,
          total_including_skipped: 5,
        },
        issues: [],
        error: null,
      },
      {
        fixture_id: FIXTURE_BAD_VIN,
        outcome: "failed",
        application_id: null,
        summary: null,
        issues: [],
        error: "internal /srv/secret/rules.yaml exploded",
      },
    ],
  };
}

function availableSummaryPayload(): DemoEvaluationSummaryResponse {
  return {
    summary_state: "available",
    suite: "main",
    claim: "C-DEV-REG",
    performance_gap: "UNVERIFIED",
    scope: "合成开发/回归语料（suite=main）",
    counts: {
      n_apps_loaded: 154,
      n_check_ok: 154,
      n_check_fail: 0,
      total_pairs: 1646,
      decisive_pairs: 1624,
      true_positive: 106,
      true_negative: 1518,
      false_positive: 0,
      false_negative: 0,
      uncertain_when_labeled: 0,
      n_inconsistent_labeled_decisive: 106,
      n_expected_inconsistent: 106,
      n_missed_inconsistent: 0,
    },
    rates: {
      coverage: 0.9882,
      false_positive_rate: 0,
      false_negative_rate: 0,
      accuracy: 1,
      miss_rate: 0,
      uncertain_rate: 0,
      mean_app_coverage: 0.9882,
    },
    warnings: [],
    honesty_note: "Official delivery metrics from suite=main only.",
  };
}

function emptySummaryPayload(): DemoEvaluationSummaryResponse {
  return {
    summary_state: "empty",
    suite: "main",
    claim: "C-DEV-REG",
    performance_gap: "UNVERIFIED",
    scope: "合成开发/回归语料（suite=main）",
    counts: null,
    rates: null,
    warnings: ["smoke_mode: labeled_files=0; FP/FN not computed"],
    honesty_note: "Official delivery metrics from suite=main only.",
  };
}

describe("DemoBatchSummaryPanel", () => {
  it("renders native checkbox controls and the server-owned cap, firing no batch POST and no summary GET on mount", async () => {
    const router = fetchRouter({
      "GET /api/demo/fixtures": () => router.jsonResponse(fixturesPayload()),
    });
    renderWithQuery(<DemoBatchSummaryPanel />);
    await screen.findByTestId("demo-batch-panel");
    expect(
      await screen.findByTestId("demo-batch-cap"),
    ).toHaveTextContent("服务端上限：50");
    expect(
      await screen.findByTestId(`demo-batch-fixture-${FIXTURE_OK}`),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId(`demo-batch-fixture-${FIXTURE_BAD_VIN}`),
    ).toBeInTheDocument();
    expect(screen.getByTestId("demo-batch-run-button")).toBeDisabled();
    expect(screen.getByTestId("demo-eval-status")).toHaveTextContent("未加载");
    expect(
      router.calls.filter(
        (call) => call.url === "/api/demo/evaluate/summary",
      ),
    ).toHaveLength(0);
    expect(router.calls.filter((call) => call.method === "POST")).toHaveLength(
      0,
    );
  });

  it("runs a two-fixture batch with a pending live status and exactly one POST carrying only fixture ids in selection order", async () => {
    let release = () => {};
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    const router = fetchRouter({
      "GET /api/demo/fixtures": () => router.jsonResponse(fixturesPayload()),
      "POST /api/demo/check/batch": async () => {
        await held;
        return router.jsonResponse(completedBatchPayload());
      },
    });
    renderWithQuery(<DemoBatchSummaryPanel />);
    await userEvent.click(
      await screen.findByTestId(`demo-batch-fixture-${FIXTURE_OK}`),
    );
    await userEvent.click(
      screen.getByTestId(`demo-batch-fixture-${FIXTURE_BAD_VIN}`),
    );
    await userEvent.click(screen.getByTestId("demo-batch-run-button"));
    await waitFor(() =>
      expect(screen.getByTestId("demo-batch-status")).toHaveTextContent(
        "批量校验中",
      ),
    );
    release();
    await waitFor(() =>
      expect(screen.getByTestId("demo-batch-status")).toHaveTextContent(
        "批量校验完成",
      ),
    );
    expect(screen.getByTestId("demo-batch-outcome")).toHaveTextContent(
      "全部完成",
    );
    expect(screen.getByTestId("demo-batch-totals")).toHaveTextContent(
      "不一致 1",
    );
    expect(
      screen.getByTestId(`demo-batch-item-${FIXTURE_OK}`),
    ).toHaveTextContent("已完成");
    expect(
      screen.getByTestId(`demo-batch-item-${FIXTURE_BAD_VIN}`),
    ).toHaveTextContent("R_VIN_CROSS");
    const posts = router.calls.filter((call) => call.method === "POST");
    expect(posts).toHaveLength(1);
    expect(posts[0].body).toEqual({
      fixture_ids: [FIXTURE_OK, FIXTURE_BAD_VIN],
    });
  });

  it("locks every selection control while an active batch runs and delivers exactly one terminal result", async () => {
    let release = () => {};
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    const router = fetchRouter({
      "GET /api/demo/fixtures": () => router.jsonResponse(fixturesPayload()),
      "POST /api/demo/check/batch": async () => {
        await held;
        return router.jsonResponse(completedBatchPayload());
      },
    });
    renderWithQuery(<DemoBatchSummaryPanel />);
    await userEvent.click(
      await screen.findByTestId(`demo-batch-fixture-${FIXTURE_OK}`),
    );
    await userEvent.click(screen.getByTestId("demo-batch-run-button"));
    await waitFor(() =>
      expect(screen.getByTestId("demo-batch-status")).toHaveTextContent(
        "批量校验中",
      ),
    );
    // pending: the run button and every selection control are locked
    expect(screen.getByTestId("demo-batch-run-button")).toBeDisabled();
    expect(
      screen.getByTestId(`demo-batch-fixture-${FIXTURE_OK}`),
    ).toBeDisabled();
    expect(
      screen.getByTestId(`demo-batch-fixture-${FIXTURE_BAD_VIN}`),
    ).toBeDisabled();
    // an attempted selection change cannot reset the live mutation: the
    // selection stays unchanged and no second POST can start
    fireEvent.click(screen.getByTestId(`demo-batch-fixture-${FIXTURE_BAD_VIN}`));
    expect(
      screen.getByTestId(`demo-batch-fixture-${FIXTURE_BAD_VIN}`),
    ).not.toBeChecked();
    expect(
      screen.getByTestId("demo-batch-status"),
    ).toHaveTextContent("批量校验中");
    expect(router.calls.filter((call) => call.method === "POST")).toHaveLength(
      1,
    );
    release();
    // exactly one terminal result is delivered for the one request
    await waitFor(() =>
      expect(screen.getByTestId("demo-batch-status")).toHaveTextContent(
        "批量校验完成",
      ),
    );
    expect(screen.getByTestId("demo-batch-outcome")).toHaveTextContent(
      "全部完成",
    );
    expect(router.calls.filter((call) => call.method === "POST")).toHaveLength(
      1,
    );
    expect(screen.getByTestId("demo-batch-totals")).toHaveTextContent(
      "不一致 1",
    );
  });

  it("shows the fixed generic request failure with no reflected code or internal detail", async () => {
    const router = fetchRouter({
      "GET /api/demo/fixtures": () => router.jsonResponse(fixturesPayload()),
      "POST /api/demo/check/batch": () =>
        new Response(
          JSON.stringify({
            detail: {
              error: "DEMO_CHECK_FAILED",
              message: "internal /srv/secret/rules.yaml exploded",
            },
          }),
          {
            status: 500,
            headers: { "Content-Type": "application/json" },
          },
        ),
    });
    renderWithQuery(<DemoBatchSummaryPanel />);
    await userEvent.click(
      await screen.findByTestId(`demo-batch-fixture-${FIXTURE_OK}`),
    );
    await userEvent.click(screen.getByTestId("demo-batch-run-button"));
    await waitFor(() =>
      expect(screen.getByTestId("demo-batch-error")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("demo-batch-error")).toHaveTextContent(
      "批量校验失败，请稍后重试",
    );
    expect(screen.getByTestId("demo-batch-error").textContent).not.toContain(
      "DEMO_CHECK_FAILED",
    );
    expect(screen.getByTestId("demo-batch-error").textContent).not.toContain(
      "/srv/secret",
    );
    expect(screen.getByTestId("demo-batch-error").textContent).not.toContain(
      "rules.yaml",
    );
    // B6: the live status is terminally failed, never waiting
    const status = screen.getByTestId("demo-batch-status");
    expect(status).toHaveAttribute("role", "status");
    expect(status).toHaveTextContent("批量校验失败");
    expect(status.textContent).not.toContain("等待批量校验");
    // a second click refetches (no auto retry, explicit user action only)
    await userEvent.click(screen.getByTestId("demo-batch-run-button"));
    await waitFor(() =>
      expect(screen.getByTestId("demo-batch-error")).toBeInTheDocument(),
    );
    expect(
      router.calls.filter((call) => call.method === "POST"),
    ).toHaveLength(2);
  });

  it("renders the cap rejection as a terminal failed status with fixed bound-specific copy and the server cap separately visible", async () => {
    const router = fetchRouter({
      "GET /api/demo/fixtures": () => router.jsonResponse(fixturesPayload()),
      "POST /api/demo/check/batch": () =>
        new Response(
          JSON.stringify({
            detail: {
              error: "DEMO_BATCH_TOO_LARGE",
              message: "批量校验数量超过服务端上限 50",
            },
          }),
          {
            status: 400,
            headers: { "Content-Type": "application/json" },
          },
        ),
    });
    renderWithQuery(<DemoBatchSummaryPanel />);
    await userEvent.click(
      await screen.findByTestId(`demo-batch-fixture-${FIXTURE_OK}`),
    );
    await userEvent.click(screen.getByTestId("demo-batch-run-button"));
    await waitFor(() =>
      expect(screen.getByTestId("demo-batch-error")).toBeInTheDocument(),
    );
    // the live status is terminally failed, never waiting
    const status = screen.getByTestId("demo-batch-status");
    expect(status).toHaveAttribute("role", "status");
    expect(status).toHaveTextContent("批量校验失败");
    expect(status.textContent).not.toContain("等待批量校验");
    // the cap case uses the fixed bound-specific copy only; no code or bound
    const alert = screen.getByTestId("demo-batch-error");
    expect(alert).toHaveAttribute("role", "alert");
    expect(alert).toHaveTextContent("所选样例数量超过服务端上限，请减少选择");
    expect(alert.textContent).not.toContain("DEMO_BATCH_TOO_LARGE");
    expect(alert.textContent).not.toContain("50");
    expect(alert.textContent).not.toContain("等待");
    // the server-owned cap label remains separately visible
    expect(screen.getByTestId("demo-batch-cap")).toHaveTextContent(
      "服务端上限：50",
    );
    expect(router.calls.filter((call) => call.method === "POST")).toHaveLength(
      1,
    );
  });

  it("renders a partial batch: partial outcome, fixed generic failed-item error, totals exclude failed items", async () => {
    const router = fetchRouter({
      "GET /api/demo/fixtures": () => router.jsonResponse(fixturesPayload()),
      "POST /api/demo/check/batch": () =>
        router.jsonResponse(partialBatchPayload()),
    });
    renderWithQuery(<DemoBatchSummaryPanel />);
    await userEvent.click(
      await screen.findByTestId(`demo-batch-fixture-${FIXTURE_OK}`),
    );
    await userEvent.click(
      screen.getByTestId(`demo-batch-fixture-${FIXTURE_BAD_VIN}`),
    );
    await userEvent.click(screen.getByTestId("demo-batch-run-button"));
    await waitFor(() =>
      expect(screen.getByTestId("demo-batch-outcome")).toHaveTextContent(
        "部分完成",
      ),
    );
    const failedItem = screen.getByTestId(
      `demo-batch-item-${FIXTURE_BAD_VIN}`,
    );
    expect(failedItem).toHaveTextContent("失败");
    expect(
      failedItem.querySelector('[data-testid="demo-batch-item-error"]'),
    ).toHaveTextContent("条目校验失败，请稍后重试");
    expect(failedItem.textContent).not.toContain("/srv/secret");
    expect(screen.getByTestId("demo-batch-totals")).toHaveTextContent("一致 5");
  });

  it("shows explicit loading/error/empty states for the fixture list", async () => {
    // loading
    let release = () => {};
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    const router = fetchRouter({
      "GET /api/demo/fixtures": async () => {
        await held;
        return router.jsonResponse(fixturesPayload());
      },
    });
    const loadingView = renderWithQuery(<DemoBatchSummaryPanel />);
    expect(
      await screen.findByTestId("demo-batch-fixtures-loading"),
    ).toBeInTheDocument();
    release();
    await waitFor(() =>
      expect(
        screen.queryByTestId("demo-batch-fixtures-loading"),
      ).not.toBeInTheDocument(),
    );
    loadingView.unmount();

    // list failure: the fixed generic alert, no reflected code (the shared
    // fixture query retries transient 503s, so the wait must cover all
    // bounded attempts)
    const errorRouter = fetchRouter({
      "GET /api/demo/fixtures": () =>
        errorRouter.jsonResponse(
          { detail: { error: "DEMO_FIXTURE_UNAVAILABLE", message: "演示样例暂不可用" } },
          503,
        ),
    });
    renderWithQuery(<DemoBatchSummaryPanel />);
    await waitFor(
      () =>
        expect(screen.getByTestId("demo-batch-fixtures-error")).toBeInTheDocument(),
      { timeout: 6_000 },
    );
    expect(screen.getByTestId("demo-batch-fixtures-error")).toHaveAttribute(
      "role",
      "alert",
    );
    expect(screen.getByTestId("demo-batch-fixtures-error").textContent).not.toContain(
      "DEMO_FIXTURE_UNAVAILABLE",
    );

    // empty list: explicit empty state
    const emptyRouter = fetchRouter({
      "GET /api/demo/fixtures": () =>
        emptyRouter.jsonResponse({ fixtures: [], batch_max_n: 50 }),
    });
    renderWithQuery(<DemoBatchSummaryPanel />);
    await waitFor(() =>
      expect(screen.getByTestId("demo-batch-fixtures-empty")).toBeInTheDocument(),
    );
  });

  it("loads the read-only summary only on explicit click with server-owned claim labels and no PASS", async () => {
    const router = fetchRouter({
      "GET /api/demo/fixtures": () => router.jsonResponse(fixturesPayload()),
      "GET /api/demo/evaluate/summary": () =>
        router.jsonResponse(availableSummaryPayload()),
    });
    renderWithQuery(<DemoBatchSummaryPanel />);
    await screen.findByTestId("demo-eval-panel");
    expect(screen.getByTestId("demo-eval-status")).toHaveTextContent("未加载");
    expect(
      router.calls.filter(
        (call) => call.url === "/api/demo/evaluate/summary",
      ),
    ).toHaveLength(0);

    await userEvent.click(screen.getByTestId("demo-eval-load-button"));
    await waitFor(() =>
      expect(screen.getByTestId("demo-eval-status")).toHaveTextContent(
        "已加载",
      ),
    );
    expect(screen.getByTestId("demo-eval-claim")).toHaveTextContent(
      "C-DEV-REG",
    );
    expect(screen.getByTestId("demo-eval-gap")).toHaveTextContent(
      "UNVERIFIED",
    );
    expect(screen.getByTestId("demo-eval-scope")).not.toBeEmptyDOMElement();
    expect(screen.getByTestId("demo-eval-counts")).toHaveTextContent(
      "total_pairs",
    );
    expect(screen.getByTestId("demo-eval-counts")).toHaveTextContent("1646");
    expect(screen.getByTestId("demo-eval-rates")).toHaveTextContent("coverage");
    expect(screen.getByTestId("demo-eval-rates")).toHaveTextContent("0.9882");
    expect(screen.getByTestId("demo-eval-note")).not.toBeEmptyDOMElement();
    expect(document.body.textContent).not.toContain("PASS");
    expect(
      router.calls.filter(
        (call) => call.url === "/api/demo/evaluate/summary",
      ),
    ).toHaveLength(1);
  });

  it("shows the explicit empty state with nullable counts/rates and no zero-success claim", async () => {
    const router = fetchRouter({
      "GET /api/demo/fixtures": () => router.jsonResponse(fixturesPayload()),
      "GET /api/demo/evaluate/summary": () =>
        router.jsonResponse(emptySummaryPayload()),
    });
    renderWithQuery(<DemoBatchSummaryPanel />);
    await screen.findByTestId("demo-eval-panel");
    await userEvent.click(screen.getByTestId("demo-eval-load-button"));
    await waitFor(() =>
      expect(screen.getByTestId("demo-eval-empty")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("demo-eval-claim")).toHaveTextContent(
      "C-DEV-REG",
    );
    expect(screen.getByTestId("demo-eval-gap")).toHaveTextContent(
      "UNVERIFIED",
    );
    expect(screen.queryByTestId("demo-eval-rates")).not.toBeInTheDocument();
    expect(screen.queryByTestId("demo-eval-counts")).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain("coverage");
    expect(
      router.calls.filter(
        (call) => call.url === "/api/demo/evaluate/summary",
      ),
    ).toHaveLength(1);
  });

  it("shows the fixed unavailable state and refetches only on a second explicit click", async () => {
    let fail = true;
    const router = fetchRouter({
      "GET /api/demo/fixtures": () => router.jsonResponse(fixturesPayload()),
      "GET /api/demo/evaluate/summary": () =>
        fail
          ? new Response(
              JSON.stringify({
                detail: {
                  error: "DEMO_EVALUATION_UNAVAILABLE",
                  message: "internal /srv/secret/evaluate.py crashed",
                },
              }),
              {
                status: 503,
                headers: { "Content-Type": "application/json" },
              },
            )
          : router.jsonResponse(availableSummaryPayload()),
    });
    renderWithQuery(<DemoBatchSummaryPanel />);
    await screen.findByTestId("demo-eval-panel");
    await userEvent.click(screen.getByTestId("demo-eval-load-button"));
    await waitFor(() =>
      expect(screen.getByTestId("demo-eval-error")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("demo-eval-error")).toHaveTextContent(
      "评估摘要不可用",
    );
    expect(screen.getByTestId("demo-eval-error").textContent).not.toContain(
      "DEMO_EVALUATION_UNAVAILABLE",
    );
    expect(screen.getByTestId("demo-eval-error").textContent).not.toContain(
      "/srv/secret",
    );

    fail = false;
    await userEvent.click(screen.getByTestId("demo-eval-load-button"));
    await waitFor(() =>
      expect(screen.getByTestId("demo-eval-status")).toHaveTextContent(
        "已加载",
      ),
    );
    expect(
      router.calls.filter(
        (call) => call.url === "/api/demo/evaluate/summary",
      ),
    ).toHaveLength(2);
  });

  it("hides cached summary metrics while reloading and after a failed reload", async () => {
    let fail = false;
    let releaseReload = () => {};
    const heldReload = new Promise<void>((resolve) => {
      releaseReload = resolve;
    });
    const router = fetchRouter({
      "GET /api/demo/fixtures": () => router.jsonResponse(fixturesPayload()),
      "GET /api/demo/evaluate/summary": () => {
        if (fail) {
          return heldReload.then(() =>
            new Response(
              JSON.stringify({
                detail: {
                  error: "DEMO_EVALUATION_UNAVAILABLE",
                  message: "internal /srv/secret/evaluate.py crashed",
                },
              }),
              {
                status: 503,
                headers: { "Content-Type": "application/json" },
              },
            ),
          );
        }
        return router.jsonResponse(availableSummaryPayload());
      },
    });
    renderWithQuery(<DemoBatchSummaryPanel />);
    await screen.findByTestId("demo-eval-panel");
    await userEvent.click(screen.getByTestId("demo-eval-load-button"));
    await waitFor(() =>
      expect(screen.getByTestId("demo-eval-status")).toHaveTextContent(
        "已加载",
      ),
    );
    expect(screen.getByTestId("demo-eval-counts")).toBeInTheDocument();

    // reload: the cached counts/rates are hidden while the refetch is in
    // flight, never shown as current next to a loading state
    fail = true;
    await userEvent.click(screen.getByTestId("demo-eval-load-button"));
    await waitFor(() =>
      expect(screen.queryByTestId("demo-eval-counts")).not.toBeInTheDocument(),
    );
    expect(screen.getByTestId("demo-eval-status")).toHaveTextContent("加载中");
    releaseReload();

    // after the failed reload: the alert is explicit and the stale metrics
    // stay hidden
    await waitFor(() =>
      expect(screen.getByTestId("demo-eval-error")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("demo-eval-counts")).not.toBeInTheDocument();
    expect(screen.queryByTestId("demo-eval-rates")).not.toBeInTheDocument();
    expect(screen.getByTestId("demo-eval-status")).toHaveTextContent(
      "评估摘要不可用",
    );
    expect(screen.getByTestId("demo-eval-error").textContent).not.toContain(
      "/srv/secret",
    );
    expect(
      router.calls.filter(
        (call) => call.url === "/api/demo/evaluate/summary",
      ),
    ).toHaveLength(2);
  });
});
