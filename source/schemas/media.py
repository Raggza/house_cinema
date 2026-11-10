"""Schemas for library scan results."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class MediaInfo(BaseModel):
    """Technical details read with ffprobe, plus the thumbnail state (plan step 2.3)."""

    model_config = ConfigDict(frozen=True)

    status: Literal["ok", "failed", "no-tools"]
    """ok = ffprobe read the file; failed = broken/unreadable file; no-tools = ffprobe missing."""
    error: str | None = None
    duration: float | None = None
    """Seconds."""
    width: int | None = None
    height: int | None = None
    container: str | None = None
    """'mp4', 'webm', 'matroska', ... (from the extension and ffprobe's format)."""
    video_codec: str | None = None
    pix_fmt: str | None = None
    audio_codec: str | None = None
    browser_playable: bool | None = None
    """True if common browsers (incl. iOS Safari) play it directly; None if unknown."""
    playable_note: str | None = None
    """Why it is not playable, e.g. 'MKV container (needs remux to MP4)'."""
    thumbnail: Literal["ok", "default", "no-tools"] | None = None
    """ok = generated from the video; default = placeholder image (broken file or no video
    stream); no-tools = placeholder because ffmpeg was missing (retried once it is found);
    None = not made (cache folder not writable)."""
    signature: str
    """'<size>:<mtime>' of the file when this info was made; a change triggers a refresh."""


class MediaItem(BaseModel):
    """One video file found in a library folder."""

    model_config = ConfigDict(frozen=True)

    id: str
    """Stable ID: hash of root_key + relative path. Same file, same ID across scans."""
    root_key: str
    """Identity of the library folder that survives a drive-letter change (see scanner.root_key)."""
    name: str
    """Real file name as on disk, e.g. 'Clip ngắn.MP4'."""
    title: str
    """File name without extension."""
    library_root: str
    """Absolute path of the library folder this file belongs to."""
    relative_path: str
    """Path inside the library root, '/' separated, e.g. 'Phim bộ/Mùa 1/Tập 01.mkv'."""
    folder: str
    """Parent folder inside the library root, '/' separated; '' for files at the root."""
    extension: str
    """Lower-case extension with dot, e.g. '.mp4'."""
    size: int
    """Size in bytes."""
    mtime: float
    """Last modification time, seconds since the epoch."""
    available: bool = True
    """False when the library folder is not connected (e.g. USB drive unplugged)."""
    info: MediaInfo | None = None
    """Metadata and thumbnail state; None until processed (plan step 2.3)."""

    @property
    def signature(self) -> str:
        return f"{self.size}:{self.mtime!r}"


class ScanIssue(BaseModel):
    """A file or folder that was skipped or could not be read."""

    model_config = ConfigDict(frozen=True)

    path: str
    reason: str


class ScanReport(BaseModel):
    roots: list[str]
    root_keys: dict[str, str] = {}
    """root path -> root_key, for roots that were available and scanned."""
    unavailable_roots: list[str] = []
    """Configured roots that were not there (drive unplugged, network share offline)."""
    found: int
    skipped: list[ScanIssue]
    """Left out on purpose: hidden/system entries, links."""
    errors: list[ScanIssue]
    """Could not be read: permission denied, file in use, vanished during the scan."""
    empty_folders: int
    """Folders with no videos and no sub-folders (logged at DEBUG)."""
    duration_seconds: float

    def summary(self) -> str:
        return (f"{self.found} found · {len(self.skipped)} skipped · "
                f"{len(self.errors)} error{'s' if len(self.errors) != 1 else ''}")


class ScanResult(BaseModel):
    items: list[MediaItem]
    report: ScanReport
