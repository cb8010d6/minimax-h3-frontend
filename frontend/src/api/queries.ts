// TanStack Query hooks wrapping apiFetch -- one place for query keys,
// polling behavior, and cache invalidation, so features/* stay UI-only.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "./client";
import type {
  AppConfig,
  CatalogLevel,
  ChatMessage,
  CurrentUser,
  DurationEstimateResponse,
  GenerationJob,
  GenerationJobDetail,
  Invite,
  JobFolder,
  Mode,
  QualityCatalog,
  QueueEstimate,
  RenderPreset,
} from "./types";

const ACTIVE_JOB_STATUSES = new Set(["queued", "processing"]);

export function useConfig() {
  return useQuery({ queryKey: ["config"], queryFn: () => apiFetch<AppConfig>("/config/") });
}

export function useCurrentUser() {
  return useQuery({ queryKey: ["me"], queryFn: () => apiFetch<CurrentUser>("/me/") });
}

export function usePresets(mode: Mode) {
  return useQuery({
    queryKey: ["presets", mode],
    queryFn: () => apiFetch<RenderPreset[]>(`/presets/?mode=${mode}`),
  });
}

export function useQueueEstimate(durationId: number | null) {
  return useQuery({
    queryKey: ["queue-estimate", durationId],
    queryFn: () =>
      apiFetch<QueueEstimate>(
        durationId != null ? `/queue-estimate/?duration_id=${durationId}` : "/queue-estimate/",
      ),
    refetchInterval: 15000,
  });
}

export function useJobs() {
  return useQuery({
    queryKey: ["jobs"],
    queryFn: () => apiFetch<GenerationJob[]>("/jobs/"),
    refetchInterval: (query) => {
      const jobs = query.state.data;
      const hasActive = jobs?.some((job) => ACTIVE_JOB_STATUSES.has(job.status));
      return hasActive ? 4000 : false;
    },
  });
}

export function useJob(jobId: number | null) {
  return useQuery({
    queryKey: ["job", jobId],
    queryFn: () => apiFetch<GenerationJobDetail>(`/jobs/${jobId}/`),
    enabled: jobId != null,
    refetchInterval: (query) =>
      query.state.data && ACTIVE_JOB_STATUSES.has(query.state.data.status) ? 3000 : false,
  });
}

export interface CreateJobInput {
  mode: Mode;
  durationId: number;
  aspectRatio: string;
  rawPrompt: string;
  improvedPrompt?: string;
  referenceImages?: File[];
  referenceAudio?: File[];
  referenceVideo?: File[];
  /** If chat was used to draft this prompt, its full transcript -- only
   * ever persisted (as PromptChatSession/PromptChatMessage) as part of
   * this same job-creation call; the live chat itself never touches the
   * DB, see useChatReply() below. */
  chatTranscript?: ChatMessage[];
  /** Only meaningful when AppConfig.spectrum_level is 0 or 1 (an optional
   * toggle) -- see extras.md#spectrum. Always sent explicitly (never
   * omitted) so the backend can tell "unchecked" apart from "not sent",
   * which matters for a level-1 (default-on) toggle -- see
   * generation/api.py::_resolve_use_spectrum. */
  useSpectrum?: boolean;
  /** Only meaningful when AppConfig.turbo_level is 0 or 1 (an optional
   * toggle) -- see extras.md#turbo. Always sent explicitly (never omitted),
   * same reasoning as useSpectrum above -- see
   * generation/api.py::_resolve_use_turbo. */
  useTurbo?: boolean;
  /** Folders (see JobFolder) to file this job under immediately, e.g. from
   * GenerateScreen's pre-queue folder picker -- every id must be one of the
   * requesting user's own folders. */
  folderIds?: number[];
}

