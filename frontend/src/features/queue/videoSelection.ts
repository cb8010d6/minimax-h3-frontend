import type { GenerationJob } from "../../api/types";

/** A history render is importable only when Director can reuse its media. */
export function canSelectHistoryVideo(job: GenerationJob, projectJobIds: ReadonlySet<number>): boolean {
  return job.status === "done" && job.content_type === "video" && !!job.video_url && !projectJobIds.has(job.id);
}

export function toggleSelectedId(selected: readonly number[], id: number): number[] {
  return selected.includes(id) ? selected.filter((candidate) => candidate !== id) : [...selected, id];
}

export function moveSelectedId(selected: readonly number[], index: number, offset: -1 | 1): number[] {
  const target = index + offset;
  if (index < 0 || index >= selected.length || target < 0 || target >= selected.length) return [...selected];
  const next = [...selected];
  [next[index], next[target]] = [next[target], next[index]];
  return next;
}

