"""Dirty-cascade rule and render orchestration for Director Mode -- kept out
of models.py so the model definitions stay easy to read on their own, and
out of api.py so the rule is unit-testable/reusable without going through a
view. See the approved plan's "Data model" and "Backend rendering engine"
sections for the full design this implements.
"""

from __future__ import annotations

import re
import uuid

from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import F
from django_q.tasks import async_task

from generation.api import _MAX_REFERENCE_AUDIO, _resolve_use_turbo
from generation.models import REFERENCE_FLOW_MODES, GenerationJob, Mode, ReferenceAsset, RenderDuration, RenderPreset
from generation.resolution import compute_resolution
from integrations import media_post, motion_context, turbo

from .models import CONTINUATION_CAPABLE_MODES, Clip, ClipReferenceAsset, JobProjectTag, Project, ProjectResource

# The only modes Director ever creates clips in -- shared by clips()/
# apply_planned_scenes()'s quality-tier lookups below (RenderPreset rows
# for image/audio modes are a separate, differently-labeled catalog, see
# generation/models.py's Mode docstring -- not relevant here).
_VIDEO_MODES = (Mode.TEXT_TO_VIDEO, Mode.IMAGE_TO_VIDEO, Mode.REFERENCE_TO_VIDEO)

_ACTIVE_JOB_STATUSES = {GenerationJob.Status.QUEUED, GenerationJob.Status.PROCESSING}

# Modes plan_scenes() (and a human editing its output) may propose when a
# project has no shared references -- r2v is deliberately excluded here
# even though it's in CONTINUATION_CAPABLE_MODES: without a reference to
# actually point at, there's no legitimate way for a planned scene to land
# on it. When the project DOES have shared references, normalize_planned_
# scenes() forces every scene to r2v instead of consulting this set at all
# -- see its require_reference_mode param.
_PLANNABLE_MODES = {Mode.TEXT_TO_VIDEO, Mode.IMAGE_TO_VIDEO}

# <Picture N>/<Video N>/<Audio N> -- matches ReferenceAsset.label's own
# token convention. Used by renumber_clip_reference_tokens() below to keep
# a Clip's written prompt text in sync with what a token number actually
# means after a ProjectResource of the same kind is added/removed.
_REFERENCE_TOKEN_PATTERNS = {
    ReferenceAsset.Kind.IMAGE: (re.compile(r"<Picture (\d+)>"), "Picture"),
    ReferenceAsset.Kind.VIDEO: (re.compile(r"<Video (\d+)>"), "Video"),
    ReferenceAsset.Kind.AUDIO: (re.compile(r"<Audio (\d+)>"), "Audio"),
}

# How much of the predecessor's own rendered audio continues_audio pulls
# in -- see Clip.continues_audio's own docstring and
# _predecessor_audio_tail_bytes() below. Not user-configurable (yet):
# picked as a plausible middle ground, not tuned against a real render.
AUDIO_TAIL_SECONDS = 3.0


class RenderConflict(Exception):
    """Raised when a render is requested for a Clip whose dirty chain
    overlaps one already in flight -- see render_clip(). Translated to a
    409 by director/api.py, same shape as generation/api.py's own 409s."""


class PlanError(Exception):
    """Raised by apply_planned_scenes() when the (possibly user-edited)
    scene list can't be turned into real Clips -- e.g. no active
    RenderPreset/RenderDuration exists for a proposed mode. Translated to a
    400 by director/api.py."""


def _predecessor(clip: Clip) -> Clip | None:
    return Clip.objects.filter(project_id=clip.project_id, order__lt=clip.order).order_by("-order").first()


def available_quality_labels() -> list[str]:
    """Every distinct RenderPreset.label active for at least one of
    Director's video modes, in catalog order -- the valid values for
    Project.quality_label (see director/api.py's projects()/project_detail()
    validation). Deduplicated in Python rather than via .distinct() +
    .order_by(): Postgres rejects SELECT DISTINCT <col> ordered by columns
    outside that same column list, which sort_order/mode/megapixels would be.
    """
    seen: list[str] = []
    labels = (
        RenderPreset.objects.filter(mode__in=_VIDEO_MODES, is_active=True)
        .order_by("sort_order", "mode", "megapixels")
        .values_list("label", flat=True)
    )
    for label in labels:
        if label not in seen:
            seen.append(label)
    return seen


def resolve_preset_for_mode(quality_label: str, mode: str) -> RenderPreset | None:
    """The RenderPreset `mode` should use for Project.quality_label. Quality
    tiers share a label across modes but each mode has its own row with its
    own megapixels/steps (see RenderPreset's own docstring), so this is a
    per-mode lookup rather than a single FK on Project. Falls back to the
    first active preset for the mode at all if the label doesn't have a row
    for it (e.g. an admin retired that combination after a project already
    picked it) -- consistent with this app's general soft-reroute-rather-
    than-hard-fail posture around is_active gating elsewhere.
    """
    preset = (
        RenderPreset.objects.filter(mode=mode, label=quality_label, is_active=True).first()
        if quality_label
        else None
    )
    return preset or RenderPreset.objects.filter(mode=mode, is_active=True).first()


def _nearest_duration(preset: RenderPreset, target_seconds: float) -> RenderDuration | None:
    """The active RenderDuration under `preset` closest to target_seconds --
    used when a Clip's preset changes (Project.quality_label edited) so its
    duration choice carries over as closely as possible instead of silently
    resetting."""
    durations = list(preset.durations.filter(is_active=True))
    return min(durations, key=lambda d: abs(d.duration_seconds - target_seconds)) if durations else None


def recompute_project_resolutions(project: Project) -> None:
    """Re-derives every Clip's preset/duration/width/height from the
    project's current aspect_ratio/quality_label -- call after either
    changes (see director/api.py's project_detail() PATCH). Both are
    project-wide settings now (MiniMax H3's continuity model requires
    consistent resolution *and duration* across a chain, and a shared
    quality tier keeps every clip's render comparable); a Clip's own
    preset/duration/width/height are just cached derivations of them, not
    independently chosen.
    """
    predecessor: Clip | None = None
    for clip in project.clips.order_by("order").select_related("preset", "duration"):
        preset = resolve_preset_for_mode(project.quality_label, clip.mode)
        if preset is not None and preset.id != clip.preset_id:
            duration = _nearest_duration(preset, clip.duration.duration_seconds)
            if duration is not None:
                clip.preset = preset
                clip.duration = duration
        elif preset is not None:
            clip.preset = preset

        if clip.continues_previous and predecessor is not None:
            clip.width, clip.height = predecessor.width, predecessor.height
            clip.duration = predecessor.duration
        else:
            clip.width, clip.height = compute_resolution(clip.preset.megapixels, project.aspect_ratio)

        clip.save(update_fields=["preset", "duration", "width", "height"])
        predecessor = clip