export function useCreateJob() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: CreateJobInput) => {
      const form = new FormData();
      form.set("mode", input.mode);
      form.set("duration_id", String(input.durationId));
      form.set("aspect_ratio", input.aspectRatio);
      form.set("raw_prompt", input.rawPrompt);
      if (input.improvedPrompt) form.set("improved_prompt", input.improvedPrompt);
      for (const file of input.referenceImages ?? []) form.append("reference_images", file);
      for (const file of input.referenceAudio ?? []) form.append("reference_audio", file);
      for (const file of input.referenceVideo ?? []) form.append("reference_video", file);
      if (input.chatTranscript?.length) {
        form.set("chat_transcript", JSON.stringify(input.chatTranscript));
      }
      if (input.useSpectrum !== undefined) {
        form.set("use_spectrum", input.useSpectrum ? "true" : "false");
      }
      if (input.useTurbo !== undefined) {
        form.set("use_turbo", input.useTurbo ? "true" : "false");
      }
      for (const folderId of input.folderIds ?? []) form.append("folder_ids", String(folderId));
      return apiFetch<GenerationJobDetail>("/jobs/", { method: "POST", body: form });
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
      void queryClient.invalidateQueries({ queryKey: ["queue-estimate"] });
      // Refreshes folder job_counts when the job was filed under any at
      // creation time (see CreateJobInput.folderIds) -- harmless no-op
      // refetch otherwise.
      void queryClient.invalidateQueries({ queryKey: ["folders"] });
    },
  });
}

export function useDeleteJob() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (jobId: number) => apiFetch<void>(`/jobs/${jobId}/`, { method: "DELETE" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
  });
}

export function useCancelJob() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (jobId: number) =>
      apiFetch<GenerationJobDetail>(`/jobs/${jobId}/cancel/`, { method: "POST" }),
    onSuccess: (_data, jobId) => {
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
      void queryClient.invalidateQueries({ queryKey: ["job", jobId] });
    },
  });
}

// "Re-queue N copies of this job" -- JobModal's More-menu item. The backend
// (generation/api.py::requeue_job) copies prompt/settings/reference files
// into N fresh rows and enqueues them in the shared FIFO; the response is
// the list of those new jobs, but the UI only needs the side effects, so
// this hook just invalidates the list (the new copies show up there) and
// the queue estimate (N more renders queued).
export function useRequeueJob() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ jobId, count }: { jobId: number; count: number }) =>
      apiFetch<GenerationJobDetail[]>(`/jobs/${jobId}/requeue/`, {
        method: "POST",
        body: JSON.stringify({ count }),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
      void queryClient.invalidateQueries({ queryKey: ["queue-estimate"] });
    },
  });
}

// Covers every PATCH-able field on a job (title, is_favorite, is_archived
// -- see backend generation/api.py's job_detail() PATCH) in one hook,
// rather than one hook per field: all three are simple independent flips
// on the same row, so a single mutation with an all-optional payload
// keeps JobModal/QueueSidebar from juggling three near-identical hooks.
export function useUpdateJob() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      jobId,
      title,
      isFavorite,
      isArchived,
      folderIds,
    }: {
      jobId: number;
      title?: string;
      isFavorite?: boolean;
      isArchived?: boolean;
      // Full replacement of this job's folder membership -- see JobFolder.
      folderIds?: number[];
    }) => {
      const body: Record<string, unknown> = {};
      if (title !== undefined) body.title = title;
      if (isFavorite !== undefined) body.is_favorite = isFavorite;
      if (isArchived !== undefined) body.is_archived = isArchived;
      if (folderIds !== undefined) body.folder_ids = folderIds;
      return apiFetch<GenerationJobDetail>(`/jobs/${jobId}/`, {
        method: "PATCH",
        body: JSON.stringify(body),
      });
    },
    onSuccess: (_data, { jobId }) => {
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
      void queryClient.invalidateQueries({ queryKey: ["job", jobId] });
    },
  });
}

// Job folders -- user-organizational tags, see api/types.ts's JobFolder.
export function useFolders() {
  return useQuery({
    queryKey: ["folders"],
    queryFn: () => apiFetch<JobFolder[]>("/folders/"),
  });
}

export function useCreateFolder() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (name: string) =>
      apiFetch<JobFolder>("/folders/", { method: "POST", body: JSON.stringify({ name }) }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["folders"] });
    },
  });
}

