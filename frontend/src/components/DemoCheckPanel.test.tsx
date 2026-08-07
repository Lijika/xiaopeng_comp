import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type {
  DemoCheckResponse,
  DemoFixtureOption,
} from "../api/client";
import { fetchRouter, renderWithQuery } from "../test-utils";
import DemoCheckPanel from "./DemoCheckPanel";

export function demoFixtureOption(): DemoFixtureOption {
  return {
    fixture_id: "app_demo_step2_bad_vin",
    title: "演示样例 2",
    description: "预置合成多单据校验样例",
    field_source: "synthetic",
    step2_sample_id: "JFL25P02L086208-01",
  };
}

export function demoCheckResponse(): DemoCheckResponse {
  return {
    track: "C-DEMO",
    data_scope: "synthetic",
    fixture_id: "app_demo_step2_bad_vin",
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
    checks: [
      {
        rule_id: "R_VIN_CROSS",
        name: "VIN 跨单据一致性",
        verdict: "inconsistent",
        severity: "critical",
        message: "合同 VIN 与登记证 VIN 不一致",
        snapshots: [
          {
            doc_id: "contract",
            doc_type: "融资租赁合同",
            field: "vin",
            raw: "LFV3A23K5J3123456",
            normalized: "LFV3A23K5J3123456",
            confidence: 0.98,
          },
          {
            doc_id: "registration",
            doc_type: "机动车登记证书",
            field: "vin",
            raw: "LFV3A23K5J3999999",
            normalized: "LFV3A23K5J3999999",
            confidence: 0.97,
          },
        ],
        diff_highlight: {
          pos: 12,
          left: "LFV3A23K5J3123456",
          right: "LFV3A23K5J3999999",
          detail: "第 13 位起不一致",
        },
        score: 0,
        rule_type: "exact",
        flags: [],
        reason_codes: ["VIN_MISMATCH"],
      },
      {
        rule_id: "R_ENGINE_CROSS",
        name: "发动机号跨单据一致性",
        verdict: "consistent",
        severity: "major",
        message: "发动机号一致",
        snapshots: [],
        score: 1,
        rule_type: "exact",
        flags: [],
        reason_codes: [],
      },
    ],
    config: {
      rule_config_version: "1.9.0",
      rule_package: "auto_lease",
      rule_changelog: [],
    },
    evidence_links: [
      {
        kind: "step2_sample",
        label: "Step2 页序样本 JFL25P02L086208-01",
        sample_id: "JFL25P02L086208-01",
        href: "/api/step2/JFL25P02L086208-01",
        limitation: "赛题影像页序/检测框元数据（无 OCR 文本）；字段值为合成仿真。",
      },
    ],
  };
}