def resolve_clip_width_height(clip: Clip) -> tuple[int, int]:
    """Width/height for `clip` right now, given its current preset,
    continues_previous, and its project's aspect_ratio -- locked to the
    immediate predecessor's own width/height when continuing (guarantees an
    exact pixel match across a continuation run even if the predecessor's
    mode resolves to different megapixels under the same quality tier),
    otherwise computed fresh. Used when continues_previous is toggled after
    creation (director/api.py's clip_detail() PATCH) -- unlike
    recompute_project_resolutions(), this only ever touches one Clip.
    """
    if clip.continues_previous:
        predecessor = _predecessor(clip)
        if predecessor is not None:
            return predecessor.width, predecessor.height
    return compute_resolution(clip.preset.megapixels, clip.project.aspect_ratio)


def resolve_clip_duration(clip: Clip) -> RenderDuration:
    """The RenderDuration `clip` must actually use right now -- locked to
    the immediate predecessor's own duration while continues_previous
    (transitively, the whole run's duration, since every continuing clip
    locks to whichever comes right before it), otherwise `clip`'s own.

    This isn't a resolution/aspect-ratio-style cosmetic lock: real chain
    continuity submits one `default_duration_seconds` for the *entire*
    plan (see _resolve_chain_params()), validated by ComfyUI's
    MiniMaxH3ChainLoopStart against whatever scene 1 was originally
    submitted with -- a per-clip duration that drifts from the run's
    first clip makes every later resubmission in that run fail outright
    ("clip N was generated from different settings, prompts, seeds, or
    durations", confirmed against a real failure). Used both when
    continues_previous is toggled after creation (clip_detail() PATCH)
    and defensively inside _resolve_chain_params() itself.
    """
    if clip.continues_previous:
        predecessor = _predecessor(clip)
        if predecessor is not None:
            return predecessor.duration
    return clip.duration


def project_requires_reference_mode(project: Project) -> bool:
    """True once a Project has any shared resource -- see director/api.py's
    project_resources()/clips() POST handlers: while true, every Clip in
    the project must be mode=r2v, since only MiniMaxH3ReferenceToVideo's job
    actually wires ref_image_N/etc (see _combined_references() below) -- a
    t2v/i2v clip would have no way to honor a project resource's <Picture N>
    token even though the resource is meant to be usable from any clip in
    the project.
    """
    return project.resources.exists()


def _combined_references(clip: Clip) -> list[ProjectResource | ReferenceAsset]:
    """Every reference this Clip's render should feed into its new
    GenerationJob, in the exact order _build_job_for_clip() copies them in
    -- this Clip's project's shared resources first (so a "<Picture N>"
    token means the same thing in every Clip's prompt, matching what
    ProjectResourcesPanel shows the user -- see ClipReferenceAsset.label's
    matching offset), then this Clip's own references appended after, per
    kind. Mirrors generation/tasks.py's own
    job.references.filter(kind=...).order_by("order", "id") consumption
    order (see that module's _build_workflow_for_job()).
    """
    combined = []
    for kind in (ReferenceAsset.Kind.IMAGE, ReferenceAsset.Kind.AUDIO, ReferenceAsset.Kind.VIDEO):
        project_items = list(clip.project.resources.filter(kind=kind).order_by("order", "id"))
        combined.extend(project_items)
        for offset, item in enumerate(clip.references.filter(kind=kind).order_by("order", "id")):
            item.order = len(project_items) + offset
            combined.append(item)
    return combined


def _cited_ordinals(text: str, kind: str) -> set[int]:
    """Which <Picture N>/<Video N>/<Audio N> ordinals of `kind` are
    actually mentioned in `text` -- see _render_references_and_prompts()
    and cited_project_resource_count() below, its two callers."""
    pattern, _word = _REFERENCE_TOKEN_PATTERNS[kind]
    return {int(n) for n in pattern.findall(text)}


def cited_project_resource_count(clip: Clip, kind: str) -> int:
    """How many of clip.project's own ProjectResources of `kind` are
    actually cited by clip.prompt/improved_prompt -- what
    _render_references_and_prompts() below will actually wire in for a
    non-continuity render of `clip`. Exposed so director/api.py could, in
    principle, size a per-kind limit check against a clip's real citation
    count -- deliberately NOT used for that today (see
    _render_references_and_prompts()'s docstring on why every existing
    limit check keeps assuming full inclusion instead); kept as a public
    entry point for whatever surfaces this count to the user (e.g. an
    "N of M shared references actually used" hint) without duplicating
    the citation scan.
    """
    total = clip.project.resources.filter(kind=kind).count()
    if total == 0:
        return 0
    cited = _cited_ordinals(f"{clip.prompt}\n{clip.improved_prompt}", kind)
    return sum(1 for ordinal in range(1, total + 1) if ordinal in cited)


def _render_references_and_prompts(clip: Clip) -> tuple[list[ProjectResource | ReferenceAsset], str, str]:
    """The references, and the prompt/improved_prompt text, a *non*-
    continuity render of `clip` actually submits -- cite-gated, unlike
    _combined_references() above (used instead whenever real chain
    continuity is active, see _build_job_for_clip(): a chained submission
    resends every earlier scene's own *stored* prompt verbatim as part of
    the plan (see _resolve_chain_params()'s `shots`), so this clip's own
    reference wiring has to agree with the same full-inclusion numbering
    those already assume. Only a non-continuity render is free to trim
    what it wires in without contradicting anything upstream).

    A project resource is only wired into the render -- and only
    occupies a slot in the renumbered <Kind N> tokens actually sent --
    when clip.prompt or clip.improved_prompt cites its *authoring*-time
    token (see ClipReferenceAsset.label/ProjectResource.token_label,
    which always count every project resource regardless of what any one
    clip cites -- that numbering is what the user writes against and
    never changes here; this function only ever produces a render-time
    copy, never written back to the Clip). Lets a project keep a large
    shared reference pool without every r2v clip paying for -- or
    hitting the per-kind cap on -- a resource it never mentions: an
    uncited resource costs this clip nothing. This clip's own references
    are always included regardless of citation; they were deliberately
    attached to this one clip, so there's nothing to cite.

    Per-kind limit checks (director/api.py's project_resources() POST /
    clip_references() POST / promote_clip_reference()) deliberately keep
    assuming full inclusion rather than a clip's real citation count --
    the conservative check, and the right one: whether this gating even
    applies to a given render depends on chain availability and
    continues_previous, both of which can change after a reference was
    added, so a clip has to stay valid under full inclusion too.
    """
    project = clip.project
    combined: list[ProjectResource | ReferenceAsset] = []
    remaps: dict[str, dict[int, int]] = {}
    cite_text = f"{clip.prompt}\n{clip.improved_prompt}"

    for kind in (ReferenceAsset.Kind.IMAGE, ReferenceAsset.Kind.AUDIO, ReferenceAsset.Kind.VIDEO):
        project_items = list(project.resources.filter(kind=kind).order_by("order", "id"))
        cited = _cited_ordinals(cite_text, kind)
        kept = [(old, r) for old, r in enumerate(project_items, start=1) if old in cited]
        remap = {old: new for new, (old, _r) in enumerate(kept, start=1)}
        # Skipping an uncited resource can leave a gap in the middle of
        # `kept` -- unlike _combined_references() above, a kept item's
        # own .order can no longer be trusted to already match its
        # position in the compacted list, so it's reassigned here rather
        # than carried over from project-wide storage.
        for new, (_old, r) in enumerate(kept, start=1):
            r.order = new - 1
            combined.append(r)

        own_refs = list(clip.references.filter(kind=kind).order_by("order", "id"))
        old_base, new_base = len(project_items), len(kept)
        for offset, item in enumerate(own_refs):
            remap[old_base + offset + 1] = new_base + offset + 1
            item.order = new_base + offset
            combined.append(item)
        remaps[kind] = remap

    def _remap_text(text: str) -> str:
        for kind, remap in remaps.items():
            pattern, word = _REFERENCE_TOKEN_PATTERNS[kind]

            def _sub(match: re.Match, remap=remap, word=word) -> str:
                new = remap.get(int(match.group(1)))
                return f"<{word} {new}>" if new is not None else match.group(0)

            text = pattern.sub(_sub, text)
        return text

    return combined, _remap_text(clip.prompt), _remap_text(clip.improved_prompt)


