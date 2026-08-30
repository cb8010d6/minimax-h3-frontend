import { useEffect, useRef, useState, type FormEvent } from "react";
import type { JobFolder } from "../../api/types";

interface FolderPickerProps {
  folders: JobFolder[];
  // Ids of the folders currently selected -- an existing job's current
  // membership (JobModal, QueueEntry) or a not-yet-queued draft's pending
  // selection (GenerateScreen).
  selectedIds: number[];
  onToggle: (folderId: number) => void;
  // Creates a folder with this name and (per the caller) selects/attaches it
  // in the same step -- see each caller's own createAndAttach.../
  // createAndSelect... (this component only knows about creating).
  onCreate: (name: string) => void;
  creating?: boolean;
}

// Shared chip-toggle UI for folder membership -- used inline in JobModal's
// "Folders" detail row, inside QueueEntry's quick-action popover (see
// QueueSidebar.tsx), and in GenerateScreen's pre-queue folder picker, so the
// toggle/create interaction only exists in one place. "New folder" is a
// trailing "+" chip that opens its own small popover for the name rather
// than an always-visible inline text field, so the chip row stays compact
// in the two places (QueueEntry, JobModal) where this already sits inside
// a modal/popover of its own.
export function FolderPicker({ folders, selectedIds, onToggle, onCreate, creating }: FolderPickerProps) {
  const [addOpen, setAddOpen] = useState(false);
  const [newName, setNewName] = useState("");
  const addAnchorRef = useRef<HTMLDivElement>(null);
  const selected = new Set(selectedIds);

  // Same outside-click/Escape pattern used elsewhere for a plain-<div>
  // popover (see JobModal's "⋯ More" menu, QueueEntry's own folder popover).
  useEffect(() => {
    if (!addOpen) return;
    function onPointerDown(e: PointerEvent) {
      if (addAnchorRef.current && !addAnchorRef.current.contains(e.target as Node)) {
        setAddOpen(false);
      }
    }
    function onKeyDown(e: globalThis.KeyboardEvent) {
      if (e.key === "Escape") setAddOpen(false);
    }
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [addOpen]);

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const name = newName.trim();
    if (!name) return;
    onCreate(name);
    setNewName("");
    setAddOpen(false);
  }

  return (
    <div className="folder-picker">
      <div className="folder-picker-chips">
        {folders.length === 0 && <span className="folder-picker-empty-hint hint">No folders yet</span>}
        {folders.map((folder) => {
          const active = selected.has(folder.id);
          return (
            <button
              key={folder.id}
              type="button"
              className={`folder-chip${active ? " folder-chip-active" : ""}`}
              aria-pressed={active}
              onClick={() => onToggle(folder.id)}
            >
              {active && <span aria-hidden="true">✓ </span>}
              {folder.name}
            </button>
          );
        })}
        <div className="folder-add-anchor" ref={addAnchorRef}>
          <button
            type="button"
            className="folder-chip-add"
            onClick={() => setAddOpen((v) => !v)}
            aria-haspopup="true"
            aria-expanded={addOpen}
            title="New folder"
          >
            <span aria-hidden="true">+</span>
          </button>
          {addOpen && (
            <div className="folder-add-popover">
              <form onSubmit={handleSubmit}>
                <input
                  type="text"
                  placeholder="New folder name…"
                  value={newName}
                  maxLength={100}
                  autoFocus
                  onChange={(e) => setNewName(e.target.value)}
                />
                <button type="submit" disabled={!newName.trim() || creating}>
                  Create
                </button>
              </form>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
