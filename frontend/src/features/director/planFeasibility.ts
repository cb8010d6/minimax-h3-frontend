import type { PlannedScene, PlannedSceneWarningCode } from "../../api/directorTypes";

const WORD_RE = /\p{Script=Han}|[\p{L}\p{N}_]+(?:['-][\p{L}\p{N}_]+)*/gu;
const DIALOGUE_RE = /<d>\s*(?:\[[^\]]+\])?\s*(.*?)<\/d>/gis;
const EXACT_TEXT_PATTERNS = [
  /\b(?:accurate|burned-in)\s+bilingual\s+subtitles?\b/i,
  /\bdisplay\b[\s\S]{0,120}\bexactly\s+as\b/i,
  /\b(?:display|show|render|present)\b[\s\S]{0,100}\bexact(?:ly)?\b[\s\S]{0,60}\b(?:text|subtitles?|captions?|typography|wording)\b/i,
  /\b(?:on-screen|production)\s+(?:text|caption)\s+(?:reads?|reading)\b/i,
  /\btext\s+(?:static\s+and\s+)?legible\b/i,
];

function wordCount(value: string): number {
  return value.match(WORD_RE)?.length ?? 0;
}

export function planSceneWarningCodes(
  scene: Pick<PlannedScene, "prompt" | "duration_seconds">,
): PlannedSceneWarningCode[] {
  const duration = Math.max(scene.duration_seconds ?? 5, 1);
  const dialogue = Array.from(scene.prompt.matchAll(DIALOGUE_RE), (match) => match[1] ?? "");
  const spokenWords = dialogue.reduce((total, line) => total + wordCount(line), 0);
  const warnings: PlannedSceneWarningCode[] = [];

  if (wordCount(scene.prompt) > 320) warnings.push("prompt_too_dense");
  if (dialogue.length > 2 || spokenWords > Math.max(8, Math.trunc(duration * 2.4))) {
    warnings.push("dialogue_overload");
  }
  if (EXACT_TEXT_PATTERNS.some((pattern) => pattern.test(scene.prompt))) {
    warnings.push("exact_generated_text");
  }
  return warnings;
}