def renumber_clip_reference_tokens(
    project: Project, kind: str, old_project_count: int, new_project_count: int, *, exclude_clip: Clip | None = None
) -> None:
    """Rewrites every "<Picture N>"/"<Video N>"/"<Audio N>" token of `kind`
    in every Clip's prompt/improved_prompt so it keeps pointing at the same
    underlying reference after a ProjectResource of this kind is added or
    removed (see director/api.py's project_resources()/resource_detail()).
    _combined_references() puts this project's shared resources of a kind
    before any Clip's own references of that kind, so both a surviving
    project resource's own token number (shared across every clip) and the
    point where a clip's *own* references start shift whenever the
    project's resource count for that kind changes -- a uniform delta
    handles both: a token number at or below whichever count is smaller
    (the surviving overlap) is untouched, everything above it shifts by
    the same delta, which is exactly right whether a resource was appended
    (nothing below shifts) or removed from the middle (everything after it
    shifts down by one, including subsequent project resources).

    Purely text-based -- a written prompt has no structured link to "which
    reference this number means", only the literal token -- so this can't
    fix a mention of the one resource actually being deleted (there's
    nothing left to remap it to, and it may now coincidentally collide
    with another resource's new number); every other still-valid mention
    is kept correct. Doesn't mark anything dirty itself -- the caller
    already does via mark_project_dirty() for the resource add/remove
    itself.

    exclude_clip: skip that one Clip entirely -- for
    promote_clip_reference(), whose source Clip is simultaneously losing a
    slot from its own numbering, which this uniform-delta shift doesn't
    model (see that function's own docstring for why its prompt is left
    unrewritten instead of guessing).
    """
    delta = new_project_count - old_project_count
    if delta == 0:
        return
    pattern, word = _REFERENCE_TOKEN_PATTERNS[kind]
    boundary = min(old_project_count, new_project_count)

    def remap(match: re.Match) -> str:
        n = int(match.group(1))
        return f"<{word} {n}>" if n <= boundary else f"<{word} {n + delta}>"

    for clip in project.clips.all():
        if exclude_clip is not None and clip.id == exclude_clip.id:
            continue
        update_fields = []
        for field in ("prompt", "improved_prompt"):
            text = getattr(clip, field)
            if not text:
                continue
            new_text = pattern.sub(remap, text)
            if new_text != text:
                setattr(clip, field, new_text)
                update_fields.append(field)
        if update_fields:
            clip.save(update_fields=update_fields)


def _chain_run_prefix_clips(clip: Clip) -> list[Clip]:
    """Every Clip from the start of `clip`'s current continuation run up to
    and including `clip` itself, in order -- just `[clip]` if it isn't
    itself a continuation. Used both to size a fresh run (len == 1) and to
    build the "shots" list a resumed run's MiniMaxH3ChainPlan submission
    needs (see _resolve_chain_params()) -- every scene's prompt, not just
    the new one, since the plan is validated/hashed as a whole (confirmed
    live, see extras.md#contex-loop)."""
    chain = [clip]
    current = clip
    while current.continues_previous:
        predecessor = _predecessor(current)
        if predecessor is None:
            break
        chain.append(predecessor)
        current = predecessor
    chain.reverse()
    return chain


def _resolve_chain_params(clip: Clip) -> dict | None:
    """Returns integrations.motion_context.apply_motion_context()'s kwargs
    for `clip`, or None if it should use the last-frame fallback instead
    (extension unavailable, or -- for a continuation Clip -- its immediate
    predecessor has no real chain checkpoint of its own to resume from,
    see Clip.chain_run_name's docstring on why that's checked on the
    predecessor specifically, not walked further back).
    """
    if not motion_context.is_available():
        return None

    if clip.continues_previous:
        predecessor = _predecessor(clip)
        if predecessor is None or predecessor.needs_render or not predecessor.chain_run_name:
            return None
        run_name = predecessor.chain_run_name
        scene_number = predecessor.chain_scene_number + 1
    else:
        # Fresh run -- uuid4 (not e.g. the Clip id alone) so re-rendering
        # the same Clip repeatedly never resumes a stale prior attempt's
        # checkpoint under the same run_name.
        run_name = f"director_c{clip.id}_{uuid.uuid4().hex[:8]}"
        scene_number = 1

    chain_clips = _chain_run_prefix_clips(clip)
    assert len(chain_clips) == scene_number, "chain_run_prefix_clips length must match scene_number"
    shots = [{"id": f"clip{c.id}", "prompt": c.prompt.strip() or " "} for c in chain_clips]

    # default_duration_seconds/default_steps apply to the whole plan, not
    # just this scene -- MiniMaxH3ChainLoopStart validates every
    # resubmission against whatever scene 1 (chain_clips[0]) was
    # originally submitted with, and rejects the resume otherwise
    # ("clip N was generated from different settings, prompts, seeds, or
    # durations", confirmed against a real failure). director/api.py locks
    # a continuing Clip's own duration to its predecessor's at edit time
    # (resolve_clip_duration()) so this should already agree with `clip`'s
    # own values in practice -- reading from chain_clips[0] here too is
    # belt-and-suspenders against any clip created before that lock existed.
    head = chain_clips[0]
    # Same override as _build_job_for_clip() below: turbo's LoRA is only
    # useful at its own trained step count, so if the whole project has
    # turbo on, that -- not head.preset.steps -- is what every scene in
    # this run actually needs to agree on (MiniMaxH3ChainLoopStart rejects
    # a resume whose settings don't match scene 1's, see this function's
    # own docstring above).
    use_turbo = _resolve_use_turbo(clip.project.use_turbo)
    default_steps = (
        turbo.default_steps(is_reference_flow=head.mode in REFERENCE_FLOW_MODES)
        if use_turbo
        else head.preset.steps
    )
    return {
        "shots": shots,
        "prompt_prefix": clip.project.overarching_prompt,
        "run_name": run_name,
        "scene_number": scene_number,
        "width": clip.width,
        "height": clip.height,
        "default_duration_seconds": head.duration.duration_seconds,
        "default_steps": default_steps,
    }


