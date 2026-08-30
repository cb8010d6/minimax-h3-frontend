import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import {
  useCreateFolder,
  useDeleteFolder,
  useFolders,
  useJobs,
  usePresets,
  useQueueEstimate,
  useRenameFolder,
  useUpdateJob,
} from "../../api/queries";
import { useDirectorProjects, useJobMemberships } from "../../api/directorQueries";
import { MODE_LABELS, type GenerationJob, type JobFolder, type JobStatus } from "../../api/types";
import { displayTitle } from "./jobTitle";
import { promptColor } from "./promptColor";
import { JobProgressBar } from "./JobProgressBar";
import { FolderPicker } from "../shared/FolderPicker";

const NOTIFY_STORAGE_KEY = "notifyOnJobDone";
const ACTIVE_STATUSES = new Set<JobStatus>(["queued", "processing"]);

// "all" (no filter) / "none" (standalone jobs -- no Director project) / a
// stringified project id. Kept as a plain string (not a union) since it
// round-trips straight through a <select>'s value without parsing.
const PROJECT_FILTER_ALL = "all";
const PROJECT_FILTER_NONE = "none";
const QUALITY_FILTER_ALL = "all";
// Same shape as PROJECT_FILTER_*, for the folder filter -- see FOLDER_FILTER_NONE
// below. "all" is also what triggers the grouped/collapsible-sections view
// (see showGroupedByFolder in QueueSidebar) rather than a flat list.
const FOLDER_FILTER_ALL = "all";
const FOLDER_FILTER_NONE = "none";
// Sentinel key for the "No folder" collapsible section's own collapsed-state
// entry -- distinct from any real JobFolder id (which are always >= 1).
const NO_FOLDER_KEY = -1;

const STATUS_LABELS: Record<JobStatus, string> = {
  queued: "Queued",
  processing: "Processing…",
  done: "Done",
  cancelled: "Cancelled",
};

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  return rest ? `${minutes}m ${rest}s` : `${minutes}m`;
}

// "done" covers both success and failure (see api/types.ts) -- video_url's
// presence is what actually distinguishes them for display purposes.
function didJobFail(job: GenerationJob): boolean {
  return job.status === "done" && !job.video_url;
}

function titleFor(job: GenerationJob): string {
  return displayTitle(job, 40);
}

