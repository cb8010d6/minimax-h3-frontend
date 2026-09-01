import { describe, expect, test } from "bun:test";
import { ApiError, apiErrorMessage } from "../src/api/client";

describe("apiErrorMessage", () => {
  test("shows the API error returned by the export endpoint", () => {
    const error = new ApiError(409, { error: "2 selected clip(s) have missing video files." });

    expect(apiErrorMessage(error, "Export failed.")).toBe(
      "2 selected clip(s) have missing video files.",
    );
  });

  test("falls back when the error has no displayable API message", () => {
    expect(apiErrorMessage(new Error("network"), "Export failed.")).toBe("Export failed.");
  });
});