def _chain_head(clip: Clip) -> Clip:
    """Walks backward from `clip` while it's a still-dirty continuation of
    an *also*-dirty predecessor, returning the earliest Clip in that run --
    either the project's first Clip, a Clip that doesn't continue its own
    predecessor (a fresh scene), or one whose predecessor is already clean
    (its checkpoint is still valid, nothing upstream needs re-rendering).
    """
    current = clip
    while current.continues_previous and current.needs_render:
        predecessor = _predecessor(current)
        if predecessor is None or not predecessor.needs_render:
            break
        current = predecessor
    return current


def _predecessor_audio_tail_bytes(clip: Clip) -> bytes | None:
    """The last AUDIO_TAIL_SECONDS of clip's immediate predecessor's own
    rendered audio -- see Clip.continues_audio's own docstring. None if
    there's no eligible predecessor render to pull it from (caller already
    checked continues_previous/mode -- this only checks the predecessor
    actually has a finished, playable render, same guard _build_job_for_clip's
    own anchor-frame extraction uses).
    """
    predecessor = _predecessor(clip)
    if predecessor is None or predecessor.needs_render or predecessor.current_job_id is None:
        return None
    pred_job = predecessor.current_job
    if not pred_job.video_file:
        return None
    pred_job.video_file.open("rb")
    try:
        video_bytes = pred_job.video_file.read()
    finally:
        pred_job.video_file.close()
    return media_post.extract_audio_tail(video_bytes, AUDIO_TAIL_SECONDS)


def _audio_tail_instruction(token: int) -> str:
    """The sentence appended to a render's prompt text pointing at the
    continues_audio tail's own <Audio N> token -- unlike every other
    reference, this one is never something the user attached or wrote a
    citation for (it's added here, at render time, with no correspondingly
    stable *authoring*-time number the way a ProjectResource/
    ClipReferenceAsset has -- see _render_references_and_prompts()), so
    nothing else in the written prompt would tell the model what it's for.
    """
    return (
        f"<Audio {token}> is the tail end of the previous shot's own sound -- "
        "continue its voice, tone, and ambience seamlessly from it."
    )


