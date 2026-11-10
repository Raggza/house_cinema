"""Fill in video metadata (ffprobe) and thumbnails (ffmpeg) for the indexed items.

Runs after a scan was saved to the index. Only items that need it are processed:
- new or changed files (size/mtime signature differs),
- items processed earlier without ffprobe/ffmpeg, now that the tool is available,
- items whose thumbnail file was deleted from the cache,
- everything, with ``force``.
A broken file, or a missing ffmpeg, never stops the run: the item keeps its place in the
library with a default thumbnail and the reason is logged.
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

from source.image.thumbnails import make_thumbnail, thumb_path, thumbs_dir, use_default_thumbnail
from source.modules.library.index import LibraryIndex
from source.modules.library.probe import probe
from source.modules.library.scanner import locate
from source.schemas.media import MediaInfo, MediaItem
from source.utils.ffmpeg import FFTools, ToolError

logger = logging.getLogger("house_cinema.library.media")

Mode = Literal["full", "thumb"]


@dataclass(frozen=True)
class MediaProgress:
    done: int
    total: int
    current: str


@dataclass
class MediaReport:
    missing_tools: list[str] = field(default_factory=list)
    to_process: int = 0
    up_to_date: int = 0
    read_ok: int = 0
    read_failed: int = 0
    thumbs_created: int = 0
    thumbs_default: int = 0
    thumbs_cached: int = 0
    carried_over: int = 0
    purged: int = 0
    skipped_unavailable: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)
    """(relative path, reason) for files whose info or thumbnail could not be made."""
    thumbs_disabled_reason: str | None = None
    thumbs_folder: str | None = None
    duration_seconds: float = 0.0

    def summary(self) -> str:
        parts = [f"{self.read_ok} read", f"{self.read_failed} failed", f"{self.up_to_date} up to date"]
        thumbs = (f"thumbnails: {self.thumbs_created} created · {self.thumbs_default} default · "
                  f"{self.thumbs_cached} from cache")
        return f"Video info: {' · '.join(parts)}; {thumbs}"


def _source_path(item: MediaItem) -> Path:
    return locate(item.library_root, item.relative_path)


def _file_was_missing(info: MediaInfo) -> bool:
    """A read that failed only because the file could not be found (moved back since, or an
    NFD file name that older versions could not open) is retried on the next scan."""
    return info.status == "failed" and "No such file or directory" in (info.error or "")


def plan_work(item: MediaItem, tools: FFTools, cache_dir: Path, *, force: bool,
              thumbs_enabled: bool) -> Mode | None:
    """What has to be (re)done for one item, or None if it is up to date."""
    info = item.info
    if force or info is None or info.signature != item.signature:
        return "full"
    if info.status == "no-tools" and tools.ffprobe:
        return "full"
    if _file_was_missing(info):
        return "full"
    if not thumbs_enabled:
        return None
    if info.thumbnail is None or not thumb_path(cache_dir, item.id).is_file():
        return "thumb"
    if info.thumbnail == "no-tools" and tools.ffmpeg:
        return "thumb"
    return None


def _process_one(item: MediaItem, mode: Mode, tools: FFTools, cache_dir: Path,
                 thumbs_enabled: bool) -> tuple[MediaInfo, str, str | None]:
    """Runs in a worker thread. Returns (info, thumbnail outcome, problem to report)."""
    source = _source_path(item)
    problem: str | None = None
    previous = item.info

    if mode == "full" or previous is None:
        if not tools.ffprobe:
            fields: dict = {"status": "no-tools", "error": "ffprobe not found"}
        else:
            try:
                r = probe(tools.ffprobe, source)
                fields = {"status": "ok", "duration": r.duration, "width": r.width, "height": r.height,
                          "container": r.container, "video_codec": r.video_codec, "pix_fmt": r.pix_fmt,
                          "audio_codec": r.audio_codec, "browser_playable": r.browser_playable,
                          "playable_note": r.playable_note}
            except ToolError as exc:
                fields = {"status": "failed", "error": str(exc)}
                problem = f"cannot read video: {exc}"
    else:
        fields = previous.model_dump(exclude={"thumbnail", "signature"})

    thumbnail: str | None = None
    if thumbs_enabled:
        target = thumb_path(cache_dir, item.id)
        has_video = fields["status"] == "ok" and fields.get("video_codec")
        try:
            if fields["status"] == "ok" and has_video and tools.ffmpeg:
                try:
                    make_thumbnail(tools.ffmpeg, source, target, fields.get("duration"))
                    thumbnail = "ok"
                except ToolError as exc:
                    use_default_thumbnail(target)
                    thumbnail = "default"
                    problem = problem or f"no thumbnail: {exc}"
            else:
                use_default_thumbnail(target)
                # Remember *why*, so a later run with ffmpeg installed retries only these.
                missing = not tools.ffmpeg or fields["status"] == "no-tools"
                thumbnail = "no-tools" if missing else "default"
        except OSError as exc:
            problem = problem or f"cannot write thumbnail: {exc.strerror or exc}"

    info = MediaInfo(**fields, thumbnail=thumbnail, signature=item.signature)
    return info, thumbnail or "none", problem


def process_media(
    index: LibraryIndex,
    tools: FFTools,
    cache_dir: Path,
    *,
    workers: int = 4,
    renamed: list[tuple[MediaItem, MediaItem]] | None = None,
    force: bool = False,
    on_progress: Callable[[MediaProgress], None] | None = None,
) -> MediaReport:
    started = time.perf_counter()
    report = MediaReport(missing_tools=tools.missing)
    for name in report.missing_tools:
        logger.warning("%s not found: %s. Install ffmpeg or set media.%s_path in config.yml.", name,
                       "video details are not available" if name == "ffprobe" else "thumbnails use a default image",
                       name)

    # Thumbnails: renamed files keep theirs, removed files lose theirs.
    folder = thumbs_dir(cache_dir)
    for old_id, new_id in index.carry_over_renames(renamed or []):
        report.carried_over += 1
        old_thumb, new_thumb = thumb_path(cache_dir, old_id), thumb_path(cache_dir, new_id)
        if old_thumb.is_file():
            os.replace(old_thumb, new_thumb)
    for media_id in index.purge_orphan_info():
        report.purged += 1
        thumb_path(cache_dir, media_id).unlink(missing_ok=True)

    thumbs_enabled = True
    report.thumbs_folder = str(folder.resolve())
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        thumbs_enabled = False
        report.thumbs_disabled_reason = f"cannot create {folder}: {exc.strerror or exc}"
        logger.warning("Thumbnails disabled: %s", report.thumbs_disabled_reason)

    work: list[tuple[MediaItem, Mode]] = []
    for item in index.items():
        if not item.available:
            report.skipped_unavailable += 1
            continue
        mode = plan_work(item, tools, cache_dir, force=force, thumbs_enabled=thumbs_enabled)
        if mode is None:
            report.up_to_date += 1
            if item.info and item.info.thumbnail and thumb_path(cache_dir, item.id).is_file():
                report.thumbs_cached += 1
        else:
            work.append((item, mode))
    report.to_process = len(work)

    if work:
        _run(index, work, tools, cache_dir, thumbs_enabled, workers, report, on_progress)

    report.duration_seconds = round(time.perf_counter() - started, 3)
    if report.thumbs_cached and not report.thumbs_created:
        logger.info("Thumbnails: %d from cache, none regenerated", report.thumbs_cached)
    logger.info("%s in %.2f s", report.summary(), report.duration_seconds)
    return report


def _run(index, work, tools, cache_dir, thumbs_enabled, workers, report, on_progress) -> None:
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="media")
    try:
        futures: dict[Future, tuple[MediaItem, Mode]] = {
            pool.submit(_process_one, item, mode, tools, cache_dir, thumbs_enabled): (item, mode)
            for item, mode in work
        }
        for done, future in enumerate(as_completed(futures), start=1):
            item, mode = futures[future]
            try:
                info, thumb, problem = future.result()
            except OSError as exc:  # e.g. the file vanished after the scan
                info = MediaInfo(status="failed", error=exc.strerror or str(exc), signature=item.signature)
                thumb, problem = "none", f"cannot read file: {exc.strerror or exc}"
            index.save_info(item.id, info)
            if mode == "full":
                if info.status == "ok":
                    report.read_ok += 1
                elif info.status == "failed":
                    report.read_failed += 1
            if thumb == "ok":
                report.thumbs_created += 1
            elif thumb in ("default", "no-tools"):
                report.thumbs_default += 1
            if problem:
                report.failures.append((item.relative_path, problem))
                logger.warning("%s: %s", item.relative_path, problem)
            if on_progress:
                on_progress(MediaProgress(done=done, total=len(work), current=item.name))
    except BaseException:
        # Ctrl+C: drop queued work; what was saved so far stays in the index.
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
