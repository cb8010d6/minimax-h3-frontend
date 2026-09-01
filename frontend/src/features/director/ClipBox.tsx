import type { DragEvent } from "react";
import type { Clip } from "../../api/directorTypes";
import { MODE_LABELS } from "../../api/types";
import { useI18n } from "../../i18n";
import { JobProgressBar } from "../queue/JobProgressBar";
import { ChevronRightIcon } from "../shared/Icon";

function statusClass(clip: Clip): string {
  if (clip.current_job_status === "queued" || clip.current_job_status === "processing") return "clip-box-active";
  if (clip.current_job_status === "done" && !clip.video_url) return "clip-box-failed";
  if (clip.needs_render) return clip.current_job_id ? "clip-box-dirty" : "clip-box-new";
  return "clip-box-clean";
}

interface ClipBoxProps {
  clip: Clip;
  isFirst: boolean;
  isLast: boolean;
  onOpen: () => void;
  onMoveUp: () => void;
  onMoveDown: () => void;
  selectedForExport: boolean;
  isDragging: boolean;
  isDropTarget: boolean;
  onToggleExport: () => void;
  onDragStart: (event: DragEvent<HTMLSpanElement>) => void;
  onDragOver: (event: DragEvent<HTMLDivElement>) => void;
  onDrop: (event: DragEvent<HTMLDivElement>) => void;
  onDragEnd: () => void;
}

export function ClipBox({
  clip,
  isFirst,
  isLast,
  onOpen,
  onMoveUp,
  onMoveDown,
  selectedForExport,
  isDragging,
  isDropTarget,
  onToggleExport,
  onDragStart,
  onDragOver,
  onDrop,
  onDragEnd,
}: ClipBoxProps) {
  const { t } = useI18n();
  function translatedStatus(): string {
    if (clip.current_job_status === "queued") return t("queue.queued", "Queued");
    if (clip.current_job_status === "processing") return t("progress.rendering", "Rendering…");
    if (clip.current_job_status === "done" && !clip.video_url) return t("queue.failed", "Failed");
    if (clip.needs_render) {
      return clip.current_job_id
        ? t("director.needsRerender", "Needs re-render")
        : t("director.notRendered", "Not rendered");
    }
    return t("director.upToDate", "Up to date");
  }
  return (
    <div
      className={`clip-box-wrap${isDragging ? " clip-box-wrap-dragging" : ""}${isDropTarget ? " clip-box-wrap-drop-target" : ""}`}
      onDragOver={onDragOver}
      onDrop={onDrop}
    >
      {clip.continues_previous && <div className="clip-chain-connector" aria-hidden="true" />}
      <div className={`clip-box ${statusClass(clip)}`}>
        <div className="clip-box-order-row">
          <label className="clip-export-select" title={t("director.includeInExport", "Include in export")}>
            <input type="checkbox" checked={selectedForExport} onChange={onToggleExport} />
            <span className="clip-box-order">#{clip.order + 1}</span>
          </label>
          <div className="clip-box-move-buttons">
            <span
              className="clip-drag-handle"
              draggable
              onDragStart={onDragStart}
              onDragEnd={onDragEnd}
              aria-label={t("director.dragToReorder", "Drag to reorder")}
              title={t("director.dragToReorder", "Drag to reorder")}
            >
              ⠿
            </span>
            <button type="button" disabled={isFirst} onClick={onMoveUp} aria-label={t("director.moveEarlier", "Move earlier")} title={t("director.moveEarlier", "Move earlier")}>
              <ChevronRightIcon size={11} style={{ transform: "rotate(180deg)" }} />
            </button>
            <button type="button" disabled={isLast} onClick={onMoveDown} aria-label={t("director.moveLater", "Move later")} title={t("director.moveLater", "Move later")}>
              <ChevronRightIcon size={11} />
            </button>
          </div>
        </div>
        <button type="button" className="clip-box-thumb" onClick={onOpen}>
          {clip.thumbnail_url ? (
            <img src={clip.thumbnail_url} alt="" />
          ) : clip.video_url ? (
            <video src={clip.video_url} muted preload="metadata" />
          ) : (
            <span className="clip-box-placeholder">{MODE_LABELS[clip.mode][0]}</span>
          )}
        </button>
        <button type="button" className="clip-box-body" onClick={onOpen}>
          <span className="clip-box-mode">{t(`mode.${clip.mode}`, MODE_LABELS[clip.mode])}</span>
          <span className="clip-box-prompt">{clip.prompt || <em>{t("director.noPrompt", "No prompt yet")}</em>}</span>
          {/* Strip the card-state prefix for the matching status pill class. */}
          <span className={`clip-box-status clip-box-status-${statusClass(clip).replace("clip-box-", "")}`}>
            {translatedStatus()}
          </span>
          {clip.current_job_status === "processing" && (
            <JobProgressBar job={{ phase: clip.phase ?? "", progress_current: clip.progress_current, progress_total: clip.progress_total }} />
          )}
        </button>
      </div>
    </div>
  );
}