def _build_job_for_clip(clip: Clip) -> GenerationJob:
    """Creates a fresh GenerationJob for `clip`'s current content (mirrors
    generation/api.py's jobs() POST handler, minus the HTTP layer), links
    it as the Clip's current_job, and enqueues it. Assumes the caller
    (render_clip()) has already confirmed this Clip is actually ready to
    render (if continues_previous, its predecessor must be clean).

    Graceful fallback when the Contex-Loop extension isn't installed --
    or a continuation Clip's immediate predecessor has no real checkpoint
    of its own (see _resolve_chain_params()) -- see extras.md#contex-loop's
    "Graceful fallback" section: instead of true motion/audio continuity,
    feeds the previous clip's last frame in as an ordinary image reference.
    Self-healing at scene-start boundaries: the next *fresh* (non-
    continuation) Clip rendered always starts a brand new real-continuity
    run if the extension is available then, regardless of how any earlier
    part of the project rendered.

    continues_audio, if also set, layers a short clipped tail of the
    predecessor's own audio in as an ordinary reference-audio upload --
    see Clip.continues_audio's own docstring for why this is a completely
    separate mechanism from the video/motion continuity above.
    """
    chain_params = _resolve_chain_params(clip)
    anchor_frame: bytes | None = None

    # A chained submission resends every earlier scene's own stored
    # prompt verbatim (see _resolve_chain_params()'s `shots`), which
    # assumes every clip in the run wires in every project resource --
    # see _render_references_and_prompts()'s docstring for why cite-
    # gating is only safe to apply outside real chain continuity.
    if chain_params is not None:
        references = _combined_references(clip)
        render_prompt, render_improved_prompt = clip.prompt, clip.improved_prompt
    else:
        references, render_prompt, render_improved_prompt = _render_references_and_prompts(clip)

    # continues_audio: an ordinary reference-audio upload, independent of
    # chain_params either way -- apply_motion_context() never touches
    # ref_audios (see that function's own docstring), so this composes
    # cleanly whether or not real chain continuity is also active.
    audio_tail_bytes: bytes | None = None
    if clip.continues_audio and clip.continues_previous and clip.mode == Mode.REFERENCE_TO_VIDEO:
        candidate = _predecessor_audio_tail_bytes(clip)
        if candidate is not None:
            audio_count = sum(1 for r in references if r.kind == ReferenceAsset.Kind.AUDIO)
            if audio_count < _MAX_REFERENCE_AUDIO[Mode.REFERENCE_TO_VIDEO]:
                # Explicit references (shared or this clip's own) were
                # deliberately attached by the user -- the tail only takes
                # whatever slot is left over rather than displacing one.
                audio_tail_bytes = candidate
                instruction = _audio_tail_instruction(audio_count + 1)
                render_prompt = f"{render_prompt}\n\n{instruction}"
                if render_improved_prompt.strip():
                    render_improved_prompt = f"{render_improved_prompt}\n\n{instruction}"

    if clip.continues_previous:
        predecessor = _predecessor(clip)
        if predecessor is None or predecessor.needs_render or predecessor.current_job_id is None:
            raise RenderConflict(f"Clip {clip.id}'s predecessor isn't rendered yet.")

        has_own_image_ref = any(r.kind == ReferenceAsset.Kind.IMAGE for r in clip.references.all())
        # An anchor frame from the predecessor's last frame is needed
        # whenever real continuity ISN'T active (that *is* the fallback,
        # see extras.md#contex-loop), and *also* whenever it IS active but
        # this is an i2v clip with no image reference of its own:
        # MiniMaxH3ImageToVideo's first_frame is a real input on the
        # underlying sampler-prep node regardless of Director's own
        # continuity mechanism (MiniMaxH3ChainContext only wraps its
        # output, see integrations/motion_context.py) -- leaving it
        # unset means the template's own placeholder-example LoadImage
        # wiring stays in place and fails ComfyUI's validation (confirmed
        # live: this exact failure, independent of chain_params).
        needs_anchor_frame = chain_params is None or (clip.mode == Mode.IMAGE_TO_VIDEO and not has_own_image_ref)
        if needs_anchor_frame:
            pred_job = predecessor.current_job
            pred_job.video_file.open("rb")
            try:
                video_bytes = pred_job.video_file.read()
            finally:
                pred_job.video_file.close()
            anchor_frame = media_post.extract_last_frame(video_bytes)

    # Same override generation/api.py's jobs() POST handler applies:
    # turbo's LoRA is only useful at its own trained step count, so it
    # replaces clip.preset.steps entirely rather than layering on top of
    # it -- see settings.TURBO_STEPS_T2V_I2V/TURBO_STEPS_R2V.
    use_turbo = _resolve_use_turbo(clip.project.use_turbo)
    steps = (
        turbo.default_steps(is_reference_flow=clip.mode in REFERENCE_FLOW_MODES)
        if use_turbo
        else clip.preset.steps
    )

    job = GenerationJob.objects.create(
        user=clip.project.user,
        mode=clip.mode,
        preset=clip.preset,
        duration=clip.duration,
        raw_prompt=render_prompt,
        improved_prompt=render_improved_prompt,
        megapixels=clip.preset.megapixels,
        steps=steps,
        aspect_ratio=clip.project.aspect_ratio,
        width=clip.width,
        height=clip.height,
        duration_seconds=clip.duration.duration_seconds,
        estimated_seconds=clip.duration.estimated_render_seconds,
        use_turbo=use_turbo,
        model_variant=clip.project.model_variant,
        continuation_params=chain_params,
    )
    # Permanent, unlike clip.current_job below -- see JobProjectTag's own
    # docstring on why this needs to survive a later re-render reassigning
    # current_job to some other, newer job.
    JobProjectTag.objects.create(job=job, project=clip.project)

    if anchor_frame is not None:
        # Leads as the new job's first (order=0) image reference -- i2v's
        # convention is order=0 -> first_frame; r2v's is order=0 -> the
        # first <Picture N> token. Either way it's the strongest reference
        # slot, matching the intent (start this clip from exactly where
        # the previous one ended). Any image references already on the
        # clip itself shift down to make room.
        for ref in references:
            if ref.kind == ReferenceAsset.Kind.IMAGE:
                ref.order += 1
        anchor_ref = ReferenceAsset(job=job, kind=ReferenceAsset.Kind.IMAGE, order=0)
        anchor_ref.file.save("continuation_last_frame.png", ContentFile(anchor_frame), save=True)

    if audio_tail_bytes is not None:
        # Appended after every explicit audio reference (order computed
        # above from `references`, unaffected by anything added since) --
        # see the "explicit references outrank the tail" comment above.
        audio_count = sum(1 for r in references if r.kind == ReferenceAsset.Kind.AUDIO)
        tail_ref = ReferenceAsset(job=job, kind=ReferenceAsset.Kind.AUDIO, order=audio_count)
        tail_ref.file.save("continuation_audio_tail.mp3", ContentFile(audio_tail_bytes), save=True)

    for ref in references:
        ref.file.open("rb")
        try:
            content = ref.file.read()
        finally:
            ref.file.close()
        new_ref = ReferenceAsset(job=job, kind=ref.kind, order=ref.order)
        new_ref.file.save(ref.file.name.rsplit("/", 1)[-1], ContentFile(content), save=True)

    # chain_run_name/chain_scene_number are deliberately NOT set here --
    # only once this job actually finishes successfully (see signals.py's
    # on_job_finished()). Setting them optimistically at creation time was
    # a real bug caught by testing a failed render for real: nothing else
    # here guarantees MiniMaxH3ChainSegmentSave ever actually ran (a
    # rejected/failed job never produces the checkpoint it names), so a
    # later continuation Clip could otherwise "resume" from one that was
    # never saved.
    clip.current_job = job
    clip.save(update_fields=["current_job"])

    # Same shared FIFO queue processor generation/api.py's own job creation
    # enqueues -- safe to enqueue redundantly (see that module's docstring).
    async_task("generation.tasks.process_queue")
    return job


def render_clip(clip: Clip, *, force: bool = False) -> GenerationJob | None:
    """Renders `clip`, first (re-)rendering any dirty continuation
    predecessors it depends on -- see _chain_head(). Only the head's job is
    created now; the rest of the chain is created progressively as each
    predecessor finishes (see director/signals.py's _advance_chain()).
    Returns None if `clip` wasn't actually dirty and force=False (nothing
    to do). Raises RenderConflict if any Clip in the run already has a job
    in flight.

    force=True re-renders an already-clean Clip on request (e.g. "I didn't
    like the outcome") -- marks it dirty first so a failed forced attempt
    is correctly left re-renderable rather than silently staying "clean"
    with a failed current_job (director/signals.py's on_job_finished()
    only clears needs_render on success, and otherwise assumes it was
    already True going in). Only ever forces `clip` itself dirty, not its
    predecessor -- _chain_head() below stops at the first clean predecessor
    either way, so a forced re-render never cascades backward.

    Caveat for a continues_previous clip specifically: this resumes the
    *same* chain run at the *same* scene number, which deterministically
    re-derives the same seed (motion_context.base_seed_for_run() hashes
    run_name alone) -- with an unchanged prompt, a forced re-render is
    likely to reproduce a near-identical result, not a genuinely different
    one. A non-continuing clip always mints a fresh run_name (a new uuid4)
    on every render, so this limitation is specific to continuation clips
    under the real Chain API, not force-re-render in general.
    """
    clip.refresh_from_db()
    if not clip.needs_render:
        if not force:
            return None
        clip.needs_render = True
        clip.save(update_fields=["needs_render"])

    head = _chain_head(clip)
    run = list(
        Clip.objects.filter(project_id=clip.project_id, order__gte=head.order, order__lte=clip.order)
        .select_related("current_job")
        .order_by("order")
    )
    for run_clip in run:
        if run_clip.current_job_id and run_clip.current_job.status in _ACTIVE_JOB_STATUSES:
            raise RenderConflict(f"Clip {run_clip.id} already has a job {run_clip.current_job.status}.")

    head.render_chain_target = clip
    head.save(update_fields=["render_chain_target"])
    return _build_job_for_clip(head)


