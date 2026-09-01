import { useEffect, useRef, useState, type DragEvent, type KeyboardEvent } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { apiErrorMessage } from "../../api/client";
import {
  useAssembleProject,
  useCancelAllRenders,
  useCheckContinuity,
  useCreateClip,
  useDirectorProject,
  useRenderAllDirty,
  useReorderClip,
  useUpdateClip,
  useUpdateDirectorProject,
} from "../../api/directorQueries";
import { useConfig, usePresets, useRefinePrompt } from "../../api/queries";
import { CONTINUATION_CAPABLE_MODES, MODE_LABELS } from "../../api/types";
import type { ModelVariant } from "../../api/types";
import type { Clip } from "../../api/directorTypes";
import { CloseIcon } from "../shared/Icon";
import { InfoTooltip } from "../shared/InfoTooltip";
import { useI18n } from "../../i18n";
import { ClipBox } from "./ClipBox";
import { ClipEditorPanel } from "./ClipEditorPanel";
import { ProjectResourcesPanel } from "./ProjectResourcesPanel";
import { availableDirectorModelVariants, mergeDirectorQualityPresets } from "./directorSettings";
import { ScriptPlanModal } from "./ScriptPlanModal";

// Director clips are scoped to the video-content modes only (see this
// feature's own purpose -- sequencing video clips); image/audio modes stay
// exclusive to the main Generate screen.
type NewClipMode = "t2v" | "i2v" | "r2v";

const NEW_CLIP_MODES: { mode: NewClipMode; label: string }[] = [
  { mode: "t2v", label: "+ Text clip" },
  { mode: "i2v", label: "+ Image clip" },
  { mode: "r2v", label: "+ Reference clip" },
];

// Truncated so a large project's context doesn't balloon every refine/chat
// call's token usage -- this is meant to give the LLM a sense of what's
// already happened, not the full text of every earlier clip.
const PREVIOUS_CLIP_PROMPT_MAX_CHARS = 200;

// A short summary of every clip before `beforeOrder`, for AI refine/chat's
// extra_context (see ClipEditorPanel's previousClipsContext prop) -- the
// raw (not AI-refined) prompt, since that's the shorter, more direct
// version of what the user actually asked for.
function buildPreviousClipsContext(clips: Clip[], beforeOrder: number): string {
  const previous = clips.filter((c) => c.order < beforeOrder);
  if (previous.length === 0) return "";
  const lines = previous.map((c) => {
    const prompt = c.prompt.trim();
    const truncated =
      prompt.length > PREVIOUS_CLIP_PROMPT_MAX_CHARS ? `${prompt.slice(0, PREVIOUS_CLIP_PROMPT_MAX_CHARS)}…` : prompt;
    const tag = `${MODE_LABELS[c.mode]}${c.continues_previous ? ", continues previous" : ""}`;
    return `${c.order + 1}. (${tag}) ${truncated || "(empty prompt)"}`;
  });
  return `Previous clips in this project, in order:\n${lines.join("\n")}`;
}

