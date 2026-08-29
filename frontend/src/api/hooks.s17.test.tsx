import { describe, expect, it } from "vitest";
import { S17_RECEIPT_KEY, S17_REQUEST_KEY } from "./hooks";

describe("S17 query keys", () => {
  it("separates export state and receipt state", () => {
    expect(S17_REQUEST_KEY("req")).toEqual(["s17", "exports", "req"]);
    expect(S17_RECEIPT_KEY("req")).toEqual(["s17", "exports", "req", "receipt"]);
  });
});