def render_all_dirty(project: Project) -> list[GenerationJob]:
    """Renders every dirty Clip in the project -- one render_clip() call
    per maximal dirty run (see _chain_head()), so each run's head is
    enqueued immediately; the existing single-worker FIFO naturally
    serializes them, and each head's own chain advances independently as
    its predecessor jobs finish."""
    clips = list(project.clips.order_by("order"))
    jobs = []
    i = 0
    while i < len(clips):
        if not clips[i].needs_render:
            i += 1
            continue
        j = i
        while j + 1 < len(clips) and clips[j + 1].continues_previous and clips[j + 1].needs_render:
            j += 1
        job = render_clip(clips[j])
        if job is not None:
            jobs.append(job)
        i = j + 1
    return jobs


def mark_dirty_cascade(clip: Clip) -> None:
    """Marks `clip` needing re-render, then walks forward through
    subsequent Clips in `order` while each one has continues_previous=True,
    marking each dirty too -- stopping at (not including) the first Clip
    that starts a fresh scene. Matches the user's own spec exactly: editing
    [scene start] dirties it plus every continuation directly chained after
    it, up to but not including the next fresh scene.
    """
    clip.needs_render = True
    clip.save(update_fields=["needs_render"])

    following = Clip.objects.filter(project_id=clip.project_id, order__gt=clip.order).order_by("order")
    for next_clip in following:
        if not next_clip.continues_previous:
            break
        next_clip.needs_render = True
        next_clip.save(update_fields=["needs_render"])


def split_clip(clip: Clip) -> Clip:
    """Inserts a brand-new Clip immediately after `clip`, continuing
    directly from it (continues_previous=True) -- backs the board's
    "Split" action for a scene whose prompt tries to cram too much into
    one beat (see DIRECTOR_PLAN_GUIDE_en.md's own chaining-over-cramming
    advice). Only does the structural half: position, mode, and the
    duration/width/height lock a continuing Clip always needs (see
    resolve_clip_width_height()/resolve_clip_duration()'s docstrings) --
    the new Clip starts as a copy of `clip`'s own prompt text, left for
    the user (or a subsequent AI refine/chat pass) to pare each half down
    to its own beat, since actually dividing the prose itself needs real
    language understanding this module doesn't have.

    Mode for the new Clip: r2v if the project has shared resources (every
    Clip must be r2v then, same rule project_resources() enforces
    elsewhere), `clip`'s own mode if that's already continuation-capable
    (the common case -- splitting a Clip that's already part of a chain
    should stay on the same mode), otherwise i2v -- the default "continue
    the previous shot" mode per the plan guide.

    Renumbers every later Clip's `order` up by one to make room -- safe
    against Clip's own deferred unique-together (project, order)
    constraint the same way reorder_clip() is, since it's only checked at
    commit. If the Clip that used to sit immediately after `clip` has
    continues_previous set, it's cascade-dirtied too: continues_previous
    is positional (see Clip's own docstring), so it now continues from
    the new Clip instead of `clip` -- a real change to what its render
    actually needs to match, not just a cosmetic renumbering.
    """
    project = clip.project
    if project_requires_reference_mode(project):
        mode = Mode.REFERENCE_TO_VIDEO
    elif clip.mode in CONTINUATION_CAPABLE_MODES:
        mode = clip.mode
    else:
        mode = Mode.IMAGE_TO_VIDEO

    preset = resolve_preset_for_mode(project.quality_label, mode)
    if preset is None:
        raise PlanError(f"No active render preset/duration is configured for mode {mode!r}.")

    with transaction.atomic():
        old_successor = Clip.objects.filter(project_id=project.id, order=clip.order + 1).first()

        Clip.objects.filter(project_id=project.id, order__gt=clip.order).update(order=F("order") + 1)

        new_clip = Clip.objects.create(
            project=project,
            order=clip.order + 1,
            continues_previous=True,
            mode=mode,
            prompt=clip.prompt,
            preset=preset,
            duration=clip.duration,
            width=clip.width,
            height=clip.height,
        )

        if old_successor is not None and old_successor.continues_previous:
            # Refreshed first -- the bulk update above already moved this
            # row's real `order` on by one, but this Python object still
            # holds its pre-shift value, and mark_dirty_cascade() needs the
            # real one to find the right clips to cascade through.
            old_successor.refresh_from_db()
            mark_dirty_cascade(old_successor)

    return new_clip


def _validate_import_job(job: GenerationJob, *, user_id: int) -> None:
    """Checks one already-rendered job before it is placed on a board.

    JobProjectTag, rather than only Clip.current_job, is the authoritative
    membership check: a Director re-render can move Clip.current_job away
    from an older job while its permanent tag intentionally remains (see
    JobProjectTag's docstring). Re-importing that superseded job would hit
    the tag's one-to-one constraint and, more importantly, make history
    claim the same render belongs to two projects.
    """
    if job.user_id != user_id:
        raise PlanError("Every selected render must belong to the project owner.")
    if job.mode not in _VIDEO_MODES:
        raise PlanError(f"{Mode(job.mode).label} jobs can't become a Director clip -- only video modes can.")
    if job.status != GenerationJob.Status.DONE or job.error_message or not job.video_file:
        raise PlanError("Only a successfully finished render can become a Director clip.")
    if job.director_clip.exists() or JobProjectTag.objects.filter(job_id=job.id).exists():
        raise PlanError("This job already belongs to a Director project.")


def _copy_job_references(job: GenerationJob, clip: Clip) -> None:
    """Copies reference files so deleting the source job never breaks Clip."""
    for ref in job.references.order_by("kind", "order"):
        ref.file.open("rb")
        try:
            content = ref.file.read()
        finally:
            ref.file.close()
        new_ref = ClipReferenceAsset(clip=clip, kind=ref.kind, order=ref.order)
        new_ref.file.save(ref.file.name.rsplit("/", 1)[-1], ContentFile(content), save=True)


def append_jobs_to_project(project: Project, jobs: list[GenerationJob]) -> list[Clip]:
    """Appends successful standalone video jobs as clean, independent Clips.

    Input order is preserved exactly. Imported clips never claim real H3
    continuation state, so continues_previous=False and their existing
    renders remain immediately exportable. A user can opt into continuity
    later in the editor, at which point the ordinary dirty/render rules
    apply. This powers both multi-select history import and
    create_project_from_jobs() below.
    """
    jobs = list(jobs)
    if not jobs:
        raise PlanError("Select at least one finished video.")
    job_ids = [job.id for job in jobs]
    if len(job_ids) != len(set(job_ids)):
        raise PlanError("The selected render list contains duplicates.")

    with transaction.atomic():
        project = Project.objects.select_for_update().get(id=project.id)
        locked_by_id = {
            job.id: job
            for job in GenerationJob.objects.select_for_update()
            .select_related("preset", "duration")
            .filter(id__in=job_ids)
        }
        if len(locked_by_id) != len(job_ids):
            raise PlanError("One or more selected renders no longer exist.")
        locked_jobs = [locked_by_id[job_id] for job_id in job_ids]
        for job in locked_jobs:
            _validate_import_job(job, user_id=project.user_id)

        last_order = project.clips.order_by("-order").values_list("order", flat=True).first()
        next_order = 0 if last_order is None else last_order + 1
        clips: list[Clip] = []
        for offset, job in enumerate(locked_jobs):
            clip = Clip.objects.create(
                project=project,
                order=next_order + offset,
                continues_previous=False,
                mode=job.mode,
                prompt=job.raw_prompt,
                improved_prompt=job.improved_prompt,
                preset=job.preset,
                duration=job.duration,
                width=job.width,
                height=job.height,
                needs_render=False,
                current_job=job,
            )
            _copy_job_references(job, clip)
            JobProjectTag.objects.create(job=job, project=project)
            clips.append(clip)
    return clips