export function useRenameFolder() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ folderId, name }: { folderId: number; name: string }) =>
      apiFetch<JobFolder>(`/folders/${folderId}/`, { method: "PATCH", body: JSON.stringify({ name }) }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["folders"] });
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
      // Partial match -- also refreshes any already-open job detail cache
      // (["job", id]) whose folder chips would otherwise show the old name.
      void queryClient.invalidateQueries({ queryKey: ["job"] });
    },
  });
}

export function useDeleteFolder() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (folderId: number) => apiFetch<void>(`/folders/${folderId}/`, { method: "DELETE" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["folders"] });
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
      void queryClient.invalidateQueries({ queryKey: ["job"] });
    },
  });
}

export function useRefinePrompt() {
  return useMutation({
    mutationFn: (input: {
      mode: Mode;
      rawPrompt: string;
      referenceLabels?: string[];
      /** The currently-selected clip length, if any -- so the LLM keeps
       * shot-cut timestamps within the actual video duration. */
      durationSeconds?: number;
      /** Currently-staged reference images (e.g. i2v's first/last frame).
       * Only actually used by the LLM when the backend has
       * LLM_VISION_ENABLED -- harmless (ignored) otherwise, but only worth
       * the upload when config.data.llm_vision_enabled is true. */
      referenceImages?: File[];
      /** Optional shared context layered on top of the mode's own house
       * guide -- Director Mode passes the project's overarching_prompt
       * here so refine stays consistent with the rest of the project. */
      extraContext?: string;
      /** Director Mode only: true for a clip flagged continues_previous --
       * tells the LLM to write it as a seamless continuation instead of a
       * fresh shot. */
      isContinuation?: boolean;
    }) => {
      const form = new FormData();
      form.set("mode", input.mode);
      form.set("raw_prompt", input.rawPrompt);
      if (input.durationSeconds != null) form.set("duration_seconds", String(input.durationSeconds));
      for (const label of input.referenceLabels ?? []) form.append("reference_labels", label);
      for (const file of input.referenceImages ?? []) form.append("reference_images", file);
      if (input.extraContext) form.set("extra_context", input.extraContext);
      if (input.isContinuation) form.set("is_continuation", "true");
      return apiFetch<{ improved_prompt: string }>("/prompt/refine/", { method: "POST", body: form });
    },
  });
}

export interface ChatReplyInput {
  mode: Mode;
  /** Prior turns, oldest first -- NOT persisted server-side, resent every
   * call (see generation/api.py's chat_message()). */
  history: ChatMessage[];
  content: string;
  /** The user's current draft in the main prompt box, if any -- given to
   * the LLM as context even on the first turn. */
  rawPrompt?: string;
  /** The currently-active AI-refined prompt, if any -- given to the LLM as
   * separate, clearly-labeled context distinct from rawPrompt. */
  improvedPrompt?: string;
  /** The currently-selected clip length, if any -- so the LLM keeps
   * shot-cut timestamps within the actual video duration. */
  durationSeconds?: number;
  referenceLabels?: string[];
  /** Currently-staged reference images. Only actually used by the LLM when
   * the backend has LLM_VISION_ENABLED -- harmless (ignored) otherwise, but
   * only worth the upload when config.data.llm_vision_enabled is true. */
  referenceImages?: File[];
  /** Optional shared context layered on top of the mode's own house
   * guide -- Director Mode passes the project's overarching_prompt here
   * so chat stays consistent with the rest of the project. */
  extraContext?: string;
  /** Director Mode only: true for a clip flagged continues_previous --
   * tells the LLM to write it as a seamless continuation instead of a
   * fresh shot. */
  isContinuation?: boolean;
}