function relativeTime(iso: string): string {
  const diffMin = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (diffMin < 1) return "just now";
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.round(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  return new Date(iso).toLocaleDateString();
}

function QueueThumb({ job }: { job: GenerationJob }) {
  if (job.status === "done" && job.video_url) {
    if (job.content_type === "video") {
      // Prefer the pre-generated poster image (a static <img>, cheap to
      // render N-per-list) -- fall back to the old <video> thumb only for
      // jobs rendered before thumbnail_url existed (or the rare
      // thumbnail-generation failure), never worse than before.
      if (job.thumbnail_url) return <img src={job.thumbnail_url} alt="" />;
      return <video src={job.video_url} muted preload="metadata" />;
    }
    if (job.content_type === "image") return <img src={job.video_url} alt="" />;
    // Audio has no useful tiny-thumbnail rendering (an <audio> element is a
    // full player, not an image) -- falls through to the placeholder below.
  }
  return (
    <span className="queue-entry-placeholder" aria-hidden="true">
      {job.status === "done" && job.video_url && job.content_type === "audio" ? "♪" : MODE_LABELS[job.mode][0]}
    </span>
  );
}

function QueueEntry({ job, onOpen, folders }: { job: GenerationJob; onOpen: () => void; folders: JobFolder[] }) {
  const failed = didJobFail(job);
  const updateJob = useUpdateJob();
  const createFolder = useCreateFolder();
  const [folderPickerOpen, setFolderPickerOpen] = useState(false);
  const folderPickerRef = useRef<HTMLDivElement>(null);

  // Same outside-click/Escape pattern as JobModal's "⋯ More" submenu (see
  // that file) -- a plain <div> popover, not a <dialog>, so neither is
  // automatic.
  useEffect(() => {
    if (!folderPickerOpen) return;
    function onPointerDown(e: PointerEvent) {
      if (folderPickerRef.current && !folderPickerRef.current.contains(e.target as Node)) {
        setFolderPickerOpen(false);
      }
    }
    function onKeyDown(e: globalThis.KeyboardEvent) {
      if (e.key === "Escape") setFolderPickerOpen(false);
    }
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [folderPickerOpen]);

  function toggleFolder(folderId: number) {
    const current = job.folders.map((f) => f.id);
    const next = current.includes(folderId) ? current.filter((id) => id !== folderId) : [...current, folderId];
    updateJob.mutate({ jobId: job.id, folderIds: next });
  }

  async function createAndAttachFolder(name: string) {
    const folder = await createFolder.mutateAsync(name);
    updateJob.mutate({ jobId: job.id, folderIds: [...job.folders.map((f) => f.id), folder.id] });
  }

  return (
    // queue-entry-popover-open lifts this row's stacking order above its
    // siblings (see App.css) while its folder popover is open -- otherwise
    // :hover's transform (see .queue-entry:hover) creates a new stacking
    // context on this row that traps the popover's own z-index locally,
    // and the next sibling row (later in DOM order, same default stacking
    // level) paints on top of it instead.
    <li className={`queue-entry${folderPickerOpen ? " queue-entry-popover-open" : ""}`}>
      <button type="button" className="queue-entry-button" onClick={onOpen}>
        <span className="queue-entry-thumb">
          <QueueThumb job={job} />
        </span>
        <span className="queue-entry-body">
          <span className="queue-entry-title">{titleFor(job)}</span>
          <span className="queue-entry-meta">
            <span className="queue-entry-status-time">
              <span
                className={`job-status job-status-${job.status} ${failed ? "job-status-failed" : ""}`}
              >
                {failed ? "Failed" : STATUS_LABELS[job.status]}
              </span>
              <span className="queue-entry-time">{relativeTime(job.created_at)}</span>
              <span className="queue-entry-id">#{job.id}</span>
              {/* Compact folder indicator -- full names on hover (see
                  `title`) rather than a dedicated row of name chips, which
                  used to add a whole extra line per foldered job. */}
              {job.folders.length > 0 && (
                <span
                  className="queue-entry-folder-badge"
                  title={job.folders.map((f) => f.name).join(", ")}
                >
                  <span aria-hidden="true">📁</span>
                  {job.folders.length}
                </span>
              )}
            </span>
            {/* Turbo marker on the quality label (owner request: turbo jobs
                should stand out in the list without opening them) -- the
                accent color below carries the same signal for non-color
                users. Truncates with an ellipsis (see App.css) instead of
                wrapping to its own line -- a long preset label used to push
                the whole row a line taller. */}
            <span
              className={`queue-entry-quality${job.use_turbo ? " queue-entry-quality-turbo" : ""}`}
              title={`${job.use_turbo ? "Turbo — " : ""}${job.preset_label}`}
            >
              {job.use_turbo && (
                <span aria-hidden="true" title="Rendered with Turbo mode">
                  🚀
                </span>
              )}{" "}
              {job.preset_label}
            </span>
          </span>
          {job.status === "processing" && <JobProgressBar job={job} />}
        </span>
      </button>
      {/* Sibling of queue-entry-button (not nested inside it -- a <button>
          can't legally contain another <button>): a narrow vertical strip
          of stacked action buttons, replacing the previous circular icons
          that floated over the thumbnail's corner. Its left edge doubles as
          the prompt-color marker (see promptColor.ts) that used to be its
          own thin bar on the far right -- merging the two so the color
          coding and the actions read as one element instead of two. */}
      <div className="queue-entry-action-bar" style={{ borderLeftColor: promptColor(job.prompt_hash) }}>
        <button
          type="button"
          className="queue-entry-action-button queue-entry-action-favorite"
          onClick={() => updateJob.mutate({ jobId: job.id, isFavorite: !job.is_favorite })}
          aria-pressed={job.is_favorite}
          title={job.is_favorite ? "Remove from favorites" : "Add to favorites"}
        >
          <span aria-hidden="true">{job.is_favorite ? "♥" : "♡"}</span>
        </button>
        <button
          type="button"
          className="queue-entry-action-button queue-entry-action-archive"
          onClick={() => updateJob.mutate({ jobId: job.id, isArchived: !job.is_archived })}
          aria-pressed={job.is_archived}
          title={job.is_archived ? "Unarchive" : "Archive"}
        >
          <span aria-hidden="true">🗄</span>
        </button>
        <div className="queue-entry-folder-anchor" ref={folderPickerRef}>
          <button
            type="button"
            className={`queue-entry-action-button queue-entry-action-folder${
              job.folders.length > 0 ? " queue-entry-action-folder-active" : ""
            }`}
            onClick={() => setFolderPickerOpen((v) => !v)}
            aria-expanded={folderPickerOpen}
            aria-haspopup="true"
            title="Add to folder"
          >
            <span aria-hidden="true">📁</span>
          </button>
          {folderPickerOpen && (
            <div className="folder-picker-popover">
              <FolderPicker
                folders={folders}
                selectedIds={job.folders.map((f) => f.id)}
                onToggle={toggleFolder}
                onCreate={(name) => void createAndAttachFolder(name)}
                creating={createFolder.isPending}
              />
            </div>
          )}
        </div>
      </div>
    </li>
  );
}

interface QueueSidebarProps {
  onOpenJob: (jobId: number) => void;
}

// Persisted in localStorage (not just React state) so the preference
// survives a reload -- there's no server-side user-settings model for it.
function useNotifyOnDone(): [boolean, (next: boolean) => void] {
  const [enabled, setEnabled] = useState(
    () => typeof Notification !== "undefined" && localStorage.getItem(NOTIFY_STORAGE_KEY) === "true",
  );

  function set(next: boolean) {
    if (next && typeof Notification !== "undefined" && Notification.permission === "default") {
      void Notification.requestPermission().then((permission) => {
        const granted = permission === "granted";
        localStorage.setItem(NOTIFY_STORAGE_KEY, String(granted));
        setEnabled(granted);
      });
      return;
    }
    localStorage.setItem(NOTIFY_STORAGE_KEY, String(next));
    setEnabled(next);
  }

  return [enabled, set];
}

export function QueueSidebar({ onOpenJob }: QueueSidebarProps) {
  const jobs = useJobs();
  const queueEstimate = useQueueEstimate(null);
  const jobMemberships = useJobMemberships();
  const directorProjects = useDirectorProjects();
  const folders = useFolders();
  const createFolder = useCreateFolder();
  const renameFolder = useRenameFolder();
  const deleteFolder = useDeleteFolder();
  // Canonical quality-tier ordering for the filter dropdown -- t2v's own
  // catalog order (RenderPreset.sort_order, see list_presets()), not
  // "whichever label a job happened to use most recently" (job.created_at
  // desc is GenerationJob's default ordering, so that's what a naive
  // first-seen-in-job-list order would actually produce -- a different,
  // effectively random-looking order every time the job history changes).
  // Admin keeps a label's sort_order in sync across every mode's row (see
  // RenderPreset's own docstring), so t2v's order should match every other
  // mode's for the same label.
  const qualityCatalogOrder = usePresets("t2v");
  const [notifyOnDone, setNotifyOnDone] = useNotifyOnDone();

  const [filtersOpen, setFiltersOpen] = useState(false);
  const [qualityFilter, setQualityFilter] = useState(QUALITY_FILTER_ALL);
  const [projectFilter, setProjectFilter] = useState(PROJECT_FILTER_ALL);
  const [folderFilter, setFolderFilter] = useState(FOLDER_FILTER_ALL);
  const [favoritesOnly, setFavoritesOnly] = useState(false);
  const [showArchived, setShowArchived] = useState(false);

  // Which folder-group sections are collapsed -- NO_FOLDER_KEY for the "No
  // folder" bucket, a real JobFolder id otherwise. Not persisted; resets on
  // reload, same as filtersOpen.
  const [collapsedFolders, setCollapsedFolders] = useState<Set<number>>(new Set());
  const [newFolderName, setNewFolderName] = useState("");
  const [renamingFolderId, setRenamingFolderId] = useState<number | null>(null);
  const [renameDraft, setRenameDraft] = useState("");
  const [deletingFolderId, setDeletingFolderId] = useState<number | null>(null);

  function resetFilters() {
    setQualityFilter(QUALITY_FILTER_ALL);
    setProjectFilter(PROJECT_FILTER_ALL);
    setFolderFilter(FOLDER_FILTER_ALL);
    setFavoritesOnly(false);
    setShowArchived(false);
  }

  function toggleFolderCollapsed(key: number) {
    setCollapsedFolders((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function startRenameFolder(folder: JobFolder) {
    setRenamingFolderId(folder.id);
    setRenameDraft(folder.name);
  }

  function commitRenameFolder(folder: JobFolder) {
    setRenamingFolderId(null);
    const name = renameDraft.trim();
    if (!name || name === folder.name) return;
    renameFolder.mutate({ folderId: folder.id, name });
  }

  async function handleDeleteFolder(folderId: number) {
    setDeletingFolderId(null);
    await deleteFolder.mutateAsync(folderId);
    if (folderFilter === String(folderId)) setFolderFilter(FOLDER_FILTER_ALL);
  }

  function handleCreateFolder(e: FormEvent) {
    e.preventDefault();
    const name = newFolderName.trim();
    if (!name) return;
    createFolder.mutate(name);
    setNewFolderName("");
  }

  // Tracks which job ids were active (queued/processing) as of the last
  // render, so a job disappearing from that set can be detected as a
  // queued->done transition -- comparing snapshots rather than trusting a
  // single job's status change event, since polling (useJobs) only hands us
  // periodic full-list snapshots, not transitions themselves.
  const previouslyActiveRef = useRef<Set<number>>(new Set());
  useEffect(() => {
    const current = jobs.data ?? [];
    const previouslyActive = previouslyActiveRef.current;
    if (notifyOnDone && typeof Notification !== "undefined" && Notification.permission === "granted") {
      for (const job of current) {
        // Cancellation is always something the user themselves just did in
        // this same browser (see JobModal's Cancel button) -- a
        // notification for it would just be a redundant echo, unlike a
        // done/failed transition which can happen while they're away.
        if (previouslyActive.has(job.id) && !ACTIVE_STATUSES.has(job.status) && job.status !== "cancelled") {
          const failed = didJobFail(job);
          new Notification(failed ? "Generation failed" : "Generation done", {
            body: titleFor(job),
            tag: `job-${job.id}`,
          });
        }
      }
    }
    previouslyActiveRef.current = new Set(
      current.filter((job) => ACTIVE_STATUSES.has(job.status)).map((job) => job.id),
    );
  }, [jobs.data, notifyOnDone]);

  // Distinct quality labels actually present in the job list (not the
  // full admin catalog, which may offer tiers no job here was ever
  // rendered at), ordered to match the catalog's own sort_order -- see
  // qualityCatalogOrder above. A label the job list has that t2v's own
  // catalog doesn't (e.g. an image/audio-only tier, or one since retired)
  // sorts after every catalog-ordered one, in first-seen order.
  const qualityOptions = useMemo(() => {
    const present: string[] = [];
    for (const job of jobs.data ?? []) {
      if (job.preset_label && !present.includes(job.preset_label)) present.push(job.preset_label);
    }
    const catalogIndex = new Map((qualityCatalogOrder.data ?? []).map((preset, index) => [preset.label, index]));
    return [...present].sort((a, b) => {
      const aIndex = catalogIndex.get(a) ?? Number.MAX_SAFE_INTEGER;
      const bIndex = catalogIndex.get(b) ?? Number.MAX_SAFE_INTEGER;
      return aIndex - bIndex;
    });
  }, [jobs.data, qualityCatalogOrder.data]);

  const projectByJobId = useMemo(() => {
    const map = new Map<number, { project_id: number; project_title: string }>();
    for (const row of jobMemberships.data ?? []) {
      map.set(row.job_id, { project_id: row.project_id, project_title: row.project_title });
    }
    return map;
  }, [jobMemberships.data]);

  const filteredJobs = useMemo(() => {
    return (jobs.data ?? []).filter((job) => {
      if (!showArchived && job.is_archived) return false;
      if (favoritesOnly && !job.is_favorite) return false;
      if (qualityFilter !== QUALITY_FILTER_ALL && job.preset_label !== qualityFilter) return false;
      if (projectFilter === PROJECT_FILTER_NONE && projectByJobId.has(job.id)) return false;
      if (
        projectFilter !== PROJECT_FILTER_ALL &&
        projectFilter !== PROJECT_FILTER_NONE &&
        projectByJobId.get(job.id)?.project_id !== Number(projectFilter)
      ) {
        return false;
      }
      if (folderFilter === FOLDER_FILTER_NONE && job.folders.length > 0) return false;
      if (
        folderFilter !== FOLDER_FILTER_ALL &&
        folderFilter !== FOLDER_FILTER_NONE &&
        !job.folders.some((f) => f.id === Number(folderFilter))
      ) {
        return false;
      }
      return true;
    });
  }, [jobs.data, showArchived, favoritesOnly, qualityFilter, projectFilter, folderFilter, projectByJobId]);

  const filtersActive =
    qualityFilter !== QUALITY_FILTER_ALL ||
    projectFilter !== PROJECT_FILTER_ALL ||
    folderFilter !== FOLDER_FILTER_ALL ||
    favoritesOnly ||
    showArchived;

  // Grouped, collapsible-by-folder view of the list (owner request: dozens
  // of slight prompt variants around the same concept are hard to track in
  // a flat list). Only kicks in with no specific folder narrowed down (a
  // job can be in several folders, so "All" is the only state where showing
  // it once per folder it belongs to -- Gmail-label style -- makes sense)
  // and only once the user actually has folders; otherwise this falls back
  // to the plain flat list exactly as before folders existed.
  const showGroupedByFolder = folderFilter === FOLDER_FILTER_ALL && (folders.data?.length ?? 0) > 0;
  const folderGroups = useMemo(() => {
    if (!showGroupedByFolder) return null;
    const byFolder = new Map<number, GenerationJob[]>();
    for (const folder of folders.data ?? []) byFolder.set(folder.id, []);
    const noFolder: GenerationJob[] = [];
    for (const job of filteredJobs) {
      if (job.folders.length === 0) {
        noFolder.push(job);
        continue;
      }
      for (const f of job.folders) byFolder.get(f.id)?.push(job);
    }
    return { byFolder, noFolder };
  }, [showGroupedByFolder, filteredJobs, folders.data]);

  return (
    <aside className="queue-sidebar">
      <h2>Queue</h2>
      <label className="queue-notify-toggle hint">
        <input
          type="checkbox"
          checked={notifyOnDone}
          onChange={(e) => setNotifyOnDone(e.target.checked)}
        />
        Notify me when a job is done
      </label>
      {queueEstimate.data && (
        <p className="hint queue-backlog">
          Backlog:{" "}
          {queueEstimate.data.seconds_ahead > 0
            ? `~${formatDuration(queueEstimate.data.seconds_ahead)}`
            : "none"}
        </p>
      )}

      <div className="queue-filters-header">
        <button
          type="button"
          className="queue-filters-toggle"
          onClick={() => setFiltersOpen((v) => !v)}
          aria-expanded={filtersOpen}
        >
          <span aria-hidden="true">{filtersOpen ? "▾" : "▸"}</span> Filters
          {filtersActive && <span className="queue-filters-badge" title="Filters active" />}
        </button>
        {filtersActive && (
          <button type="button" className="link-button" onClick={resetFilters}>
            Reset
          </button>
        )}
      </div>

      {filtersOpen && (
        <div className="queue-filters-panel">
          <div className="queue-filters">
            <label className="queue-filter-control">
              <span className="hint">Quality</span>
              <select value={qualityFilter} onChange={(e) => setQualityFilter(e.target.value)}>
                <option value={QUALITY_FILTER_ALL}>All</option>
                {qualityOptions.map((label) => (
                  <option key={label} value={label}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label className="queue-filter-control">
              <span className="hint">Director project</span>
              <select value={projectFilter} onChange={(e) => setProjectFilter(e.target.value)}>
                <option value={PROJECT_FILTER_ALL}>All</option>
                <option value={PROJECT_FILTER_NONE}>No project</option>
                {directorProjects.data?.map((project) => (
                  <option key={project.id} value={project.id}>
                    {project.title || `Untitled project #${project.id}`}
                  </option>
                ))}
              </select>
            </label>
            <label className="queue-filter-control">
              <span className="hint">Folder</span>
              <select value={folderFilter} onChange={(e) => setFolderFilter(e.target.value)}>
                <option value={FOLDER_FILTER_ALL}>All</option>
                <option value={FOLDER_FILTER_NONE}>No folder</option>
                {folders.data?.map((folder) => (
                  <option key={folder.id} value={folder.id}>
                    {folder.name} ({folder.job_count})
                  </option>
                ))}
              </select>
            </label>
            <label className="queue-filter-checkbox hint">
              <input type="checkbox" checked={favoritesOnly} onChange={(e) => setFavoritesOnly(e.target.checked)} />
              Favorites only
            </label>
            <label className="queue-filter-checkbox hint">
              <input type="checkbox" checked={showArchived} onChange={(e) => setShowArchived(e.target.checked)} />
              Show archived
            </label>
          </div>

          <div className="folder-manage">
            <span className="hint folder-manage-heading">Folders</span>
            {folders.data && folders.data.length > 0 && (
              <ul className="folder-manage-list">
                {folders.data.map((folder) => (
                  <li key={folder.id} className="folder-manage-row">
                    {renamingFolderId === folder.id ? (
                      <input
                        type="text"
                        className="folder-manage-rename-input"
                        autoFocus
                        value={renameDraft}
                        maxLength={100}
                        onChange={(e) => setRenameDraft(e.target.value)}
                        onBlur={() => commitRenameFolder(folder)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") e.currentTarget.blur();
                          if (e.key === "Escape") setRenamingFolderId(null);
                        }}
                      />
                    ) : (
                      <span className="folder-manage-name">
                        {folder.name} <span className="folder-manage-count">({folder.job_count})</span>
                      </span>
                    )}
                    <span className="folder-manage-actions">
                      {deletingFolderId === folder.id ? (
                        <>
                          <button
                            type="button"
                            className="link-button"
                            onClick={() => void handleDeleteFolder(folder.id)}
                          >
                            Confirm delete
                          </button>
                          <button type="button" className="link-button" onClick={() => setDeletingFolderId(null)}>
                            Cancel
                          </button>
                        </>
                      ) : (
                        <>
                          <button
                            type="button"
                            className="link-button"
                            onClick={() => startRenameFolder(folder)}
                            title="Rename folder"
                          >
                            ✎
                          </button>
                          <button
                            type="button"
                            className="link-button"
                            onClick={() => setDeletingFolderId(folder.id)}
                            title="Delete folder"
                          >
                            🗑
                          </button>
                        </>
                      )}
                    </span>
                  </li>
                ))}
              </ul>
            )}
            <form className="folder-new-form" onSubmit={handleCreateFolder}>
              <input
                type="text"
                placeholder="+ New folder…"
                value={newFolderName}
                maxLength={100}
                onChange={(e) => setNewFolderName(e.target.value)}
              />
              <button type="submit" disabled={!newFolderName.trim() || createFolder.isPending}>
                Add
              </button>
            </form>
          </div>
        </div>
      )}

      {jobs.isLoading && <p className="hint">Loading…</p>}
      {jobs.isError && <p className="error">Couldn't load your jobs.</p>}
      {jobs.data?.length === 0 && <p className="empty-state">No jobs yet — queue one to see it here.</p>}
      {!!jobs.data?.length && filteredJobs.length === 0 && (
        <p className="empty-state">
          {filtersActive ? "No jobs match these filters." : "No jobs yet — queue one to see it here."}
        </p>
      )}
      {filtersActive && filteredJobs.length > 0 && (
        <p className="hint queue-filter-count">
          {filteredJobs.length} of {jobs.data?.length}
        </p>
      )}

      {showGroupedByFolder && folderGroups ? (
        <>
          {(folders.data ?? []).map((folder) => {
            const jobsInFolder = folderGroups.byFolder.get(folder.id) ?? [];
            if (jobsInFolder.length === 0) return null;
            const collapsed = collapsedFolders.has(folder.id);
            return (
              <div className="queue-folder-group" key={folder.id}>
                <button
                  type="button"
                  className="queue-folder-group-header"
                  onClick={() => toggleFolderCollapsed(folder.id)}
                  aria-expanded={!collapsed}
                >
                  <span aria-hidden="true">{collapsed ? "▸" : "▾"}</span> {folder.name}
                  <span className="queue-folder-group-count">{jobsInFolder.length}</span>
                </button>
                {!collapsed && (
                  <ul className="queue-list">
                    {jobsInFolder.map((job) => (
                      <QueueEntry key={job.id} job={job} onOpen={() => onOpenJob(job.id)} folders={folders.data ?? []} />
                    ))}
                  </ul>
                )}
              </div>
            );
          })}
          {folderGroups.noFolder.length > 0 &&
            (() => {
              const collapsed = collapsedFolders.has(NO_FOLDER_KEY);
              return (
                <div className="queue-folder-group">
                  <button
                    type="button"
                    className="queue-folder-group-header"
                    onClick={() => toggleFolderCollapsed(NO_FOLDER_KEY)}
                    aria-expanded={!collapsed}
                  >
                    <span aria-hidden="true">{collapsed ? "▸" : "▾"}</span> No folder
                    <span className="queue-folder-group-count">{folderGroups.noFolder.length}</span>
                  </button>
                  {!collapsed && (
                    <ul className="queue-list">
                      {folderGroups.noFolder.map((job) => (
                        <QueueEntry
                          key={job.id}
                          job={job}
                          onOpen={() => onOpenJob(job.id)}
                          folders={folders.data ?? []}
                        />
                      ))}
                    </ul>
                  )}
                </div>
              );
            })()}
        </>
      ) : (
        <ul className="queue-list">
          {filteredJobs.map((job) => (
            <QueueEntry key={job.id} job={job} onOpen={() => onOpenJob(job.id)} folders={folders.data ?? []} />
          ))}
        </ul>
      )}
    </aside>
  );
}