describe("DemoCheckPanel", () => {
  it("selects a fixture and runs exactly one explicit POST carrying only fixture_id", async () => {
    const router = fetchRouter({
      "GET /api/demo/fixtures": () =>
        router.jsonResponse({ fixtures: [demoFixtureOption()] }),
      "POST /api/demo/check": () => router.jsonResponse(demoCheckResponse()),
    });
    renderWithQuery(<DemoCheckPanel />);
    await screen.findByTestId("demo-fixture-select");
    await userEvent.selectOptions(
      screen.getByTestId("demo-fixture-select"),
      "app_demo_step2_bad_vin",
    );
    await userEvent.click(screen.getByTestId("demo-run-button"));
    await waitFor(() =>
      expect(screen.getByTestId("demo-report")).toBeInTheDocument(),
    );
    const posts = router.calls.filter(
      (call) => call.method === "POST" && call.url.includes("/api/demo/check"),
    );
    expect(posts).toHaveLength(1);
    expect(posts[0].body).toEqual({ fixture_id: "app_demo_step2_bad_vin" });
    expect(screen.getByTestId("demo-report-track")).toHaveTextContent(
      "C-DEMO",
    );
    expect(screen.getByTestId("demo-report-scope")).toHaveTextContent(
      "synthetic",
    );
    expect(screen.getByTestId("demo-summary")).toHaveTextContent("不一致 1");
    expect(screen.getByTestId("demo-check-item-R_VIN_CROSS")).toHaveTextContent(
      "R_VIN_CROSS",
    );
    expect(screen.getByTestId("demo-evidence-link")).toHaveAttribute(
      "href",
      "/api/step2/JFL25P02L086208-01",
    );
  });

  it("shows an explicit loading state and issues exactly one fixtures read", () => {
    const router = fetchRouter({
      "GET /api/demo/fixtures": () =>
        new Promise(() => {
          // The bounded pending promise owns the loading state.
        }),
    });
    renderWithQuery(<DemoCheckPanel />);
    const loading = screen.getByTestId("demo-fixtures-loading");
    expect(loading).toHaveAttribute("role", "status");
    expect(router.calls).toHaveLength(1);
    expect(screen.queryByTestId("demo-run-button")).not.toBeInTheDocument();
  });

  it("shows an explicit empty state when the server returns no options", async () => {
    const router = fetchRouter({
      "GET /api/demo/fixtures": () => router.jsonResponse({ fixtures: [] }),
    });
    renderWithQuery(<DemoCheckPanel />);
    await waitFor(() =>
      expect(screen.getByTestId("demo-fixtures-empty")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("demo-fixtures-loading")).not.toBeInTheDocument();
    expect(router.calls).toHaveLength(1);
  });

  it("shows an explicit list-failure state without echoing server codes", async () => {
    const router = fetchRouter({
      "GET /api/demo/fixtures": () =>
        router.errorResponse(500, "DEMO_INTERNAL_ERROR"),
    });
    renderWithQuery(<DemoCheckPanel />);
    await waitFor(() =>
      expect(screen.getByTestId("demo-fixtures-error")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("demo-fixtures-error")).toHaveAttribute(
      "role",
      "alert",
    );
    expect(screen.getByTestId("demo-fixtures-error").textContent).not.toContain(
      "DEMO_INTERNAL_ERROR",
    );
    expect(router.calls).toHaveLength(1);
  });

  it("shows only the neutral server title in the selector before the check", async () => {
    const router = fetchRouter({
      "GET /api/demo/fixtures": () =>
        router.jsonResponse({ fixtures: [demoFixtureOption()] }),
    });
    renderWithQuery(<DemoCheckPanel />);
    const option = await screen.findByRole("option", { name: "演示样例 2" });
    expect(option).toHaveAttribute("value", "app_demo_step2_bad_vin");
    // visible option copy is only the neutral title: no raw fixture id and
    // no expected-outcome vocabulary is visible before the check
    expect(option.textContent).toBe("演示样例 2");
    const optionText = option.textContent ?? "";
    expect(optionText).not.toContain("app_demo_step2");
    expect(optionText).not.toContain("一致");
    expect(optionText).not.toContain("expected");
    expect(optionText).not.toContain("label");
    expect(
      screen.getAllByRole("option").map((o) => o.textContent),
    ).toEqual(["请选择…", "演示样例 2"]);
    expect(router.calls).toHaveLength(1);
  });

  it("keeps the run button disabled until a fixture is selected", async () => {
    const router = fetchRouter({
      "GET /api/demo/fixtures": () =>
        router.jsonResponse({ fixtures: [demoFixtureOption()] }),
    });
    renderWithQuery(<DemoCheckPanel />);
    await screen.findByTestId("demo-fixture-select");
    const run = screen.getByTestId("demo-run-button");
    expect(run).toBeDisabled();
    expect(screen.getByTestId("demo-check-status")).toHaveTextContent(
      "等待运行",
    );
    expect(screen.getByTestId("demo-check-status")).toHaveAttribute(
      "role",
      "status",
    );
    expect(screen.getByTestId("demo-check-status")).toHaveAttribute(
      "aria-live",
      "polite",
    );
    await userEvent.selectOptions(
      screen.getByTestId("demo-fixture-select"),
      "app_demo_step2_bad_vin",
    );
    expect(run).toBeEnabled();
  });

  it("shows the running state only after the explicit click and disables re-entry", async () => {
    let resolveCheck: ((value: Response) => void) | undefined;
    const router = fetchRouter({
      "GET /api/demo/fixtures": () =>
        router.jsonResponse({ fixtures: [demoFixtureOption()] }),
      "POST /api/demo/check": () =>
        new Promise((resolve) => {
          resolveCheck = resolve;
        }),
    });
    renderWithQuery(<DemoCheckPanel />);
    await screen.findByTestId("demo-fixture-select");
    await userEvent.selectOptions(
      screen.getByTestId("demo-fixture-select"),
      "app_demo_step2_bad_vin",
    );
    expect(router.calls.filter((call) => call.method === "POST")).toHaveLength(
      0,
    );
    await userEvent.click(screen.getByTestId("demo-run-button"));
    expect(screen.getByTestId("demo-check-status")).toHaveTextContent("校验中…");
    expect(screen.getByTestId("demo-run-button")).toBeDisabled();
    expect(
      router.calls.filter((call) => call.method === "POST"),
    ).toHaveLength(1);
    resolveCheck?.(
      router.jsonResponse(demoCheckResponse(), 200),
    );
    await waitFor(() =>
      expect(screen.getByTestId("demo-report")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("demo-check-status")).toHaveTextContent(
      "校验完成",
    );
  });

  it("shows an explicit check-failure state with only the fixed generic copy", async () => {
    const router = fetchRouter({
      "GET /api/demo/fixtures": () =>
        router.jsonResponse({ fixtures: [demoFixtureOption()] }),
      "POST /api/demo/check": () =>
        new Response(
          JSON.stringify({
            detail: {
              error: "DEMO_CHECK_FAILED",
              message: "internal /srv/secret/rules.yaml exploded",
            },
          }),
          { status: 500, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderWithQuery(<DemoCheckPanel />);
    await screen.findByTestId("demo-fixture-select");
    await userEvent.selectOptions(
      screen.getByTestId("demo-fixture-select"),
      "app_demo_step2_bad_vin",
    );
    await userEvent.click(screen.getByTestId("demo-run-button"));
    await waitFor(() =>
      expect(screen.getByTestId("demo-check-error")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("demo-check-error")).toHaveAttribute(
      "role",
      "alert",
    );
    expect(screen.getByTestId("demo-check-error")).toHaveTextContent(
      "校验失败，请稍后重试",
    );
    // the rendered failure state never reflects server/internal detail
    const rendered = screen.getByTestId("demo-check-error").textContent ?? "";
    expect(rendered).not.toContain("/srv/secret");
    expect(rendered).not.toContain("rules.yaml");
    expect(rendered).not.toContain("exploded");
    expect(rendered).not.toContain("DEMO_CHECK_FAILED");
    expect(screen.queryByTestId("demo-report")).not.toBeInTheDocument();
    expect(screen.getByTestId("demo-run-button")).toBeEnabled();
    expect(
      router.calls.filter((call) => call.method === "POST"),
    ).toHaveLength(1);
  });

  it("resets the report and mutation state when the selection changes", async () => {
    const router = fetchRouter({
      "GET /api/demo/fixtures": () =>
        router.jsonResponse({
          fixtures: [demoFixtureOption()],
        }),
      "POST /api/demo/check": () => router.jsonResponse(demoCheckResponse()),
    });
    renderWithQuery(<DemoCheckPanel />);
    await screen.findByTestId("demo-fixture-select");
    await userEvent.selectOptions(
      screen.getByTestId("demo-fixture-select"),
      "app_demo_step2_bad_vin",
    );
    await userEvent.click(screen.getByTestId("demo-run-button"));
    await waitFor(() =>
      expect(screen.getByTestId("demo-report")).toBeInTheDocument(),
    );
    await userEvent.selectOptions(
      screen.getByTestId("demo-fixture-select"),
      "",
    );
    expect(screen.queryByTestId("demo-report")).not.toBeInTheDocument();
    expect(screen.getByTestId("demo-check-status")).toHaveTextContent(
      "等待运行",
    );
    expect(screen.getByTestId("demo-run-button")).toBeDisabled();
    expect(
      router.calls.filter((call) => call.method === "POST"),
    ).toHaveLength(1);
  });

  it("renders snapshot and diff rows for the graded finding", async () => {
    const router = fetchRouter({
      "GET /api/demo/fixtures": () =>
        router.jsonResponse({ fixtures: [demoFixtureOption()] }),
      "POST /api/demo/check": () => router.jsonResponse(demoCheckResponse()),
    });
    renderWithQuery(<DemoCheckPanel />);
    await screen.findByTestId("demo-fixture-select");
    await userEvent.selectOptions(
      screen.getByTestId("demo-fixture-select"),
      "app_demo_step2_bad_vin",
    );
    await userEvent.click(screen.getByTestId("demo-run-button"));
    await waitFor(() =>
      expect(screen.getByTestId("demo-report")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("demo-config-version")).toHaveTextContent(
      "1.9.0",
    );
    const snapshots = screen.getAllByTestId("demo-snapshot-R_VIN_CROSS");
    expect(snapshots).toHaveLength(2);
    expect(snapshots[0]).toHaveTextContent("LFV3A23K5J3123456");
    expect(snapshots[1]).toHaveTextContent("LFV3A23K5J3999999");
    expect(screen.getByTestId("demo-diff-R_VIN_CROSS")).toHaveTextContent(
      "LFV3A23K5J3123456",
    );
    expect(screen.getByTestId("demo-diff-R_VIN_CROSS")).toHaveTextContent(
      "LFV3A23K5J3999999",
    );
    expect(
      screen.getByTestId("demo-evidence-link"),
    ).toHaveAttribute("href", "/api/step2/JFL25P02L086208-01");
    expect(screen.getByTestId("demo-evidence-link").textContent).toContain(
      "Step2 页序样本",
    );
  });
});
