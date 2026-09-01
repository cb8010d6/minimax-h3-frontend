import { describe, expect, test } from "bun:test";

import { planSceneWarningCodes } from "../src/features/director/planFeasibility";

describe("planSceneWarningCodes", () => {
  test("warns about overloaded dialogue and exact generated text", () => {
    const prompt =
      "Show accurate bilingual subtitles. " +
      "<d>[English] This line contains far too many spoken words to fit naturally.</d> " +
      "<d>[English] A second line makes the timing substantially worse.</d>";
    expect(planSceneWarningCodes({ prompt, duration_seconds: 5 })).toEqual([
      "dialogue_overload",
      "exact_generated_text",
    ]);
  });

  test("warns when a short scene prompt is over 320 words", () => {
    const prompt = `detailed_description: ${"detail ".repeat(321)}`;
    expect(planSceneWarningCodes({ prompt, duration_seconds: 8 })).toContain("prompt_too_dense");
  });

  test("recomputes cleanly after the reviewer simplifies a scene", () => {
    const prompt = "A woman pauses at the doorway. <d>[English] Wait.</d>";
    expect(planSceneWarningCodes({ prompt, duration_seconds: 7 })).toEqual([]);
  });

  test("counts Chinese dialogue against the timing budget", () => {
    const prompt = "<d>[中文]这是一句明显无法在五秒内自然说完的中文对白内容</d>";
    expect(planSceneWarningCodes({ prompt, duration_seconds: 5 })).toContain("dialogue_overload");
  });
});
