"""Video thumbnails made with ffmpeg, stored as <cache_dir>/thumbs/<media id>.jpg."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from source.utils.ffmpeg import ToolError, run_tool

THUMB_WIDTH = 320
THUMB_TIMEOUT_SECONDS = 60
DEFAULT_THUMBNAIL = Path(__file__).resolve().parent / "assets" / "no-thumbnail.jpg"


def thumbs_dir(cache_dir: Path) -> Path:
    return Path(cache_dir) / "thumbs"


def thumb_path(cache_dir: Path, media_id: str) -> Path:
    return thumbs_dir(cache_dir) / f"{media_id}.jpg"


def seek_time(duration: float | None) -> float:
    """Where to grab the frame: 10% in (skips logos and black intros), at most 5 minutes."""
    if duration is None:
        return 1.0
    if duration < 2:
        return duration / 2
    return min(duration * 0.1, 300.0)


def make_thumbnail(ffmpeg: str, source: Path | str, target: Path, duration: float | None) -> None:
    """Write a JPEG thumbnail to ``target``. Raises ToolError if no frame could be produced.

    The image is written to a temporary file and then renamed, so a crash never leaves a
    half-written thumbnail in the cache.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.stem}.tmp{os.getpid()}.jpg")
    # "thumbnail" picks the most representative of the next 30 frames (avoids fades/black).
    vf = f"thumbnail=30,scale={THUMB_WIDTH}:-2"
    attempts = [seek_time(duration)]
    if attempts[0] > 0:
        attempts.append(0.0)  # seek past the end, or a broken index: try the first frame
    last_error = "no frame produced"
    try:
        for at in attempts:
            tmp.unlink(missing_ok=True)
            try:
                run_tool([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{at:.3f}",
                          "-i", str(source), "-map", "0:v:0", "-vf", vf, "-frames:v", "1",
                          "-q:v", "4", str(tmp)], timeout=THUMB_TIMEOUT_SECONDS)
            except ToolError as exc:
                last_error = str(exc)
                continue
            if tmp.is_file() and tmp.stat().st_size > 0:
                os.replace(tmp, target)
                return
        raise ToolError(last_error)
    finally:
        tmp.unlink(missing_ok=True)


def use_default_thumbnail(target: Path) -> None:
    """Copy the placeholder image so every video has <id>.jpg in the cache."""
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(DEFAULT_THUMBNAIL, target)
