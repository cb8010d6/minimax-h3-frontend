"""ffmpeg assembly of a Director project's ordered clip videos into one
downloadable video -- backs "Export" / POST .../assemble/ (see
director/api.py's assemble_project). The plain concat *demuxer* (stream
copy, no re-encode) requires every input to share identical codec
parameters, which Director doesn't guarantee -- a "fresh scene" clip can
have a different aspect ratio/resolution than its neighbours (see
Clip.aspect_ratio) -- so this always goes through the concat *filter*
instead, scaling/padding every clip onto a common canvas first. Mirrors
media_post.py's _run_ffmpeg shape but needs N inputs instead of one, so
it's its own small runner rather than reusing that one.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from integrations import media_post

FFMPEG_TIMEOUT = 600


class AssemblyError(RuntimeError):
    pass


def _probe_video(path: Path) -> tuple[int, int, str]:
    """Return video metadata using the backend's bundled user-space ffmpeg.

    imageio-ffmpeg yields metadata before it decodes the first frame, so this
    retains ffprobe-like cost without requiring a separately installed
    ffprobe binary on the host.
    """
    reader = None
    try:
        from imageio_ffmpeg import read_frames

        reader = read_frames(str(path), pix_fmt="rgb24")
        metadata = next(reader)
        width, height = metadata.get("source_size") or metadata.get("size") or (0, 0)
        fps = metadata.get("fps") or 30.0
        if not width or not height:
            raise ValueError("no video dimensions")
        return int(width), int(height), str(fps)
    except (ImportError, OSError, RuntimeError, StopIteration, TypeError, ValueError) as exc:
        raise AssemblyError(f"Could not inspect video clip {path.name}: {exc}") from exc
    finally:
        if reader is not None:
            reader.close()


def concat_videos(video_paths: list[Path]) -> bytes:
    """Concatenates video_paths, in order, into one MP4 (H.264/AAC) and
    returns its bytes. Every clip is scaled to fit (letterboxed, aspect
    preserved) onto the largest clip's resolution and normalized to the
    first clip's frame rate before concatenation -- Director allows each
    "fresh scene" clip its own aspect ratio, which the concat *filter*
    (unlike the stream-copy concat demuxer) tolerates by design as long as
    every input is normalized to the same canvas/frame rate first.
    """
    if not video_paths:
        raise AssemblyError("No clips to assemble.")

    dims = [_probe_video(p) for p in video_paths]
    target_width = max(w for w, _, _ in dims) or 1280
    target_height = max(h for _, h, _ in dims) or 720
    # Even dimensions -- libx264's yuv420p chroma subsampling requires it.
    target_width += target_width % 2
    target_height += target_height % 2
    target_fps = dims[0][2]

    clip_count = len(video_paths)
    filter_parts = [
        f"[{i}:v]scale={target_width}:{target_height}:force_original_aspect_ratio=decrease,"
        f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={target_fps}[v{i}]"
        for i in range(clip_count)
    ]
    concat_inputs = "".join(f"[v{i}][{i}:a]" for i in range(clip_count))
    filter_complex = ";".join(filter_parts) + f";{concat_inputs}concat=n={clip_count}:v=1:a=1[outv][outa]"

    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "assembled.mp4"
        try:
            ffmpeg_executable = media_post._ffmpeg_executable()
        except media_post.FfmpegError as exc:
            raise AssemblyError(str(exc)) from exc
        args = [ffmpeg_executable, "-y"]
        for path in video_paths:
            args += ["-i", str(path)]
        args += [
            "-filter_complex", filter_complex,
            "-map", "[outv]",
            "-map", "[outa]",
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "192k",
            str(out_path),
        ]
        try:
            result = subprocess.run(args, capture_output=True, timeout=FFMPEG_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            raise AssemblyError(f"ffmpeg timed out after {FFMPEG_TIMEOUT}s") from exc
        except OSError as exc:
            raise AssemblyError(f"ffmpeg could not start: {exc}") from exc
        if result.returncode != 0 or not out_path.exists():
            raise AssemblyError(f"ffmpeg failed: {result.stderr.decode(errors='replace')[-2000:]}")
        return out_path.read_bytes()
