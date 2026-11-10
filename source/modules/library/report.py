"""Human-readable output for a library scan (used by `python -m source.pipeline scan`)."""

from __future__ import annotations

import os
from datetime import datetime
from itertools import groupby

from source.modules.library.index import IndexDiff, RootInfo
from source.modules.library.media_info import MediaProgress, MediaReport
from source.modules.library.scanner import ScanProgress, locate
from source.schemas.media import MediaItem, ScanResult
from source.schemas.subtitle import SubtitleTrack


def format_progress(progress: ScanProgress) -> str:
    """Text for the terminal progress line (the current folder is passed separately as tail)."""
    text = f"Scanning… {progress.videos_found} videos · {progress.folders_scanned} folders"
    if progress.errors:
        text += f" · {progress.errors} error{'s' if progress.errors != 1 else ''}"
    return text


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"  # pragma: no cover


def _short_path(path: str, roots: list[str]) -> str:
    """Show a path relative to its library root when it is inside one."""
    for root in roots:
        try:
            rel = os.path.relpath(path, root)
        except ValueError:  # different drive on Windows
            continue
        if not rel.startswith(".."):
            return rel
    return path


NOT_CONNECTED = "not connected"


def _format_items(items: list[MediaItem], roots: list[str], root_notes: dict[str, str]) -> list[str]:
    """Items grouped by library root and folder; roots without items are listed too."""
    lines: list[str] = []
    width = max((len(i.name) for i in items), default=0)
    by_root = {root: [i for i in items if i.library_root == root] for root in roots}
    for root in roots:
        note = f"  [{root_notes[root]}]" if root in root_notes else ""
        lines.append(f"Library: {root}{note}")
        if not by_root[root]:
            lines.append("  (no videos)")
        for folder, group in groupby(by_root[root], key=lambda i: i.folder):
            lines.append(f"  {folder or '(root)'}/")
            for item in group:
                lines.append(f"    {item.name.ljust(width)}  {human_size(item.size):>9}  {item.id}")
        lines.append("")
    return lines


def format_scan(result: ScanResult) -> str:
    roots = result.report.roots
    notes = {r: NOT_CONNECTED for r in result.report.unavailable_roots}
    lines = _format_items(result.items, roots, notes)

    if result.report.skipped:
        lines.append("Skipped:")
        lines.extend(f"  {_short_path(s.path, roots)}  — {s.reason}" for s in result.report.skipped)
        lines.append("")
    if result.report.errors:
        lines.append("Errors:")
        lines.extend(f"  {_short_path(e.path, roots)}  — {e.reason}" for e in result.report.errors)
        lines.append("")

    lines.append(f"{result.report.summary()}  ({result.report.duration_seconds:.2f} s)")
    return "\n".join(lines)


def format_index(items: list[MediaItem], roots: list[RootInfo], index_path: str) -> str:
    """The `list` command: what the index holds, without scanning."""
    if not roots:
        return f"Index {index_path} is empty. Run: python -m source.pipeline scan"
    notes = {}
    for r in roots:
        notes[r.path] = (f"{NOT_CONNECTED}, {r.items} videos unavailable" if not r.available
                         else f"last scan {_when(r.last_scan)}")
    lines = _format_items(items, [r.path for r in roots], notes)
    available = sum(1 for i in items if i.available)
    lines.append(f"{len(items)} videos in index · {available} available · "
                 f"{len(items) - available} unavailable  ({index_path})")
    return "\n".join(lines)


def _when(timestamp: float | None) -> str:
    if timestamp is None:
        return "never"
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    total = int(round(seconds))
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def format_media_progress(progress: MediaProgress) -> str:
    return f"Reading video info… {progress.done}/{progress.total}"


def format_media_report(report: MediaReport, *, limit: int = 20) -> str:
    lines = [report.summary() + f"  ({report.duration_seconds:.2f} s)"]
    if report.missing_tools:
        lines.append(f"  ! not found: {', '.join(report.missing_tools)} — install ffmpeg or set "
                     "media.ffmpeg_path / media.ffprobe_path in config.yml")
    if report.thumbs_disabled_reason:
        lines.append(f"  ! thumbnails disabled: {report.thumbs_disabled_reason}")
    elif report.thumbs_folder:
        lines.append(f"  thumbnails saved in: {report.thumbs_folder}  (<id>.jpg; see `info <id>`)")
    if report.skipped_unavailable:
        lines.append(f"  ({report.skipped_unavailable} videos on disconnected folders skipped)")
    for path, problem in report.failures[:limit]:
        lines.append(f"  ! {path} — {problem}")
    if len(report.failures) > limit:
        lines.append(f"    … and {len(report.failures) - limit} more")
    return "\n".join(lines)