export function ProjectBoard() {
  const { t } = useI18n();
  const { projectId: projectIdParam } = useParams<{ projectId: string }>();
  // Number(undefined) and Number() of a malformed param both come out NaN --
  // still type `number` (never null/undefined), so this can be used
  // directly in function bodies declared later without TypeScript's
  // narrowing-doesn't-cross-function-declaration-boundaries limitation
  // (const projectId: number | null narrowed by an early-return guard
  // isn't seen as narrowed inside a nested `function` declared afterward,
  // only inline closures at the same scope depth) -- see the invalid-id
  // check below instead.
  const projectId = Number(projectIdParam);
  const navigate = useNavigate();
  const config = useConfig();

  const project = useDirectorProject(Number.isNaN(projectId) ? null : projectId);
  const updateProject = useUpdateDirectorProject();
  const createClip = useCreateClip();
  const reorderClip = useReorderClip();
  const renderAllDirty = useRenderAllDirty();
  const cancelAllRenders = useCancelAllRenders();
  const assembleProject = useAssembleProject();
  const checkContinuity = useCheckContinuity();
  const refinePrompt = useRefinePrompt();
  const updateClip = useUpdateClip();

  // Prefetched so "+ Add clip" can create one immediately with a sensible
  // default duration, without a request-then-wait step in between.
  const projectModelVariant = project.data?.model_variant ?? "fp8";
  const projectAspectRatio = project.data?.aspect_ratio ?? null;
  const presetsByMode = {
    t2v: usePresets("t2v", projectModelVariant, projectAspectRatio),
    i2v: usePresets("i2v", projectModelVariant, projectAspectRatio),
    r2v: usePresets("r2v", projectModelVariant, projectAspectRatio),
  };

  const [selectedClipId, setSelectedClipId] = useState<number | null>(null);
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  const [promptDraft, setPromptDraft] = useState("");
  const [planModalOpen, setPlanModalOpen] = useState(false);
  const [continuityReportOpen, setContinuityReportOpen] = useState(false);
  const [exportClipIds, setExportClipIds] = useState<number[]>([]);
  const [draggedClipId, setDraggedClipId] = useState<number | null>(null);
  const [dropTargetClipId, setDropTargetClipId] = useState<number | null>(null);
  const exportSelectionProjectRef = useRef<number | null>(null);
  const knownClipIdsRef = useRef<Set<number>>(new Set());
  const [refineAllProgress, setRefineAllProgress] = useState<{ done: number; total: number; failed: number } | null>(
    null,
  );

  // Deliberately narrower than "whenever project.data changes" -- this
  // project is polled every few seconds while a clip is rendering (see
  // useDirectorProject), and project.data is a fresh object reference on
  // every poll tick; depending on the whole object would clobber an
  // in-progress edit mid-typing. Only resync when actually switching
  // projects or when the server-side value itself changes (e.g. after
  // this same save round-trips, or another tab/client edits it).
  useEffect(() => {
    if (project.data) setPromptDraft(project.data.overarching_prompt);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project.data?.id, project.data?.overarching_prompt]);

  const clipIdsKey = project.data?.clips.map((clip) => clip.id).join(",") ?? "";
  useEffect(() => {
    if (!project.data) return;
    const currentIds = project.data.clips.map((clip) => clip.id);
    if (exportSelectionProjectRef.current !== project.data.id) {
      exportSelectionProjectRef.current = project.data.id;
      knownClipIdsRef.current = new Set(currentIds);
      setExportClipIds(currentIds);
      return;
    }
    const currentSet = new Set(currentIds);
    const newlyAdded = currentIds.filter((id) => !knownClipIdsRef.current.has(id));
    knownClipIdsRef.current = currentSet;
    setExportClipIds((selected) => [
      ...selected.filter((id) => currentSet.has(id)),
      ...newlyAdded,
    ]);
    // clipIdsKey deliberately captures membership changes without reacting
    // to every polling response's fresh clips array identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project.data?.id, clipIdsKey]);

  if (Number.isNaN(projectId)) return <p className="error">{t("director.invalidProject", "Invalid project.")}</p>;

  async function handleAddClip(mode: NewClipMode) {
    const proj = project.data;
    if (!proj) return;
    const presets = presetsByMode[mode].data;
    // Quality is project-wide (proj.quality_label) -- match this mode's
    // own preset row for that shared label, same fallback the backend
    // uses if the label doesn't have a row for this mode (see
    // director/services.py's resolve_preset_for_mode()).
    const preset = presets?.find((p) => p.label === proj.quality_label) ?? presets?.[0];
    const duration = preset?.durations[0];
    if (!duration) return;
    const lastClip = proj.clips[proj.clips.length - 1];
    const continuesPrevious = !!lastClip && CONTINUATION_CAPABLE_MODES.has(mode);
    const clip = await createClip.mutateAsync({ projectId, mode, durationId: duration.id, continuesPrevious });
    setSelectedClipId(clip.id);
  }

  async function saveTitle() {
    setEditingTitle(false);
    if (!project.data || titleDraft.trim() === project.data.title.trim()) return;
    await updateProject.mutateAsync({ projectId, title: titleDraft.trim() });
  }

  async function savePrompt() {
    if (!project.data || promptDraft === project.data.overarching_prompt) return;
    await updateProject.mutateAsync({ projectId, overarchingPrompt: promptDraft });
  }

  async function handleAspectRatioChange(aspectRatio: string) {
    await updateProject.mutateAsync({ projectId, aspectRatio });
  }

  async function handleQualityChange(qualityLabel: string) {
    await updateProject.mutateAsync({ projectId, qualityLabel });
  }

  async function handleTurboChange(useTurbo: boolean) {
    await updateProject.mutateAsync({ projectId, useTurbo });
  }

  async function handleModelVariantChange(modelVariant: ModelVariant) {
    await updateProject.mutateAsync({ projectId, modelVariant });
  }

  function handleTitleKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter") e.currentTarget.blur();
    if (e.key === "Escape") setEditingTitle(false);
  }

  async function handleCheckContinuity() {
    setContinuityReportOpen(true);
    await checkContinuity.mutateAsync(projectId);
  }

  function toggleExportClip(clipId: number) {
    setExportClipIds((selected) =>
      selected.includes(clipId) ? selected.filter((id) => id !== clipId) : [...selected, clipId],
    );
  }

  function handleClipDragStart(event: DragEvent<HTMLSpanElement>, clipId: number) {
    setDraggedClipId(clipId);
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", String(clipId));
  }

  function handleClipDragOver(event: DragEvent<HTMLDivElement>, clipId: number) {
    if (draggedClipId === null || draggedClipId === clipId) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    setDropTargetClipId(clipId);
  }

  function handleClipDrop(event: DragEvent<HTMLDivElement>, targetClip: Clip) {
    event.preventDefault();
    const sourceId = draggedClipId ?? Number(event.dataTransfer.getData("text/plain"));
    setDraggedClipId(null);
    setDropTargetClipId(null);
    if (!Number.isInteger(sourceId) || sourceId === targetClip.id) return;
    reorderClip.mutate({ projectId, clipId: sourceId, order: targetClip.order });
  }

  // Re-runs AI refine for every clip that already has an AI-refined
  // prompt, using each clip's current context (project prompt, resources,
  // its own place in the sequence) -- for when the project prompt or a
  // shared reference changed after a clip was refined, leaving its
  // improved_prompt stale. Only touches clips that opted into AI refine in
  // the first place (a non-blank improved_prompt); a clip that's always
  // just used its raw prompt keeps doing that. Sequential, not parallel --
  // gentler on a self-hosted LLM server, and each clip's own extra_context
  // includes the clips before it, so there's no benefit to racing them.
  async function handleRefineAll() {
    const proj = project.data;
    if (!proj) return;
    const targets = proj.clips.filter((c) => c.improved_prompt.trim());
    if (targets.length === 0) return;
    setRefineAllProgress({ done: 0, total: targets.length, failed: 0 });
    let failed = 0;
    for (const clip of targets) {
      const referenceLabels = [...resourceLabels, ...clip.references.map((r) => r.label)];
      const extraContext = [proj.overarching_prompt, buildPreviousClipsContext(proj.clips, clip.order)]
        .filter(Boolean)
        .join("\n\n");
      try {
        const result = await refinePrompt.mutateAsync({
          mode: clip.mode,
          rawPrompt: clip.prompt,
          referenceLabels,
          extraContext,
          isContinuation: clip.continues_previous,
        });
        await updateClip.mutateAsync({ projectId, clipId: clip.id, improvedPrompt: result.improved_prompt });
      } catch {
        failed += 1;
      }
      setRefineAllProgress((prev) => (prev ? { ...prev, done: prev.done + 1, failed } : prev));
    }
  }

  const dirtyCount = project.data?.clips.filter((c) => c.needs_render).length ?? 0;
  const refinableCount = project.data?.clips.filter((c) => c.improved_prompt.trim()).length ?? 0;
  const activeCount =
    project.data?.clips.filter((c) => c.current_job_status === "queued" || c.current_job_status === "processing")
      .length ?? 0;
  const selectedClip = project.data?.clips.find((c) => c.id === selectedClipId) ?? null;
  const qualityPresets = mergeDirectorQualityPresets({
    t2v: presetsByMode.t2v.data,
    i2v: presetsByMode.i2v.data,
    r2v: presetsByMode.r2v.data,
  });
  const availableModelVariants = availableDirectorModelVariants(
    config.data?.available_model_keys ?? [],
    (project.data?.clips.map((clip) => clip.mode).filter((mode) =>
      mode === "t2v" || mode === "i2v" || mode === "r2v",
    ) ?? []) as ("t2v" | "i2v" | "r2v")[],
  );
  const modelVariants = availableModelVariants.includes(projectModelVariant)
    ? availableModelVariants
    : [projectModelVariant, ...availableModelVariants];
  const selectedExportClips = project.data?.clips.filter((clip) => exportClipIds.includes(clip.id)) ?? [];
  const canAssemble = selectedExportClips.length > 0 && selectedExportClips.every((c) => c.video_url && !c.needs_render);
  // Only r2v clips can actually wire a shared resource into a render (see
  // backend director/services.py's project_requires_reference_mode()) --
  // once the project has any, every clip must be one.
  const hasResources = !!project.data?.resources.length;
  const visibleClipModes = hasResources ? NEW_CLIP_MODES.filter((m) => m.mode === "r2v") : NEW_CLIP_MODES;
  const resourceLabels = project.data?.resources.map((r) => r.token_label) ?? [];
  // Mirrors ProjectResourcesPanel's own hasNonReferenceClips check -- shown
  // in ScriptPlanModal's no-references nudge so it doesn't point the user
  // at an "add a reference" action that's actually blocked right now.
  const canAddResources = !project.data?.clips.some((c) => c.mode !== "r2v");
  const previousClipsContext = selectedClip && project.data ? buildPreviousClipsContext(project.data.clips, selectedClip.order) : "";

  return (
    <section className="director-board">
      <button type="button" className="link-button director-back-link" onClick={() => navigate("/director")}>
        ← {t("director.allProjects", "All projects")}
      </button>

      {project.isLoading && <p className="hint">{t("common.loading", "Loading…")}</p>}
      {project.isError && <p className="error">{t("director.projectLoadError", "Couldn't load this project.")}</p>}

      {project.data && (
        <>
          {editingTitle ? (
            <input
              type="text"
              className="modal-title-input"
              autoFocus
              value={titleDraft}
              maxLength={200}
              onChange={(e) => setTitleDraft(e.target.value)}
              onBlur={() => void saveTitle()}
              onKeyDown={handleTitleKeyDown}
            />
          ) : (
            <h1
              className="modal-title-editable"
              onClick={() => {
                setTitleDraft(project.data.title);
                setEditingTitle(true);
              }}
              title={t("director.clickRename", "Click to rename")}
            >
              {project.data.title || `${t("director.untitled", "Untitled project")} #${project.data.id}`}
            </h1>
          )}

          <fieldset className="prompt-fieldset">
            <legend>{t("director.overarchingPrompt", "Overarching prompt")}</legend>
            <p className="hint">
              {t("director.overarchingHint", "Shared world, setting and character context used by every generated clip; changing it marks clips for re-render.")}
            </p>
            <textarea
              rows={3}
              value={promptDraft}
              onChange={(e) => setPromptDraft(e.target.value)}
              onBlur={() => void savePrompt()}
              placeholder={t("director.overarchingPlaceholder", "e.g. A cyberpunk city at night, neon-lit rain-slicked streets…")}
            />
          </fieldset>

          <fieldset className="prompt-fieldset">
            <legend>{t("director.aspectQuality", "Aspect ratio & quality")}</legend>
            <p className="hint">
              {t("director.aspectQualityHint", "Shared by every generated clip. Changing either setting requires re-render because continuity uses a consistent canvas.")}
            </p>
            <div className="toolbar">
              <label className="toolbar-control">
                <span>{t("generate.aspect", "Aspect ratio")}</span>
                <select value={project.data.aspect_ratio} onChange={(e) => void handleAspectRatioChange(e.target.value)}>
                  {config.data?.aspect_ratios.map((ratio) => (
                    <option key={ratio.value} value={ratio.value}>
                      {ratio.label}
                    </option>
                  ))}
                  {!config.data?.aspect_ratios.some((r) => r.value === project.data.aspect_ratio) && (
                    <option value={project.data.aspect_ratio}>{project.data.aspect_ratio}</option>
                  )}
                </select>
              </label>
              <label className="toolbar-control">
                <span>{t("generate.quality", "Quality")}</span>
                <select value={project.data.quality_label} onChange={(e) => void handleQualityChange(e.target.value)}>
                  {qualityPresets.map((preset) => (
                    <option key={preset.label} value={preset.label}>
                      {preset.label} ({preset.megapixels}MP{preset.is_draft ? ", draft" : ""})
                    </option>
                  ))}
                  {!qualityPresets.some((preset) => preset.label === project.data.quality_label) && (
                    <option value={project.data.quality_label}>{project.data.quality_label}</option>
                  )}
                </select>
              </label>
              <label className="toolbar-control">
                <span>{t("generate.model", "Model")}</span>
                <select
                  value={project.data.model_variant}
                  onChange={(e) => void handleModelVariantChange(e.target.value as ModelVariant)}
                >
                  {modelVariants.map((variant) => (
                    <option key={variant} value={variant}>
                      {variant === "fp8"
                        ? t("generate.modelFp8", "FP8 (faster / lower VRAM)")
                        : t("generate.modelInt8", "INT8 (alternate quantization)")}
                    </option>
                  ))}
                </select>
              </label>
              {config.data?.turbo_level != null &&
                (config.data.turbo_level === 2 ? (
                  <span className="hint toolbar-control-checkbox">
                    🚀 Turbo mode is always on for this deployment.
                  </span>
                ) : (
                  <label className="toolbar-control-checkbox">
                    <input
                      type="checkbox"
                      checked={project.data.use_turbo}
                      onChange={(e) => void handleTurboChange(e.target.checked)}
                    />{" "}
                    🚀 Turbo mode
                    <InfoTooltip
                      text={`Renders every clip at just ${config.data.turbo_steps_t2v_i2v} (t2v/i2v) or ${config.data.turbo_steps_r2v} (r2v) sampler steps via a distilled LoRA -- much faster, but softer/less consistent than a full render. Overrides quality's own steps for every clip in this project.`}
                    />
                  </label>
                ))}
            </div>
          </fieldset>

          <ProjectResourcesPanel project={project.data} />

          <div className="director-board-actions">
            <button
              type="button"
              onClick={() => setPlanModalOpen(true)}
              title={
                project.data.script_text.trim()
                  ? "Reopens with the script last used to generate this project's clips"
                  : undefined
              }
            >
              {t("director.generateFromScript", "Generate from script…")}
            </button>
            {config.data?.llm_enabled && (
              <button
                type="button"
                onClick={() => void handleCheckContinuity()}
                disabled={checkContinuity.isPending || project.data.clips.length === 0}
              >
                {checkContinuity.isPending
                  ? t("director.checking", "Checking…")
                  : t("director.checkContinuity", "Check continuity")}
              </button>
            )}
            {config.data?.llm_enabled && refinableCount > 0 && (
              <button
                type="button"
                onClick={() => void handleRefineAll()}
                disabled={!!refineAllProgress && refineAllProgress.done < refineAllProgress.total}
                title="Re-run AI refine for every clip that already has an AI-refined prompt -- useful after changing the overarching prompt or a shared reference."
              >
                {refineAllProgress && refineAllProgress.done < refineAllProgress.total
                  ? `Re-refining… (${refineAllProgress.done}/${refineAllProgress.total})`
                  : `Re-refine all (${refinableCount})`}
              </button>
            )}
            <button
              type="button"
              className="button button-primary"
              onClick={() => renderAllDirty.mutate(projectId)}
              disabled={renderAllDirty.isPending || dirtyCount === 0}
            >
              {renderAllDirty.isPending
                ? t("director.starting", "Starting…")
                : t("director.renderDirty", "Render all dirty ({count})", { count: dirtyCount })}
            </button>
            {activeCount > 0 && (
              <button
                type="button"
                className="button-danger"
                onClick={() => cancelAllRenders.mutate(projectId)}
                disabled={cancelAllRenders.isPending}
              >
                <span aria-hidden="true">⏹</span>{" "}
                {cancelAllRenders.isPending
                  ? t("director.cancelling", "Cancelling…")
                  : t("director.cancelAll", "Cancel all ({count})", { count: activeCount })}
              </button>
            )}
            <button
              type="button"
              onClick={() => assembleProject.mutate({ projectId, clipIds: exportClipIds })}
              disabled={assembleProject.isPending || !canAssemble}
              title={canAssemble ? undefined : t("director.exportUnavailable", "Select at least one rendered, up-to-date clip.")}
            >
              {assembleProject.isPending
                ? t("director.assembling", "Assembling…")
                : t("director.exportSelected", "Export selected ({count})", { count: exportClipIds.length })}
            </button>
            {project.data.assembled_video_url && (
              <a href={project.data.assembled_video_url} download className="button">
                <span aria-hidden="true">⬇</span> {t("director.downloadExport", "Download export")}
              </a>
            )}
          </div>
          {assembleProject.isError && (
            <p className="error">
              {apiErrorMessage(
                assembleProject.error,
                t("director.exportError", "Couldn't assemble the export. Try again."),
              )}
            </p>
          )}
          {refineAllProgress && refineAllProgress.done === refineAllProgress.total && (
            <p className={refineAllProgress.failed ? "error" : "hint"}>
              Re-refined {refineAllProgress.total - refineAllProgress.failed}/{refineAllProgress.total} clip
              {refineAllProgress.total === 1 ? "" : "s"}
              {refineAllProgress.failed ? ` — ${refineAllProgress.failed} failed, try again.` : "."}
            </p>
          )}

          <div className="director-export-selection">
            <strong>{t("director.exportSelection", "Export selection: {selected}/{total}", {
              selected: exportClipIds.length,
              total: project.data.clips.length,
            })}</strong>
            <span className="hint">{t("director.exportOrderHint", "Export follows the timeline order. Drag the ⠿ handle to reorder.")}</span>
            <button type="button" className="link-button" onClick={() => setExportClipIds(project.data.clips.map((clip) => clip.id))}>
              {t("director.selectAll", "Select all")}
            </button>
            <button type="button" className="link-button" onClick={() => setExportClipIds([])}>
              {t("common.clear", "Clear")}
            </button>
          </div>

          <div className="director-timeline" onDragLeave={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDropTargetClipId(null);
          }}>
            {project.data.clips.map((clip, index) => (
              <ClipBox
                key={clip.id}
                clip={clip}
                isFirst={index === 0}
                isLast={index === project.data.clips.length - 1}
                onOpen={() => setSelectedClipId(clip.id)}
                onMoveUp={() => reorderClip.mutate({ projectId, clipId: clip.id, order: clip.order - 1 })}
                onMoveDown={() => reorderClip.mutate({ projectId, clipId: clip.id, order: clip.order + 1 })}
                selectedForExport={exportClipIds.includes(clip.id)}
                isDragging={draggedClipId === clip.id}
                isDropTarget={dropTargetClipId === clip.id}
                onToggleExport={() => toggleExportClip(clip.id)}
                onDragStart={(event) => handleClipDragStart(event, clip.id)}
                onDragOver={(event) => handleClipDragOver(event, clip.id)}
                onDrop={(event) => handleClipDrop(event, clip)}
                onDragEnd={() => {
                  setDraggedClipId(null);
                  setDropTargetClipId(null);
                }}
              />
            ))}

            <div className="director-add-clip">
              {visibleClipModes.map(({ mode, label }) => (
                <button type="button" key={mode} onClick={() => void handleAddClip(mode)} disabled={createClip.isPending}>
                  {t(`director.add.${mode}`, label)}
                </button>
              ))}
            </div>
          </div>
          {hasResources && (
            <p className="hint">
              {t("director.sharedReferencesMode", "This project has shared references, so every generated clip must use reference mode while they are attached.")}
            </p>
          )}
          {project.data.clips.length === 0 && (
            <p className="empty-state">{t("director.noClips", "No clips yet — add one above to start the sequence.")}</p>
          )}
        </>
      )}

      {selectedClip && (
        <ClipEditorPanel
          projectId={projectId}
          clip={selectedClip}
          isFirstClip={selectedClip.order === 0}
          overarchingPrompt={project.data?.overarching_prompt ?? ""}
          previousClipsContext={previousClipsContext}
          projectResourceLabels={resourceLabels}
          allClipsReference={canAddResources}
          // selectedClip can only be non-null when project.data is (it's
          // derived from project.data?.clips), so the ?? false is just to
          // satisfy the type checker, never taken in practice.
          useTurbo={project.data?.use_turbo ?? false}
          onClose={() => setSelectedClipId(null)}
        />
      )}

      {planModalOpen && (
        <ScriptPlanModal
          projectId={projectId}
          hasExistingClips={!!project.data?.clips.length}
          projectResources={project.data?.resources ?? []}
          canAddResources={canAddResources}
          projectAspectRatio={project.data?.aspect_ratio ?? "16:9"}
          initialIdeaText={project.data?.script_text ?? ""}
          onClose={() => setPlanModalOpen(false)}
        />
      )}

      {continuityReportOpen && (
        <div className="modal-overlay" onClick={() => setContinuityReportOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <button
              type="button"
              className="modal-close"
              onClick={() => setContinuityReportOpen(false)}
              aria-label={t("common.close", "Close")}
            >
              <CloseIcon size={16} />
            </button>
            <h2>{t("director.checkContinuity", "Continuity check")}</h2>
            {checkContinuity.isPending && <p className="hint">{t("director.reviewingPrompts", "Reviewing every clip's prompt…")}</p>}
            {checkContinuity.isError && <p className="error">{t("director.continuityError", "Couldn't run the continuity check. Try again.")}</p>}
            {checkContinuity.data && <p className="continuity-report">{checkContinuity.data.report}</p>}
          </div>
        </div>
      )}
    </section>
  );
}
