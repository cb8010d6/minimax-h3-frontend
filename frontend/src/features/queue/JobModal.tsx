import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { useNavigate } from "react-router-dom";
import { API_BASE_URL } from "../../api/client";
import { useCancelJob, useDeleteJob, useJob, useRequeueJob, useUpdateJob } from "../../api/queries";
import { useCreateProjectFromJob, useJobMemberships } from "../../api/directorQueries";
import { MODE_LABELS, type GenerationJobDetail } from "../../api/types";
import { displayTitle } from "./jobTitle";
import { JobProgressBar } from "./JobProgressBar";

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  return rest ? `${minutes}m ${rest}s` : `${minutes}m`;
}

// Actual measured render time once done; falls back to the estimate while
// still queued/processing (there's no finished_at yet to measure from).
function renderTimeLabel(job: GenerationJobDetail): string {
  if (job.started_at && job.finished_at) {
    const seconds = (new Date(job.finished_at).getTime() - new Date(job.started_at).getTime()) / 1000;
    return `${formatDuration(seconds)} (actual)`;
  }
  return `~${formatDuration(job.estimated_seconds)} (estimated)`;
}

// Resolution is fixed/meaningless for audio (visual output is discarded --
// see backend's Mode docstring) and duration is pinned near-zero for image
// (only frame 0 is kept), so each content type only shows the axis that
// actually means something for it.
function resolutionLabel(job: GenerationJobDetail): string {
  const resolution = `${job.width}×${job.height} (${job.aspect_ratio}, ${job.megapixels}MP)`;
  if (job.content_type === "video") return `${resolution} — ${job.duration_seconds}s`;
  if (job.content_type === "image") return resolution;
  return `${job.duration_seconds}s`;
}

interface JobModalProps {
  jobId: number;
  onClose: () => void;
  onRedo: (job: GenerationJobDetail) => void;
}