def format_info(item: MediaItem, thumbnail_file: str | None,
                subtitles: list[SubtitleTrack] | None = None) -> str:
    """The `info <id>` command. ``subtitles`` None = not looked up (folder not connected)."""
    info = item.info
    rows: list[tuple[str, str]] = [
        ("id", item.id),
        ("title", item.title),
        ("file", str(locate(item.library_root, item.relative_path)) if item.available
         else os.path.join(item.library_root, *item.relative_path.split("/"))),
        ("available", "yes" if item.available else f"no ({NOT_CONNECTED})"),
        ("size", f"{human_size(item.size)} ({item.size:,} bytes)"),
        ("modified", _when(item.mtime)),
    ]
    if info is None:
        rows.append(("video info", "not read yet (run: python -m source.pipeline scan)"))
    elif info.status == "no-tools":
        rows.append(("video info", "not available: ffprobe not found"))
    elif info.status == "failed":
        rows.append(("video info", f"cannot read this file: {info.error}"))
    else:
        resolution = f"{info.width}×{info.height}" if info.width and info.height else "unknown"
        video = info.video_codec or "none"
        if info.pix_fmt:
            video += f" ({info.pix_fmt})"
        rows += [
            ("duration", format_duration(info.duration)),
            ("resolution", resolution),
            ("container", info.container or "unknown"),
            ("video", video),
            ("audio", info.audio_codec or "none"),
            ("browser", "plays directly" if info.browser_playable
             else f"needs conversion: {info.playable_note}"),
        ]
    if info is not None:
        state = {"ok": "from the video", "default": "default image",
                 "no-tools": "default image (ffmpeg not found)", None: "none"}[info.thumbnail]
        rows.append(("thumbnail", f"{state}" + (f" — {thumbnail_file}" if thumbnail_file else "")))
    if subtitles is not None:
        if not subtitles:
            rows.append(("subtitles", "none (put e.g. <video name>.vi.srt next to the video)"))
        for n, track in enumerate(subtitles):
            rows.append(("subtitles" if n == 0 else "",
                         f"{track.label} [{track.lang}] — {track.file_name}  (/subtitles/{item.id}/{track.key}.vtt)"))
    width = max(len(k) for k, _ in rows)
    return "\n".join(f"  {k.ljust(width)} | {v}" for k, v in rows)


def format_diff(diff: IndexDiff, *, dry_run: bool = False, limit: int = 20) -> str:
    """Changes since the previous scan, at most ``limit`` lines per kind."""
    title = "Would change" if dry_run else "Changes since last scan"
    if diff.first_scan:
        verb = "would be indexed" if dry_run else "indexed"
        return f"First scan: {len(diff.added)} videos {verb}."
    if diff.is_empty():
        return f"{title}: none."
    lines = [f"{title}: {diff.summary()}"]

    def section(symbol: str, entries: list[str]) -> None:
        for text in entries[:limit]:
            lines.append(f"  {symbol} {text}")
        if len(entries) > limit:
            lines.append(f"    … and {len(entries) - limit} more")

    section("+", [i.relative_path for i in diff.added])
    section("−", [i.relative_path for i in diff.removed])
    section("~", [f"{old.relative_path} → {new.relative_path}" for old, new in diff.renamed])
    section("*", [f"{i.relative_path} (file changed)" for i in diff.changed])
    section("!", [f"{root} — {NOT_CONNECTED}, {n} videos kept as unavailable" for root, n in diff.now_unavailable])
    section("↺", [f"{root} — back online, {n} videos available again" for root, n in diff.restored])
    section("×", [f"{root} — no longer in config, {n} videos dropped from index" for root, n in diff.dropped_roots])
    if diff.kept_after_errors:
        lines.append(f"  (kept {diff.kept_after_errors} indexed videos that could not be read this time)")
    return "\n".join(lines)
