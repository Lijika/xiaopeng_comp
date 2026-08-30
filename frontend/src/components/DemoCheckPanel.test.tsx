import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import type { DemoCheckResponse } from "../api/client";
import { fetchRouter, renderWithQuery } from "../test-utils";
import DemoCheckPanel from "./DemoCheckPanel";

function demoCheckResponse(): DemoCheckResponse {
  return {
    track: "C-DEMO",
    data_scope: "uploaded",
    fixture_id: null,
    application_id: "EXHIBIT-JFL25P02L080310-01-OK",
    summary: {
      consistent: 12,
      inconsistent: 1,
      uncertain: 0,
      skipped: 0,
      coverage: 1,
      total: 13,
      total_including_skipped: 13,
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
            doc_id: "lease",
            doc_type: "融资租赁合同",
            field: "vin",
            raw: "WDDUX6HB4FA197350",
            normalized: "WDDUX6HB4FA197350",
            confidence: 0.93,
          },
          {
            doc_id: "reg_cert",
            doc_type: "机动车登记证书",
            field: "vin",
            raw: "WDDUX6HB4FA197351",
            normalized: "WDDUX6HB4FA197351",
            confidence: 0.94,
          },
        ],
        diff_highlight: {
          pos: 16,
          left: "WDDUX6HB4FA197350",
          right: "WDDUX6HB4FA197351",
          detail: "末位不一致",
        },
        score: 0,
        rule_type: "exact",
        flags: [],
        reason_codes: ["VIN_MISMATCH"],
      },
    ],
    config: {
      rule_config_version: "1.9.0",
      rule_package: "auto_lease",
      rule_changelog: [],
    },
    evidence_links: [],
  };
}

function applicationFile(name = "app.json") {
  return new File(
    [
      JSON.stringify({
        application_id: "EXHIBIT-JFL25P02L080310-01-OK",
        documents: [
          {
            doc_id: "reg_cert",
            doc_type: "机动车登记证书",
            fields: { vin: { raw: "WDDUX6HB4FA197351" } },
          },
        ],
      }),
    ],
    name,
    { type: "application/json" },
  );
}

describe("DemoCheckPanel", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("uploads an application JSON and posts the parsed documents", async () => {
    const router = fetchRouter({
      "POST /api/demo/check": () => router.jsonResponse(demoCheckResponse()),
    });
    renderWithQuery(<DemoCheckPanel />);
    const input = await screen.findByTestId("demo-application-file");
    await userEvent.upload(input, applicationFile());
    await userEvent.click(screen.getByTestId("demo-run-button"));
    await waitFor(() =>
      expect(screen.getByTestId("demo-report")).toBeInTheDocument(),
    );
    const posts = router.calls.filter((call) => call.method === "POST");
    expect(posts).toHaveLength(1);
    expect(posts[0].url).toBe("/api/demo/check");
    expect(posts[0].body).toEqual({
      application: {
        application_id: "EXHIBIT-JFL25P02L080310-01-OK",
        documents: [
          {
            doc_id: "reg_cert",
            doc_type: "机动车登记证书",
            fields: { vin: { raw: "WDDUX6HB4FA197351" } },
          },
        ],
      },
    });
    expect(screen.getByTestId("demo-report-scope")).toHaveTextContent(
      "uploaded",
    );
    expect(screen.getByTestId("demo-check-item-R_VIN_CROSS")).toHaveTextContent(
      "车辆识别代号",
    );
  });

  it("keeps the run button disabled until a JSON file is chosen", async () => {
    fetchRouter({});
    renderWithQuery(<DemoCheckPanel />);
    expect(await screen.findByTestId("demo-run-button")).toBeDisabled();
    expect(screen.getByTestId("demo-check-status")).toHaveTextContent(
      "等待上传",
    );
  });

  it("rejects a non-application JSON without posting", async () => {
    const router = fetchRouter({});
    renderWithQuery(<DemoCheckPanel />);
    const input = await screen.findByTestId("demo-application-file");
    await userEvent.upload(
      input,
      new File(["{}"], "empty.json", { type: "application/json" }),
    );
    expect(await screen.findByTestId("demo-parse-error")).toHaveTextContent(
      "请上传任务4申请 JSON",
    );
    expect(screen.getByTestId("demo-run-button")).toBeDisabled();
    expect(router.calls.filter((call) => call.method === "POST")).toHaveLength(
      0,
    );
  });

  it("shows the running state only after the explicit click", async () => {
    let resolveCheck: ((value: Response) => void) | undefined;
    const router = fetchRouter({
      "POST /api/demo/check": () =>
        new Promise((resolve) => {
          resolveCheck = resolve;
        }),
    });
    renderWithQuery(<DemoCheckPanel />);
    await userEvent.upload(
      await screen.findByTestId("demo-application-file"),
      applicationFile(),
    );
    await userEvent.click(screen.getByTestId("demo-run-button"));
    expect(screen.getByTestId("demo-check-status")).toHaveTextContent("校验中…");
    expect(screen.getByTestId("demo-run-button")).toBeDisabled();
    resolveCheck?.(router.jsonResponse(demoCheckResponse(), 200));
    await waitFor(() =>
      expect(screen.getByTestId("demo-report")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("demo-check-status")).toHaveTextContent(
      "校验完成",
    );
  });

  it("shows the fixed generic failure without echoing server detail", async () => {
    fetchRouter({
      "POST /api/demo/check": () =>
        new Response(
          JSON.stringify({
            detail: {
              error: "DEMO_CHECK_FAILED",
              message: "internal /srv/secret exploded",
            },
          }),
          { status: 500, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderWithQuery(<DemoCheckPanel />);
    await userEvent.upload(
      await screen.findByTestId("demo-application-file"),
      applicationFile(),
    );
    await userEvent.click(screen.getByTestId("demo-run-button"));
    await waitFor(() =>
      expect(screen.getByTestId("demo-check-error")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("demo-check-error")).toHaveTextContent(
      "校验失败，请稍后重试",
    );
    expect(screen.getByTestId("demo-check-error").textContent).not.toContain(
      "/srv/secret",
    );
  });
});
