"""MiniMax H3 reference-token numbering and validation."""

from __future__ import annotations

import re

_REFERENCE_TOKEN_RE = re.compile(r"<(Picture|Video|Audio)\s+([^<>]+)>")


def invalid_reference_tokens(
    prompt: str, *, image_count: int, video_count: int, audio_count: int
) -> list[str]:
    """Return malformed or out-of-range H3 reference tags in ``prompt``.

    Reference-video soundtracks consume the first Audio ordinals; standalone
    audio starts at ``video_count + 1``. The total valid Audio range is thus
    ``1..(video_count + audio_count)``.
    """
    limits = {
        "Picture": image_count,
        "Video": video_count,
        "Audio": video_count + audio_count,
    }
    invalid: list[str] = []
    for match in _REFERENCE_TOKEN_RE.finditer(prompt):
        token = match.group(0)
        ordinal_text = match.group(2).strip()
        if not ordinal_text.isdigit():
            invalid.append(token)
            continue
        ordinal = int(ordinal_text)
        if ordinal < 1 or ordinal > limits[match.group(1)]:
            invalid.append(token)
    return list(dict.fromkeys(invalid))


def expected_primary_reference_tokens(
    *, image_count: int, video_count: int, audio_count: int
) -> list[str]:
    """Canonical tags users should normally mention for uploaded media.

    Video soundtrack Audio tags are optional, so only standalone audio tags
    are included in the reminder set.
    """
    pictures = [f"<Picture {index}>" for index in range(1, image_count + 1)]
    videos = [f"<Video {index}>" for index in range(1, video_count + 1)]
    audios = [
        f"<Audio {index}>"
        for index in range(video_count + 1, video_count + audio_count + 1)
    ]
    return pictures + videos + audios
