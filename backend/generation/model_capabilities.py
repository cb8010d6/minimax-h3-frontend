"""Authoritative, fail-closed MiniMax H3 model capability registry.

Every user-visible catalog response and every submitted job passes through
this module. Adding a model family or quantization to ModelVariant alone is
therefore insufficient: it must also receive a reviewed capability entry
here, otherwise the API rejects it instead of silently exposing generic
resolution or duration controls.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import CONTENT_TYPE_BY_MODE, ContentType, Mode, ModelVariant, ResolutionPolicy
from .resolution import compute_h3_native_resolution, compute_resolution


class ModelCapabilityError(ValueError):
    pass


@dataclass(frozen=True)
class ModelCapability:
    family: str
    variant: str
    min_duration_seconds: float
    max_duration_seconds: float
    trained_min_frames: int = 124
    trained_max_frames: int = 362
    fps: int = 24


# FP8 and INT8 are quantizations of the same FL2VA/Ref2VA architectures;
# quantization changes memory/performance, not the trained canvas or length.
MODEL_CAPABILITIES: dict[tuple[str, str], ModelCapability] = {
    (family, variant): ModelCapability(
        family=family,
        variant=variant,
        min_duration_seconds=5,
        max_duration_seconds=15,
    )
    for family in ("fl2va", "ref2va")
    for variant in (ModelVariant.FP8, ModelVariant.INT8)
}


def family_for_mode(mode: str) -> str:
    if mode not in Mode.values:
        raise ModelCapabilityError(f"Unknown generation mode: {mode}")
    return "ref2va" if mode in {Mode.REFERENCE_TO_VIDEO, Mode.REFERENCE_TO_IMAGE, Mode.REFERENCE_TO_AUDIO} else "fl2va"


def capability_for(mode: str, variant: str) -> ModelCapability:
    family = family_for_mode(mode)
    try:
        return MODEL_CAPABILITIES[(family, variant)]
    except KeyError as exc:
        raise ModelCapabilityError(
            f"No reviewed capability profile for {family}/{variant}."
        ) from exc


def is_duration_supported(mode: str, duration_seconds: float, capability: ModelCapability) -> bool:
    if CONTENT_TYPE_BY_MODE[mode] == ContentType.IMAGE:
        return duration_seconds == 0
    return capability.min_duration_seconds <= duration_seconds <= capability.max_duration_seconds


def resolve_resolution(
    *, mode: str, megapixels: float, resolution_policy: str, aspect_ratio: str
) -> tuple[int, int]:
    if CONTENT_TYPE_BY_MODE[mode] == ContentType.AUDIO:
        return 32, 32

    native_width, native_height = compute_h3_native_resolution(aspect_ratio)
    if resolution_policy == ResolutionPolicy.H3_NATIVE:
        return native_width, native_height
    if resolution_policy != ResolutionPolicy.FIXED_MEGAPIXELS:
        raise ModelCapabilityError(f"Unknown resolution policy: {resolution_policy}")

    width, height = compute_resolution(megapixels, aspect_ratio)
    if width > native_width or height > native_height:
        raise ModelCapabilityError(
            f"{width}x{height} exceeds the H3 native maximum "
            f"{native_width}x{native_height} for {aspect_ratio}."
        )
    return width, height
