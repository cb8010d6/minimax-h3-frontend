import type { Mode, ModelVariant, RenderPreset } from "../../api/types";

type DirectorVideoMode = Extract<Mode, "t2v" | "i2v" | "r2v">;
type PresetsByMode = Partial<Record<DirectorVideoMode, readonly RenderPreset[]>>;

const DIRECTOR_MODE_PRIORITY: DirectorVideoMode[] = ["r2v", "i2v", "t2v"];
const MODEL_VARIANTS: ModelVariant[] = ["fp8", "int8"];

export function mergeDirectorQualityPresets(groups: PresetsByMode): RenderPreset[] {
  const seen = new Set<string>();
  const merged: RenderPreset[] = [];
  for (const mode of DIRECTOR_MODE_PRIORITY) {
    for (const preset of groups[mode] ?? []) {
      if (seen.has(preset.label)) continue;
      seen.add(preset.label);
      merged.push(preset);
    }
  }
  return merged;
}

export function availableDirectorModelVariants(
  availableModelKeys: readonly string[],
  projectModes: readonly DirectorVideoMode[],
): ModelVariant[] {
  const keys = new Set(availableModelKeys);
  const requiredFamilies = new Set(
    (projectModes.length ? projectModes : ["r2v"]).map((mode) => (mode === "r2v" ? "ref2va" : "fl2va")),
  );
  return MODEL_VARIANTS.filter((variant) =>
    [...requiredFamilies].every((family) => keys.has(`${family}:${variant}`)),
  );
}