def create_project_from_jobs(jobs: list[GenerationJob], *, title: str = "") -> Project:
    """Creates one Director project from several history renders, in order."""
    jobs = list(jobs)
    if not jobs:
        raise PlanError("Select at least one finished video.")
    first = jobs[0]
    project_title = title.strip() or first.title.strip() or first.raw_prompt.strip()[:80]
    with transaction.atomic():
        project = Project.objects.create(
            user=first.user,
            title=project_title,
            aspect_ratio=first.aspect_ratio,
            quality_label=first.preset.label,
            model_variant=first.model_variant,
        )
        append_jobs_to_project(project, jobs)
    return project


def create_project_from_job(job: GenerationJob) -> Project:
    """Backward-compatible one-job wrapper for the original queue action."""
    return create_project_from_jobs([job])


def delete_project(project: Project, *, delete_related_jobs: bool = False) -> None:
    """Deletes `project` and everything under it, cleaning up the actual
    files first -- Project.delete()'s own cascade (Clip/ClipReferenceAsset/
    ProjectResource/JobProjectTag rows) only removes database rows, not
    the files those rows point at, the same gap clip_detail()'s/
    resource_detail()'s own single-item DELETE handlers already work
    around by deleting a file before its row.

    delete_related_jobs: also deletes every GenerationJob ever tagged to
    this project (see JobProjectTag), including their own video/thumbnail/
    reference files. Off by default -- create_project_from_job()'s whole
    premise is "reuse a render, don't waste it," so the symmetric default
    here is to let a project's renders outlive it as ordinary untagged
    jobs in the main Generate page, not silently destroy something the
    user might still want just because its project wrapper is gone. A
    job that's currently queued/processing is never deleted regardless of
    this flag -- same guard generation/api.py's own job_detail() DELETE
    applies, just skipped here rather than failing the whole request.
    """
    if project.assembled_video_file:
        project.assembled_video_file.delete(save=False)

    for resource in project.resources.all():
        resource.file.delete(save=False)

    for clip in project.clips.all():
        for ref in clip.references.all():
            ref.file.delete(save=False)

    if delete_related_jobs:
        active_statuses = {GenerationJob.Status.QUEUED, GenerationJob.Status.PROCESSING}
        jobs = GenerationJob.objects.filter(director_project_tag__project=project).exclude(status__in=active_statuses)
        for job in jobs:
            for ref in job.references.all():
                ref.file.delete(save=False)
            if job.video_file:
                job.video_file.delete(save=False)
            if job.thumbnail_file:
                job.thumbnail_file.delete(save=False)
        jobs.delete()

    project.delete()


def mark_project_dirty(project: Project) -> None:
    """Every Clip's render depends on Project.overarching_prompt/resources,
    so changing either invalidates the whole project, not just a cascade
    from one point."""
    project.clips.update(needs_render=True)


def convert_clips_to_reference(project: Project) -> int:
    """Switches every t2v/i2v Clip in `project` to mode=r2v, in place --
    lets a project with existing clips add shared references without
    deleting them first, since project_resources() otherwise rejects
    adding one while any non-reference Clip exists. Returns how many
    Clips were actually converted (0 leaves the project untouched and
    doesn't dirty anything).

    Never loses a Clip's own references: r2v's own per-kind limits
    (generation/api.py's _MAX_REFERENCE_IMAGES/_MAX_REFERENCE_AUDIO/
    _MAX_REFERENCE_VIDEO) are >= every other mode's, so whatever a Clip
    already had under its old mode always still fits. What *does* change
    is meaning, not content: an i2v Clip's own image reference was
    implicitly "the first frame"; after conversion it's just this Clip's
    first r2v reference, addressable as a <Picture N> token in its
    prompt like any other -- existing prompt text isn't rewritten to add
    that token, so a converted Clip may need a manual prompt touch-up to
    actually reference it again.

    Processes Clips in `order` and threads each one's own (possibly just-
    recomputed) width/height/duration into the next as `predecessor`, same
    as recompute_project_resolutions() -- required for continues_previous
    locking to see correct values when the clip immediately before it was
    itself converted earlier in this same pass.
    """
    preset = resolve_preset_for_mode(project.quality_label, Mode.REFERENCE_TO_VIDEO)
    if preset is None or not preset.durations.filter(is_active=True).exists():
        raise PlanError("No active render preset/duration is configured for reference clips.")

    converted = 0
    predecessor: Clip | None = None
    with transaction.atomic():
        for clip in project.clips.order_by("order").select_related("preset", "duration"):
            if clip.mode != Mode.REFERENCE_TO_VIDEO:
                clip.mode = Mode.REFERENCE_TO_VIDEO
                clip.preset = preset
                if clip.continues_previous and predecessor is not None:
                    clip.duration = predecessor.duration
                    clip.width, clip.height = predecessor.width, predecessor.height
                else:
                    clip.duration = (
                        _nearest_duration(preset, clip.duration.duration_seconds)
                        or preset.durations.filter(is_active=True).first()
                    )
                    clip.width, clip.height = compute_resolution(preset.megapixels, project.aspect_ratio)
                clip.save(update_fields=["mode", "preset", "duration", "width", "height"])
                converted += 1
            predecessor = clip
        if converted:
            mark_project_dirty(project)
    return converted


