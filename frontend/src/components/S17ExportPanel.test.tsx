import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import S17ExportPanel from "./S17ExportPanel";

describe("S17ExportPanel", () => {
  it("renders the controlled export seam", () => {
    render(<QueryClientProvider client={new QueryClient()}><S17ExportPanel /></QueryClientProvider>);
    expect(screen.getByTestId("s17-export-panel")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "预览导出范围" })).toBeInTheDocument();
  });
});