export function useChatReply() {
  return useMutation({
    mutationFn: (input: ChatReplyInput) => {
      const form = new FormData();
      form.set("mode", input.mode);
      form.set("history", JSON.stringify(input.history));
      form.set("content", input.content);
      if (input.rawPrompt) form.set("raw_prompt", input.rawPrompt);
      if (input.improvedPrompt) form.set("improved_prompt", input.improvedPrompt);
      if (input.durationSeconds != null) form.set("duration_seconds", String(input.durationSeconds));
      for (const label of input.referenceLabels ?? []) form.append("reference_labels", label);
      for (const file of input.referenceImages ?? []) form.append("reference_images", file);
      if (input.extraContext) form.set("extra_context", input.extraContext);
      if (input.isContinuation) form.set("is_continuation", "true");
      return apiFetch<ChatMessage>("/prompt/chat/", { method: "POST", body: form });
    },
  });
}

export function useInvites() {
  return useQuery({ queryKey: ["invites"], queryFn: () => apiFetch<Invite[]>("/invites/") });
}

export function useCreateInvite() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { email?: string; note?: string; expiresInDays?: number }) =>
      apiFetch<Invite>("/invites/", {
        method: "POST",
        body: JSON.stringify({
          email: input.email,
          note: input.note,
          expires_in_days: input.expiresInDays,
        }),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["invites"] });
    },
  });
}

export function useDeleteInvite() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (inviteId: number) => apiFetch<void>(`/invites/${inviteId}/`, { method: "DELETE" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["invites"] });
    },
  });
}

export function useQualityCatalog() {
  return useQuery({
    queryKey: ["quality-catalog"],
    queryFn: () => apiFetch<QualityCatalog>("/quality-catalog/"),
  });
}

export interface CreateQualityLevelInput {
  label: string;
  isDraft: boolean;
  modes: Partial<Record<Mode, { megapixels: number; steps: number }>>;
  copyDurationsFrom?: string;
}

export function useCreateQualityLevel() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: CreateQualityLevelInput) =>
      apiFetch<CatalogLevel>("/quality-levels/", {
        method: "POST",
        body: JSON.stringify({
          label: input.label,
          is_draft: input.isDraft,
          modes: input.modes,
          copy_durations_from: input.copyDurationsFrom,
        }),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["quality-catalog"] });
    },
  });
}

export interface UpdateQualityLevelInput {
  label: string;
  newLabel?: string;
  isDraft?: boolean;
  sortOrder?: number;
  modes?: Partial<Record<Mode, { megapixels?: number; steps?: number; is_active?: boolean }>>;
}

export function useUpdateQualityLevel() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: UpdateQualityLevelInput) =>
      apiFetch<CatalogLevel>(`/quality-levels/${encodeURIComponent(input.label)}/`, {
        method: "PATCH",
        body: JSON.stringify({
          new_label: input.newLabel,
          is_draft: input.isDraft,
          sort_order: input.sortOrder,
          modes: input.modes,
        }),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["quality-catalog"] });
    },
  });
}

export function useReorderQualityLevels() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (order: string[]) =>
      apiFetch<QualityCatalog>("/quality-levels/reorder/", {
        method: "POST",
        body: JSON.stringify({ order }),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["quality-catalog"] });
    },
  });
}

export interface UpdateDurationTarget {
  label: string;
  mode: Mode;
  isActive: boolean;
  estimatedRenderSeconds?: number;
}

export function useUpdateDuration() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { durationSeconds: number; targets: UpdateDurationTarget[] }) =>
      apiFetch<QualityCatalog>(`/quality-durations/${input.durationSeconds}/`, {
        method: "PATCH",
        body: JSON.stringify({
          targets: input.targets.map((t) => ({
            label: t.label,
            mode: t.mode,
            is_active: t.isActive,
            estimated_render_seconds: t.estimatedRenderSeconds,
          })),
        }),
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["quality-catalog"] });
    },
  });
}

export function useEstimateDurations() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { mode: Mode; apply?: boolean }) =>
      apiFetch<DurationEstimateResponse>("/quality-durations/estimate/", {
        method: "POST",
        body: JSON.stringify({ mode: input.mode, apply: input.apply }),
      }),
    onSuccess: (_data, variables) => {
      // A preview call (apply omitted/false) writes nothing -- no need to refetch.
      if (variables.apply) void queryClient.invalidateQueries({ queryKey: ["quality-catalog"] });
    },
  });
}
