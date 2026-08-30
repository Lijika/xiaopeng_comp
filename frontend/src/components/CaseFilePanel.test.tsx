import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { fetchRouter, renderWithQuery } from "../test-utils";
import CaseFilePanel from "./CaseFilePanel";

describe("CaseFilePanel", () => {
  it("renders the uploaded application from the server case", async () => {
    fetchRouter({
      "GET /api/demo/case": () =>
        new Response(
          JSON.stringify({
            application_id: "EXHIBIT-1",
            file_name: "bad.json",
            report: {
              application_id: "EXHIBIT-1",
              consistent: 12,
              inconsistent: 1,
              uncertain: 0,
              skipped: 0,
            },
            checks: [
              {
                rule_id: "R_VIN_CROSS",
                name: "VIN跨单据一致",
                verdict: "inconsistent",
                severity: "critical",
                message: "合同 VIN 与登记证 VIN 不一致",
                snapshots: [],
              },
            ],
            documents: [
              {
                doc_id: "reg_cert",
                doc_type: "机动车登记证书",
                fields: [{ field: "vin", raw: "WDDUX6HB4FA197351" }],
              },
            ],
            review: null,
            approval: null,
            attachments: [],
            governance: {},
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
    });
    renderWithQuery(<CaseFilePanel />);
    await waitFor(() =>
      expect(screen.getByTestId("case-file-panel")).toHaveTextContent("EXHIBIT-1"),
    );
    expect(screen.getByTestId("case-file-issues")).toHaveTextContent("车辆识别代号");
    expect(screen.getByTestId("case-file-docs")).toHaveTextContent("机动车登记证书");
  });
});