def promote_clip_reference(ref: ClipReferenceAsset) -> ProjectResource:
    """Moves a Clip's own reference (ClipReferenceAsset) up to a
    Project-wide shared resource (ProjectResource) -- e.g. a reference the
    user added to one clip and only afterward realized should be shared
    (a recurring character, a consistent voice) rather than local to that
    one render. The caller (director/api.py's promote_clip_reference())
    has already checked every clip in the project is r2v and that no
    other clip's own reference count would go over its per-kind limit
    once this one joins the shared pool -- same preconditions
    project_resources() POST enforces for a brand-new upload.

    Renumbers every OTHER clip's own reference tokens the same way a
    plain project_resources() POST does (renumber_clip_reference_tokens)
    -- the shared pool for this kind just grew by one, same as if this
    file had been uploaded fresh there. The clip actually losing a
    reference needs different arithmetic (it's simultaneously losing a
    slot from its own numbering while the shared pool gains one, so its
    surviving own-references' tokens shift by a different amount than
    every other clip's, and the promoted token itself isn't a simple
    shift at all -- same underlying file, unrelated new number) -- left
    unrewritten rather than guessed, same "may need a manual prompt
    touch-up" caveat as convert_clips_to_reference() already carries.
    """
    clip = ref.clip
    project = clip.project
    kind = ref.kind
    old_project_count = project.resources.filter(kind=kind).count()

    ref.file.open("rb")
    try:
        content = ref.file.read()
    finally:
        ref.file.close()

    with transaction.atomic():
        resource = ProjectResource(project=project, kind=kind, order=old_project_count)
        resource.file.save(ref.file.name.rsplit("/", 1)[-1], ContentFile(content), save=True)

        ref.file.delete(save=False)
        ref.delete()

        renumber_clip_reference_tokens(project, kind, old_project_count, old_project_count + 1, exclude_clip=clip)
        mark_project_dirty(project)

    return resource


def normalize_planned_scenes(raw_scenes, *, require_reference_mode: bool = False) -> list[dict]:
    """Coerces integrations.llm.plan_scenes()'s raw reply -- or a scene list
    a user has since hand-edited in the preview step -- into the exact
    shape apply_planned_scenes() and the API's response both expect:
    [{"mode": str, "continues_previous": bool, "duration_seconds": float | None,
    "prompt": str, "notes": str}, ...].

    The LLM's JSON is untrusted input, not a contract: silently drops
    entries with no usable prompt and repairs everything else (unknown/
    missing mode falls back to t2v, continues_previous is coerced to False
    whenever the mode can't actually support it) rather than failing the
    whole plan over one malformed scene. Positional continuity (can't
    continue when this is the sequence's very first scene) is enforced by
    apply_planned_scenes() instead, since that depends on whether this is
    appended after existing clips.

    require_reference_mode: True once the target project has shared
    resources (see project_requires_reference_mode()) -- every clip in such
    a project must be r2v, so every scene's mode is forced to r2v here
    regardless of what the LLM said (llm.plan_scenes() is told to write
    r2v-structured prompts and use the project's reference tokens in this
    case, but the mode field itself isn't trusted to come back right).
    """
    scenes = []
    for raw in raw_scenes if isinstance(raw_scenes, list) else []:
        if not isinstance(raw, dict):
            continue
        prompt = str(raw.get("prompt", "")).strip()
        if not prompt:
            continue
        if require_reference_mode:
            mode = Mode.REFERENCE_TO_VIDEO
        else:
            mode = raw.get("mode") if raw.get("mode") in _PLANNABLE_MODES else Mode.TEXT_TO_VIDEO
        raw_duration = raw.get("duration_seconds")
        try:
            duration_seconds = float(raw_duration) if raw_duration is not None else None
        except (TypeError, ValueError):
            duration_seconds = None
        if duration_seconds is not None and duration_seconds <= 0:
            duration_seconds = None
        scenes.append(
            {
                "mode": mode,
                "continues_previous": bool(raw.get("continues_previous")) and mode in CONTINUATION_CAPABLE_MODES,
                "duration_seconds": duration_seconds,
                "prompt": prompt,
                "notes": str(raw.get("notes", "")).strip(),
            }
        )
    return scenes


def normalize_reference_candidates(raw_candidates) -> list[dict]:
    """Coerces integrations.llm.extract_reference_subjects()'s raw reply into
    the shape director/api.py's extract_references() response and the
    frontend's per-candidate "generate" step expect:
    [{"name": str, "kind": "image" | "audio", "description": str}, ...].

    Same posture as normalize_planned_scenes() -- the LLM's JSON is
    untrusted input, not a contract: silently drops an entry with no
    usable description (nothing to generate from) and falls back
    "image" for an unrecognized/missing kind rather than failing the
    whole list over one malformed entry.
    """
    candidates = []
    for raw in raw_candidates if isinstance(raw_candidates, list) else []:
        if not isinstance(raw, dict):
            continue
        description = str(raw.get("description", "")).strip()
        if not description:
            continue
        name = str(raw.get("name", "")).strip() or "Untitled reference"
        kind = raw.get("kind") if raw.get("kind") in ("image", "audio") else "image"
        candidates.append({"name": name, "kind": kind, "description": description})
    return candidates


def apply_planned_scenes(project: Project, scenes, *, replace: bool) -> list[Clip]:
    """Turns a (possibly user-edited) scene list into real Clip rows,
    appended after the project's existing clips by default, or replacing
    them entirely when replace=True. Preset/width/height are resolved from
    the project's own quality_label/aspect_ratio (see
    director/api.py's clips() POST handler, which resolves the same way for
    a manually-created clip). Doesn't itself trigger any render.
    """
    normalized = normalize_planned_scenes(scenes, require_reference_mode=project_requires_reference_mode(project))
    if not normalized:
        raise PlanError("No usable scenes to apply.")

    with transaction.atomic():
        if replace:
            for clip in project.clips.all():
                for ref in clip.references.all():
                    ref.file.delete(save=False)
            project.clips.all().delete()
            predecessor: Clip | None = None
            next_order = 0
        else:
            predecessor = project.clips.order_by("-order").first()
            next_order = 0 if predecessor is None else predecessor.order + 1

        created: list[Clip] = []
        for scene in normalized:
            mode = scene["mode"]
            preset = resolve_preset_for_mode(project.quality_label, mode)
            if preset is None:
                raise PlanError(f"No active render preset/duration is configured for mode {mode!r}.")
            requested_seconds = scene.get("duration_seconds")
            duration = (_nearest_duration(preset, requested_seconds) if requested_seconds else None) or (
                preset.durations.filter(is_active=True).first()
            )
            if duration is None:
                raise PlanError(f"No active render preset/duration is configured for mode {mode!r}.")

            continues_previous = scene["continues_previous"] and predecessor is not None
            if continues_previous:
                # Locked to the predecessor's own width/height/duration --
                # see resolve_clip_width_height()/resolve_clip_duration()'s
                # docstrings on why duration in particular isn't just
                # cosmetic for a continuing clip.
                width, height = predecessor.width, predecessor.height
                duration = predecessor.duration
            else:
                width, height = compute_resolution(preset.megapixels, project.aspect_ratio)

            clip = Clip.objects.create(
                project=project,
                order=next_order,
                continues_previous=continues_previous,
                mode=mode,
                prompt=scene["prompt"],
                preset=preset,
                duration=duration,
                width=width,
                height=height,
            )
            created.append(clip)
            predecessor = clip
            next_order += 1

    return created
