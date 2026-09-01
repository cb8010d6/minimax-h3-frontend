import { describe, expect, test } from "bun:test";
import type { GenerationJob } from "../src/api/types";
import { canSelectHistoryVideo, moveSelectedId, toggleSelectedId } from "../src/features/queue/videoSelection";

function job(overrides: Partial<GenerationJob> = {}): GenerationJob {
  return {
    id: 1,
    status: "done",
    content_type: "video",
    video_url: "/media/video.mp4",
    ...overrides,
  } as GenerationJob;
}

describe("history video selection", () => {
  test("accepts successful standalone video renders only", () => {
    expect(canSelectHistoryVideo(job(), new Set())).toBe(true);
    expect(canSelectHistoryVideo(job({ status: "processing" }), new Set())).toBe(false);
    expect(canSelectHistoryVideo(job({ content_type: "image" }), new Set())).toBe(false);
    expect(canSelectHistoryVideo(job({ video_url: null }), new Set())).toBe(false);
    expect(canSelectHistoryVideo(job(), new Set([1]))).toBe(false);
  });

  test("toggles ids without changing the order of existing selections", () => {
    expect(toggleSelectedId([4, 9], 3)).toEqual([4, 9, 3]);
    expect(toggleSelectedId([4, 9], 4)).toEqual([9]);
  });

  test("moves an item by one position and leaves boundary moves unchanged", () => {
    expect(moveSelectedId([4, 9, 3], 2, -1)).toEqual([4, 3, 9]);
    expect(moveSelectedId([4, 9, 3], 0, -1)).toEqual([4, 9, 3]);
    expect(moveSelectedId([4, 9, 3], 2, 1)).toEqual([4, 9, 3]);
  });
});