export function JobModal({ jobId, onClose, onRedo }: JobModalProps) {
  const navigate = useNavigate();
  const job = useJob(jobId);
  const deleteJob = useDeleteJob();
  const cancelJob = useCancelJob();
  const updateJob = useUpdateJob();
  const jobMemberships = useJobMemberships();
  const createProjectFromJob = useCreateProjectFromJob();
  const requeueJob = useRequeueJob();
  const [showAiPrompt, setShowAiPrompt] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  const [steamDeckExporting, setSteamDeckExporting] = useState(false);
  const [steamDeckError, setSteamDeckError] = useState(false);
  // "⋯ More" submenu (Re-queue / Steam Deck export / Create Director
  // project) -- see its usage in modal-actions below.
  const [moreOpen, setMoreOpen] = useState(false);
  const moreMenuRef = useRef<HTMLDivElement>(null);
  // "Re-queue" dialog (opened from the More menu): how many identical
  // copies of this job to queue, as a string so the <input type="number">
  // can hold whatever the user typed (including empty) while editing.
  const [requeueOpen, setRequeueOpen] = useState(false);
  const [requeueCount, setRequeueCount] = useState("1");
  const [requeueError, setRequeueError] = useState(false);

  // Reset per-job UI state in case this modal instance is reused for a
  // different job rather than remounted (e.g. clicking straight from one
  // job to another without closing in between).
  useEffect(() => {
    setConfirmingDelete(false);
    setEditingTitle(false);
    setSteamDeckExporting(false);
    setSteamDeckError(false);
    setMoreOpen(false);
    setRequeueOpen(false);
    setRequeueCount("1");
    setRequeueError(false);
  }, [jobId]);

  // The submenu is a plain <div>, not a <dialog>, so outside-click and
  // Escape closing aren't automatic -- wire both here. The listener checks
  // the whole .modal-more container (trigger + menu), so clicking the
  // trigger itself is left to its own onClick toggle.
  useEffect(() => {
    if (!moreOpen) return;
    function onPointerDown(e: PointerEvent) {
      if (moreMenuRef.current && !moreMenuRef.current.contains(e.target as Node)) {
        setMoreOpen(false);
      }
    }
    function onKeyDown(e: globalThis.KeyboardEvent) {
      if (e.key === "Escape") setMoreOpen(false);
    }
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [moreOpen]);

  // The re-queue dialog is a nested .modal-overlay, not a <dialog>, so
  // Escape closing isn't automatic either (outside-click is handled by the
  // overlay's own onClick).
  useEffect(() => {
    if (!requeueOpen) return;
    function onKeyDown(e: globalThis.KeyboardEvent) {
      if (e.key === "Escape") setRequeueOpen(false);
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [requeueOpen]);

  async function handleDelete() {
    await deleteJob.mutateAsync(jobId);
    onClose();
  }

  async function handleCancel() {
    await cancelJob.mutateAsync(jobId);
  }

  async function handleRequeue() {
    const count = Number(requeueCount);
    if (!Number.isInteger(count) || count < 1 || count > 10) return;
    setRequeueError(false);
    try {
      await requeueJob.mutateAsync({ jobId, count });
      setRequeueOpen(false);
    } catch {
      // mutateAsync throws on non-2xx (see client.ts) -- show the inline
      // error and keep the dialog open so the count is preserved.
      setRequeueError(true);
    }
  }

  async function handleCreateProject() {
    const project = await createProjectFromJob.mutateAsync(jobId);
    onClose();
    navigate(`/director/${project.id}`);
  }

  // Not a plain <a href download> -- the conversion runs synchronously on
  // the server (VP9 CPU encoding, can take a while), so a bare link would
  // leave the browser showing nothing at all until it's done. Fetching the
  // blob ourselves lets the button show "Converting…" for that whole time
  // instead, then hands the browser a real file via a temporary object URL.
  async function handleSteamDeckExport() {
    setSteamDeckExporting(true);
    setSteamDeckError(false);
    try {
      const response = await fetch(`${API_BASE_URL}/jobs/${jobId}/steam_deck_export/`, {
        credentials: "include",
      });
      if (!response.ok) throw new Error(`steam_deck_export failed with status ${response.status}`);
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `job_${jobId}_steam_deck.webm`;
      link.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      console.error("Steam Deck export failed", err);
      setSteamDeckError(true);
    } finally {
      setSteamDeckExporting(false);
    }
  }

  const directorMembership = jobMemberships.data?.find((m) => m.job_id === jobId);
  // Mirrors backend director/services.py's create_project_from_job() own
  // eligibility check (video-mode, successfully finished) -- content_type
  // "video" is exactly Director's own t2v/i2v/r2v mode set, see
  // generation/models.py's CONTENT_TYPE_BY_MODE.
  const canCreateDirectorProject =
    job.data?.content_type === "video" && job.data.status === "done" && !!job.data.video_url;
  // Which items the "⋯ More" submenu below offers. Re-queue is ALWAYS
  // offered (any job, in any state, can be re-rendered), so the trigger is
  // always visible; the other two only appear when they qualify.
  const showSteamDeckItem = job.data?.content_type === "video" && !!job.data.video_url;
  const showDirectorItem = canCreateDirectorProject && !directorMembership;
  // 1..10, mirroring the backend's _MAX_REQUEUE_COPIES (see
  // generation/api.py::requeue_job) -- the input's min/max attributes give
  // the same bounds for arrow-key/typing, this is the submit-time check.
  const requeueCountValue = Number(requeueCount);
  const requeueCountValid = Number.isInteger(requeueCountValue) && requeueCountValue >= 1 && requeueCountValue <= 10;

  function startEditingTitle() {
    if (!job.data) return;
    setTitleDraft(displayTitle(job.data));
    setEditingTitle(true);
  }

  async function saveTitle() {
    setEditingTitle(false);
    if (!job.data || titleDraft.trim() === job.data.title.trim()) return;
    await updateJob.mutateAsync({ jobId, title: titleDraft.trim() });
  }

  function toggleFavorite() {
    if (!job.data) return;
    updateJob.mutate({ jobId, isFavorite: !job.data.is_favorite });
  }

  function toggleArchived() {
    if (!job.data) return;
    updateJob.mutate({ jobId, isArchived: !job.data.is_archived });
  }

  function handleTitleKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter") e.currentTarget.blur(); // triggers onBlur -> saveTitle
    if (e.key === "Escape") setEditingTitle(false);
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
          ×
        </button>

        {job.isLoading && <p className="hint">Loading…</p>}
        {job.isError && <p className="error">Couldn't load this job.</p>}

        {job.data && (
          <>
            {editingTitle ? (
              <input
                type="text"
                className="modal-title-input"
                autoFocus
                value={titleDraft}
                maxLength={200}
                onChange={(e) => setTitleDraft(e.target.value)}
                onBlur={saveTitle}
                onKeyDown={handleTitleKeyDown}
              />
            ) : (
              <h2
                className="modal-title-editable"
                onClick={startEditingTitle}
                title="Click to rename"
              >
                {displayTitle(job.data)}
              </h2>
            )}
            <p className="hint modal-mode-label">
              {MODE_LABELS[job.data.mode]}
              <button
                type="button"
                className="link-button job-favorite-toggle"
                onClick={toggleFavorite}
                aria-pressed={job.data.is_favorite}
                title={job.data.is_favorite ? "Remove from favorites" : "Add to favorites"}
              >
                <span aria-hidden="true">{job.data.is_favorite ? "♥" : "♡"}</span>
              </button>
              {job.data.is_archived && <span className="hint"> — Archived</span>}
            </p>

            {job.data.status === "done" && job.data.video_url ? (
              job.data.content_type === "video" ? (
                <video src={job.data.video_url} controls className="modal-video" />
              ) : job.data.content_type === "image" ? (
                <img src={job.data.video_url} alt="" className="modal-image" />
              ) : (
                <audio src={job.data.video_url} controls className="modal-audio" />
              )
            ) : job.data.status === "done" ? (
              <p className="error">Failed: {job.data.error_message || "no output was produced."}</p>
            ) : job.data.status === "cancelled" ? (
              <p className="hint">Cancelled.</p>
            ) : (
              <>
                <p className="hint">
                  {job.data.status === "processing" ? "Processing…" : "Queued…"}
                  {job.data.expected_finish_time &&
                    ` Expected done by ${new Date(job.data.expected_finish_time).toLocaleTimeString()}.`}
                </p>
                {job.data.status === "processing" && <JobProgressBar job={job.data} />}
              </>
            )}

            <dl className="modal-details">
              <dt>Prompt</dt>
              <dd>{job.data.raw_prompt}</dd>
              {job.data.improved_prompt && (
                <>
                  <dt>
                    <button
                      type="button"
                      className="link-button"
                      onClick={() => setShowAiPrompt((v) => !v)}
                      aria-expanded={showAiPrompt}
                    >
                      AI-refined prompt {showAiPrompt ? "▾" : "▸"}
                    </button>
                  </dt>
                  {showAiPrompt && <dd>{job.data.improved_prompt}</dd>}
                </>
              )}
              <dt>
                {job.data.content_type === "video"
                  ? "Resolution & length"
                  : job.data.content_type === "image"
                    ? "Resolution"
                    : "Length"}
              </dt>
              <dd>{resolutionLabel(job.data)}</dd>
              <dt>Render time</dt>
              <dd>{renderTimeLabel(job.data)}</dd>
              {/* Turbo is ALWAYS shown (owner request: the detail page must
                  say whether a job used turbo, including "No") -- unlike
                  Spectrum, which only appears when it was actually used. */}
              <dt>Turbo</dt>
              <dd>{job.data.use_turbo ? "Yes" : "No"}</dd>
              {job.data.use_spectrum && (
                <>
                  <dt>Spectrum</dt>
                  <dd>Yes</dd>
                </>
              )}
              {directorMembership && (
                <>
                  <dt>Director project</dt>
                  <dd>
                    <button
                      type="button"
                      className="link-button"
                      onClick={() => navigate(`/director/${directorMembership.project_id}`)}
                    >
                      {directorMembership.project_title || "Untitled project"}
                    </button>
                  </dd>
                </>
              )}
            </dl>

            <div className="modal-actions">
              {job.data.video_url && (
                <a href={job.data.video_url} download className="button button-primary">
                  <span aria-hidden="true">⬇</span> Download
                </a>
              )}
              {/* Re-queue + Steam Deck export + Create Director project
                  live in this "⋯ More" submenu instead of as top-level
                  buttons (owner request: keep the action row short).
                  Re-queue is always offered, so the trigger is always
                  visible; the other two only appear when they qualify.
                  The menu is a plain <div> (a <button> can't legally
                  contain another <button>), and it opens *upward* because
                  .modal is overflow-y: auto -- a downward menu would run
                  into the modal's scroll edge (see App.css's
                  .modal-more-menu). */}
              <div className="modal-more" ref={moreMenuRef}>
                <button
                  type="button"
                  className="modal-more-trigger"
                  onClick={() => setMoreOpen((v) => !v)}
                  aria-expanded={moreOpen}
                  aria-haspopup="true"
                  title="Extra actions for this job"
                >
                  <span aria-hidden="true">⋯</span> More
                </button>
                {moreOpen && (
                  <div className="modal-more-menu" role="menu">
                    <button
                      type="button"
                      role="menuitem"
                      onClick={() => {
                        setMoreOpen(false);
                        setRequeueOpen(true);
                      }}
                      title="Queue more identical renders of this job (same prompt, settings, and reference files) -- you'll be asked how many, defaulting to 1."
                    >
                      <span aria-hidden="true">🔁</span> Re-queue…
                    </button>
                    {showSteamDeckItem && (
                      <button
                        type="button"
                        role="menuitem"
                        onClick={() => {
                          setMoreOpen(false);
                          void handleSteamDeckExport();
                        }}
                        disabled={steamDeckExporting}
                        title="Converts to the 1280x800 VP9+Opus WebM format Steam Deck's custom startup video needs -- can take a while, VP9 encoding is slow."
                      >
                        <span aria-hidden="true">🎮</span>{" "}
                        {steamDeckExporting ? "Converting…" : "Steam Deck video"}
                      </button>
                    )}
                    {showDirectorItem && (
                      <button
                        type="button"
                        role="menuitem"
                        onClick={() => void handleCreateProject()}
                        disabled={createProjectFromJob.isPending}
                        title="Starts a new Director project with this already-rendered clip as its first scene -- no re-render needed."
                      >
                        {createProjectFromJob.isPending ? "Creating…" : "Create Director project"}
                      </button>
                    )}
                  </div>
                )}
              </div>
              <button type="button" onClick={() => onRedo(job.data)}>
                <span aria-hidden="true">↻</span> Redo
              </button>
              <button type="button" onClick={toggleArchived}>
                <span aria-hidden="true">🗄</span> {job.data.is_archived ? "Unarchive" : "Archive"}
              </button>
              {(job.data.status === "queued" || job.data.status === "processing") && (
                <button
                  type="button"
                  className="button-danger"
                  onClick={handleCancel}
                  disabled={cancelJob.isPending}
                >
                  <span aria-hidden="true">⏹</span>{" "}
                  {cancelJob.isPending ? "Cancelling…" : "Cancel job"}
                </button>
              )}
              {confirmingDelete ? (
                <>
                  <span className="hint">Delete this job? This can't be undone.</span>
                  <button
                    type="button"
                    className="button-danger"
                    onClick={handleDelete}
                    disabled={deleteJob.isPending}
                  >
                    {deleteJob.isPending ? "Deleting…" : "Yes, delete"}
                  </button>
                  <button type="button" onClick={() => setConfirmingDelete(false)}>
                    Cancel
                  </button>
                </>
              ) : (
                <button
                  type="button"
                  className="button-danger"
                  onClick={() => setConfirmingDelete(true)}
                  disabled={job.data.status === "processing"}
                  title={
                    job.data.status === "processing"
                      ? "Can't delete a job that's currently processing."
                      : undefined
                  }
                >
                  <span aria-hidden="true">🗑</span> Delete
                </button>
              )}
            </div>
            {deleteJob.isError && <p className="error">Couldn't delete that job. Try again.</p>}
            {cancelJob.isError && <p className="error">Couldn't cancel that job. Try again.</p>}
            {createProjectFromJob.isError && <p className="error">Couldn't create a project from this job. Try again.</p>}
            {steamDeckError && <p className="error">Couldn't convert this video. Try again.</p>}

            {/* Re-queue dialog: a NESTED .modal-overlay inside this modal's
                own .modal (not a sibling of the outer overlay). It's
                position:fixed + inset:0 with the same z-index as the outer
                overlay but later in the DOM, so it paints on top of the job
                modal and covers the viewport -- see App.css's .requeue-modal.
                Kept inside the .modal (rather than as a sibling) because
                that's what makes the same-z-index stacking work. */}
            {requeueOpen && (
              <div className="modal-overlay" onClick={() => setRequeueOpen(false)}>
                <div
                  className="modal requeue-modal"
                  onClick={(e) => e.stopPropagation()}
                  role="dialog"
                  aria-label="Re-queue this job"
                >
                  <h2>Re-queue this job</h2>
                  <p className="hint">
                    Queue identical copies of this job -- same prompt, settings, and reference
                    files. Each copy is a full render in its own right and joins the queue behind
                    whatever's already in it.
                  </p>
                  <label className="requeue-count-label">
                    How many copies?
                    <input
                      type="number"
                      min={1}
                      max={10}
                      step={1}
                      value={requeueCount}
                      onChange={(e) => setRequeueCount(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") void handleRequeue();
                      }}
                      autoFocus
                    />
                  </label>
                  {requeueError && <p className="error">Couldn't re-queue this job. Try again.</p>}
                  <div className="modal-actions">
                    <button
                      type="button"
                      className="button button-primary"
                      onClick={() => void handleRequeue()}
                      disabled={requeueJob.isPending || !requeueCountValid}
                    >
                      {requeueJob.isPending
                        ? "Queuing…"
                        : `Queue ${requeueCountValue} ${requeueCountValue === 1 ? "copy" : "copies"}`}
                    </button>
                    <button type="button" onClick={() => setRequeueOpen(false)}>
                      Cancel
                    </button>
                  </div>
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
