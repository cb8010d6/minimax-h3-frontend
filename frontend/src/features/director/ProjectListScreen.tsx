import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useCreateDirectorProject, useDeleteDirectorProject, useDirectorProjects } from "../../api/directorQueries";
import type { Project } from "../../api/directorTypes";
import { CloseIcon, TrashIcon } from "../shared/Icon";
import { useI18n } from "../../i18n";

function relativeTime(iso: string, isChinese: boolean): string {
  const diffMin = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (diffMin < 1) return isChinese ? "刚刚" : "just now";
  if (diffMin < 60) return isChinese ? `${diffMin} 分钟前` : `${diffMin}m ago`;
  const diffHr = Math.round(diffMin / 60);
  if (diffHr < 24) return isChinese ? `${diffHr} 小时前` : `${diffHr}h ago`;
  return new Date(iso).toLocaleDateString();
}

function formatEta(seconds: number, isChinese: boolean): string {
  if (seconds < 60) return isChinese ? `约剩 ${Math.round(seconds)} 秒` : `~${Math.round(seconds)}s left`;
  const minutes = Math.round(seconds / 60);
  return isChinese ? `约剩 ${minutes} 分钟` : `~${minutes}m left`;
}

function progressLabel(project: Project, done: number, total: number, active: number, isChinese: boolean): string {
  const parts = [isChinese ? `已完成 ${done}/${total}` : `${done}/${total} rendered`];
  if (active > 0) parts.push(isChinese ? "生成中…" : "rendering…");
  else if (project.eta_seconds) parts.push(formatEta(project.eta_seconds, isChinese));
  return parts.join(" · ");
}

export function ProjectListScreen() {
  const { t } = useI18n();
  const navigate = useNavigate();
  const projects = useDirectorProjects();
  const createProject = useCreateDirectorProject();
  const [newTitle, setNewTitle] = useState("");

  async function handleCreate(e: FormEvent) {
    e.preventDefault();
    const project = await createProject.mutateAsync({ title: newTitle.trim() || undefined });
    navigate(`/director/${project.id}`);
  }

  return (
    <section className="screen director-list-screen">
      <h1>{t("director.title", "Director Mode")}</h1>
      <p className="hint">
        {t("director.description", "Sequence multiple clips into one long video, with optional continuity between adjacent scenes.")}
      </p>

      <form className="director-new-project-form" onSubmit={(e) => void handleCreate(e)}>
        <input
          type="text"
          placeholder={t("director.newTitle", "New project title…")}
          value={newTitle}
          onChange={(e) => setNewTitle(e.target.value)}
        />
        <button type="submit" className="button button-primary" disabled={createProject.isPending}>
          {createProject.isPending ? t("director.creating", "Creating…") : t("director.newProject", "New project")}
        </button>
      </form>
      {createProject.isError && <p className="error">{t("director.createError", "Couldn't create that project. Try again.")}</p>}

      {projects.isLoading && <p className="hint">{t("common.loading", "Loading…")}</p>}
      {projects.isError && <p className="error">{t("director.loadError", "Couldn't load your projects.")}</p>}
      {projects.data?.length === 0 && (
        <p className="empty-state">{t("director.empty", "No projects yet — create one above to start sequencing clips.")}</p>
      )}

      <ul className="director-project-list">
        {projects.data?.map((project) => (
          <ProjectCard key={project.id} project={project} onOpen={() => navigate(`/director/${project.id}`)} />
        ))}
      </ul>
    </section>
  );
}

function DeleteProjectModal({ project, onClose }: { project: Project; onClose: () => void }) {
  const { t } = useI18n();
  const deleteProject = useDeleteDirectorProject();
  const [deleteRelatedJobs, setDeleteRelatedJobs] = useState(false);

  async function handleDelete() {
    await deleteProject.mutateAsync({ projectId: project.id, deleteRelatedJobs });
    onClose();
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <button type="button" className="modal-close" onClick={onClose} aria-label={t("common.close", "Close")}>
          <CloseIcon size={16} />
        </button>
        <h2>{t("director.deleteProject", "Delete project?")}</h2>
        <p className="hint">
          {t("director.deleteWarning", "“{title}” and its clips will be removed. This can't be undone.", {
            title: project.title || `${t("director.untitled", "Untitled project")} #${project.id}`,
          })}
        </p>
        <label className="clip-editor-continues-toggle">
          <input
            type="checkbox"
            checked={deleteRelatedJobs}
            onChange={(e) => setDeleteRelatedJobs(e.target.checked)}
          />
          {t("director.deleteVideos", "Also delete the rendered videos for this project's clips")}
          <span className="hint">
            {" "}{t("director.deleteVideosHint", "— otherwise they stay in Generate history without this project tag. Active videos are never deleted.")}
          </span>
        </label>
        {deleteProject.isError && <p className="error">{t("director.deleteError", "Couldn't delete that project. Try again.")}</p>}
        <div className="modal-actions">
          <button type="button" onClick={onClose} disabled={deleteProject.isPending}>
            {t("common.cancel", "Cancel")}
          </button>
          <button
            type="button"
            className="button-danger"
            onClick={() => void handleDelete()}
            disabled={deleteProject.isPending}
          >
            {deleteProject.isPending ? t("director.deleting", "Deleting…") : t("director.confirmDelete", "Yes, delete")}
          </button>
        </div>
      </div>
    </div>
  );
}

function ProjectCard({ project, onOpen }: { project: Project; onOpen: () => void }) {
  const { t, isChinese } = useI18n();
  const [deleteModalOpen, setDeleteModalOpen] = useState(false);

  const total = project.clip_count ?? 0;
  const dirty = project.dirty_count ?? 0;
  const active = project.active_count ?? 0;
  const done = total - dirty;
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;

  return (
    <li className="director-project-card">
      <button type="button" className="director-project-card-open" onClick={onOpen}>
        <span className="director-project-card-title">{project.title || `${t("director.untitled", "Untitled project")} #${project.id}`}</span>
        <span className="director-project-card-meta">
          {t("director.updated", "Updated {time}", { time: relativeTime(project.updated_at, isChinese) })}
          {total > 0 && <> · {progressLabel(project, done, total, active, isChinese)}</>}
        </span>
        {total > 0 && (
          <div className="job-progress-track director-project-progress-track">
            <div className="job-progress-fill" style={{ width: `${pct}%` }} />
          </div>
        )}
      </button>

      <div className="director-project-card-actions">
        <button
          type="button"
          className="link-button"
          onClick={(e) => {
            e.stopPropagation();
            setDeleteModalOpen(true);
          }}
        >
          <TrashIcon size={13} /> {t("common.delete", "Delete")}
        </button>
      </div>

      {deleteModalOpen && <DeleteProjectModal project={project} onClose={() => setDeleteModalOpen(false)} />}
    </li>
  );
}
