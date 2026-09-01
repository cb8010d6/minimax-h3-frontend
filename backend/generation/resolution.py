"""Aspect-ratio -> pixel-resolution math, replicating the ResolutionSelector
node's own logic (megapixels + aspect ratio -> width/height, rounded to a
multiple) -- see resources/workflows/*.json. We bypass that node entirely in
generation/tasks.py::build_api_workflow() (it only accepts an aspect-ratio
preset + megapixels widget, not arbitrary literal width/height, and we need
to set literal width/height there), so this is our own reimplementation of
"pick pixel dimensions from megapixels + ratio" for the API/frontend layer.

Unlike megapixels (which determines render time -- see RenderPreset), aspect
ratio does not meaningfully affect render time for a fixed pixel count, so
it's kept as a small fixed enum here rather than a DB-backed model: it's
orthogonal to the RenderPreset/RenderDuration catalog, not another axis of
things worth benchmarking.
"""

from __future__ import annotations

import re

# (value, label) pairs, value is what the API/frontend use. Every entry
# except "8:5" mirrors ResolutionSelector's own aspect_ratio combo options
# exactly (see backend/scripts/object_info_cache/ResolutionSelector.json,
# fetched live from ComfyUI) -- harmless to add our own on top since
# build_api_workflow() always overwrites ResolutionSelector's width/height
# with a literal int anyway (see that function's own comment), it never
# actually reads this node's aspect_ratio widget.
# "8:5" (1280x800) exists for Steam Deck custom startup-video export (see
# integrations/media_post.py::to_steam_deck_webm(), generation/api.py's
# steam_deck_export view) -- picking it doesn't guarantee an exact 1280x800
# render (that depends on the chosen preset's megapixels too, rounded to
# RESOLUTION_MULTIPLE), it just keeps the render's aspect close to the
# export's target so that step's scale+letterbox has as little to correct
# for as possible.
ASPECT_RATIOS: list[tuple[str, str]] = [
    ("1:1", "1:1 (Square)"),
    ("2:3", "2:3 (Portrait Photo)"),
    ("3:2", "3:2 (Photo)"),
    ("3:4", "3:4 (Portrait Standard)"),
    ("4:3", "4:3 (Standard)"),
    ("8:5", "8:5 (Steam Deck start video)"),
    ("9:16", "9:16 (Portrait Widescreen)"),
    ("16:9", "16:9 (Widescreen)"),
    ("21:9", "21:9 (Ultrawide)"),
]
ASPECT_RATIO_VALUES = [value for value, _ in ASPECT_RATIOS]
DEFAULT_ASPECT_RATIO = "16:9"

# MiniMaxH3ImageToVideo/ReferenceToVideo's width/height inputs both declare
# step=32 (confirmed live via /object_info) -- round to that so every
# resolution we ever send is guaranteed valid regardless of aspect ratio.
RESOLUTION_MULTIPLE = 32

# A custom "W:H" ratio, distinct from the fixed ASPECT_RATIOS presets above --
# used by the i2v "match uploaded image" option (see
# frontend/src/features/generate/GenerateScreen.tsx's firstFrame handling),
# which computes the actual uploaded first frame's own ratio client-side
# rather than forcing it into the nearest preset. Digits only, each part
# 1-4 digits (matches GenerationJob.aspect_ratio's max_length=10).
_CUSTOM_ASPECT_RATIO_RE = re.compile(r"^\d{1,4}:\d{1,4}$")


def is_valid_aspect_ratio(value: str | None) -> bool:
    """True for one of ASPECT_RATIOS' fixed presets, or a well-formed custom
    "W:H" ratio within a sane range -- guards against a malformed or
    degenerate (near-zero or extreme) value producing a nonsensical
    resolution via compute_resolution() below.
    """
    if value in ASPECT_RATIO_VALUES:
        return True
    if not value or not _CUSTOM_ASPECT_RATIO_RE.match(value):
        return False
    w_ratio, h_ratio = (float(p) for p in value.split(":"))
    return w_ratio > 0 and h_ratio > 0 and 0.1 <= w_ratio / h_ratio <= 10


def compute_resolution(megapixels: float, aspect_ratio: str) -> tuple[int, int]:
    """(megapixels, "W:H") -> (width, height), both multiples of
    RESOLUTION_MULTIPLE, with width/height ratio as close to the requested
    aspect ratio as that rounding allows."""
    w_ratio_str, h_ratio_str = aspect_ratio.split(":")
    w_ratio, h_ratio = float(w_ratio_str), float(h_ratio_str)

    # ComfyUI's ResolutionSelector defines one megapixel as 1024 * 1024
    # pixels. Keep this byte-for-byte equivalent to the installed node so
    # the preview and the submitted workflow can never disagree.
    target_pixels = megapixels * 1024 * 1024
    height = (target_pixels * h_ratio / w_ratio) ** 0.5
    width = height * (w_ratio / h_ratio)

    def round_to_multiple(value: float) -> int:
        return max(RESOLUTION_MULTIPLE, round(value / RESOLUTION_MULTIPLE) * RESOLUTION_MULTIPLE)

    return round_to_multiple(width), round_to_multiple(height)


H3_NATIVE_SHORT_EDGE = 768
H3_NATIVE_MAX_PIXELS = 768 * 1344


def compute_h3_native_resolution(aspect_ratio: str) -> tuple[int, int]:
    """Replicate MiniMax H3's official ``adapt_canvas`` helper.

    The native canvas starts at a 768px short edge, is scaled down when its
    area would exceed 768*1344, then each axis is rounded independently to a
    multiple of 32. This is why 16:9 is exactly 1344x768, while square is
    768x768 and other aspect ratios have their own exact native maximum.
    """
    w_ratio_str, h_ratio_str = aspect_ratio.split(":")
    ratio = float(w_ratio_str) / float(h_ratio_str)
    if ratio >= 1:
        nominal_width = H3_NATIVE_SHORT_EDGE * ratio
        nominal_height = H3_NATIVE_SHORT_EDGE
    else:
        nominal_width = H3_NATIVE_SHORT_EDGE
        nominal_height = H3_NATIVE_SHORT_EDGE / ratio

    area = nominal_width * nominal_height
    if area > H3_NATIVE_MAX_PIXELS:
        scale = (H3_NATIVE_MAX_PIXELS / area) ** 0.5
        nominal_width *= scale
        nominal_height *= scale

    def round_to_multiple(value: float) -> int:
        return max(RESOLUTION_MULTIPLE, round(value / RESOLUTION_MULTIPLE) * RESOLUTION_MULTIPLE)

    return round_to_multiple(nominal_width), round_to_multiple(nominal_height)
