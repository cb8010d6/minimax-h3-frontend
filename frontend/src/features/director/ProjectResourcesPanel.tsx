import type { ProjectDetail } from "../../api/directorTypes";
import { useConvertToReference, useCreateProjectResource, useDeleteProjectResource } from "../../api/directorQueries";
import type { ReferenceKind } from "../../api/types";
import { useI18n } from "../../i18n";
import { DropZone } from "../shared/DropZone";

const KIND_ACCEPT: Record<ReferenceKind, string> = {
  image: "image/*",
  audio: "audio/*",
  video: "video/*",
};
interface ProjectResourcesPanelProps {
  project: ProjectDetail;
}

// Shared world/character/voice references every Clip's render draws on
// (see backend director/models.py's ProjectResource) -- distinct from a
// Clip's own reference images, which only that one clip's render sees.
export function ProjectResourcesPanel({ project }: ProjectResourcesPanelProps) {
  const { t } = useI18n();
  const createResource = useCreateProjectResource();
  const deleteResource = useDeleteProjectResource();
  const convertToReference = useConvertToReference();

  // Only a reference clip's render can actually wire a shared resource in
  // (see backend director/services.py's _combined_references()) -- adding
  // one is rejected while any other-mode clip exists, so surface that
  // proactively instead of letting the user hit a 400 from the add button.
  const hasNonReferenceClips = project.clips.some((c) => c.mode !== "r2v");

  return (
    <fieldset className="director-resources-panel">
      <legend>{t("director.sharedResources", "Shared resources")}</legend>
      <p className="hint">
        {t("director.sharedResourcesHint", "Reusable character, voice, world and style references for every reference-mode clip. Insert tokens such as")}{" "}
        <code>&lt;Picture 1&gt;</code>{" "}{t("director.sharedResourcesHintEnd", "in clip prompts. Adding one requires every generated clip to use reference mode.")}
      </p>
      {project.resources.length > 0 && (
        <ul className="reference-list director-resource-list">
          {project.resources.map((resource) => (
            <li key={resource.id} className="reference-item">
              {resource.kind === "image" && resource.url && (
                <img src={resource.url} className="ref-thumb-sm" alt="" />
              )}
              <span>{resource.label}</span>
              <button
                type="button"
                onClick={() => deleteResource.mutate({ projectId: project.id, resourceId: resource.id })}
              >
                {t("common.remove", "Remove")}
              </button>
            </li>
          ))}
        </ul>
      )}
      {hasNonReferenceClips ? (
        <div>
          <p className="hint">
            {t("director.convertReferencesHint", "This project has non-reference generated clips. Convert them before adding shared references; existing videos and clip references are preserved.")}
          </p>
          <button
            type="button"
            onClick={() => convertToReference.mutate(project.id)}
            disabled={convertToReference.isPending}
          >
            {convertToReference.isPending
              ? t("director.converting", "Converting…")
              : t("director.convertAllReferences", "Convert all clips to reference mode")}
          </button>
          {convertToReference.isError && <p className="error">{t("director.convertError", "Couldn't convert. Try again.")}</p>}
        </div>
      ) : (
        <div className="director-resource-add-row">
          {(["image", "audio", "video"] as ReferenceKind[]).map((kind) => (
            <DropZone
              key={kind}
              accept={KIND_ACCEPT[kind]}
              className="file-slot"
              onFiles={(files) => createResource.mutate({ projectId: project.id, kind, file: files[0] })}
            >
              + {t(`director.resource.${kind}`, kind === "image" ? "Character sheet / reference image" : kind === "audio" ? "Voice reference" : "Reference video")}
              <input
                type="file"
                accept={KIND_ACCEPT[kind]}
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (file) createResource.mutate({ projectId: project.id, kind, file });
                  e.target.value = "";
                }}
              />
            </DropZone>
          ))}
        </div>
      )}
      {createResource.isError && <p className="error">{t("director.resourceError", "Couldn't add that resource. Try again.")}</p>}
    </fieldset>
  );
}
