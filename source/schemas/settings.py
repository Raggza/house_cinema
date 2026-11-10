"""Validated application settings shared by every module."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from source.utils.paths import data_home

# From source: the project folder. In the packaged app: the per-user data folder
# (see source/utils/paths.py). Relative index/cache paths are relative to it.
PROJECT_ROOT = data_home()
DEFAULT_INDEX_PATH = PROJECT_ROOT / "data" / "library.db"
DEFAULT_CACHE_DIR = PROJECT_ROOT / "cache"
DEFAULT_MEDIA_WORKERS = max(1, min(4, os.cpu_count() or 1))

# Validation context key: library folders already in the index. Such a folder may be
# missing (drive unplugged): it is then kept and its videos shown as unavailable, instead
# of refusing to start. A folder never indexed must exist (it is most likely a typo).
KNOWN_ROOTS_CONTEXT = "known_library_roots"
# Validation context key: library_paths come from the admin page's saved file (step 4.2).
SAVED_PATHS_CONTEXT = "library_paths_saved"

DEFAULT_VIDEO_EXTENSIONS: tuple[str, ...] = (
    ".mp4",
    ".m4v",
    ".webm",
    ".mkv",
    ".avi",
    ".mov",
)

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


def _resolve(path: Path | str) -> Path:
    return Path(os.path.expandvars(str(path))).expanduser().resolve()


def check_library_dir(path: Path) -> Path:
    """Return the resolved directory, or raise ValueError with a readable reason."""
    resolved = _resolve(path)
    if not resolved.exists():
        raise ValueError(f"path does not exist: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"path is not a folder: {resolved}")
    try:
        # os.access() is unreliable on Windows, so actually try to list the folder.
        with os.scandir(resolved) as entries:
            next(entries, None)
    except PermissionError:
        raise ValueError(f"no permission to read folder: {resolved}") from None
    except OSError as exc:
        raise ValueError(f"cannot read folder: {resolved} ({exc.strerror})") from None
    return resolved


def resolve_index_path(value: Path | str) -> Path:
    """Absolute index path; relative paths are relative to the project folder (the per-user
    data folder in the packaged app)."""
    path = Path(os.path.expandvars(str(value))).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


class Settings(BaseModel):
    """Effective configuration after merging config.yml, .env, environment and CLI."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    app_name: str = "house_cinema"
    version: str = "0.1.0"
    app_env: str = "development"
    log_level: LogLevel = "INFO"
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    library_paths: tuple[Path, ...] = ()
    """Library folders; may be empty (folders are then added on the admin page, step 4.2)."""
    library_source: Literal["cli", "saved", "config"] = "config"
    """Where library_paths came from: --path, the admin page's saved file, or config/env."""
    video_extensions: tuple[str, ...] = DEFAULT_VIDEO_EXTENSIONS
    index_path: Path = DEFAULT_INDEX_PATH
    """SQLite library index. A relative path is relative to the project folder."""
    cache_dir: Path = DEFAULT_CACHE_DIR
    """Thumbnails etc. A relative path is relative to the project folder."""
    ffmpeg_path: Path | None = None
    """ffmpeg executable; None = look it up on PATH."""
    ffprobe_path: Path | None = None
    """ffprobe executable; None = next to ffmpeg, else on PATH."""
    media_workers: int = Field(default=DEFAULT_MEDIA_WORKERS, ge=1, le=32)
    """How many files are probed / thumbnailed in parallel."""
    transcode_enabled: bool = True
    """Convert videos a browser cannot play (plan step 5.1); needs ffmpeg."""
    transcode_max_sessions: int = Field(default=2, ge=1, le=16)
    """Conversions at the same time (viewers of converted videos + MKV→MP4 remuxes)."""
    transcode_video_encoder: str = "libx264"
    """ffmpeg H.264 encoder: libx264 (CPU), or h264_nvenc / h264_qsv / h264_amf (GPU)."""
    transcode_max_height: int = Field(default=1080, ge=144, le=4320)
    """Converted video is scaled down to at most this height (lighter for the CPU)."""
    transcode_idle_seconds: int = Field(default=60, ge=5, le=3600)
    """A conversion nobody asked anything of for this long is stopped (tab closed)."""
    transcode_cache_gb: float = Field(default=20.0, ge=0, le=10000)
    """Remuxed copies (MKV→MP4, no quality loss) kept for next time, at most this much."""

    access_pin: str | None = Field(default=None, repr=False)
    """PIN asked by other devices before the library (plan step 5.2); None = no PIN.
    Set in .env (HUB_PIN), never in config.yml."""
    access_lan_only: bool = True
    """Refuse devices with a public (Internet) IP address."""
    pin_max_attempts: int = Field(default=5, ge=1, le=100)
    """Wrong PINs in a row from one device before it is locked out for a while."""
    pin_lockout_minutes: int = Field(default=5, ge=1, le=1440)
    pin_remember_days: int = Field(default=30, ge=1, le=3650)
    """How long a device stays unlocked after a right PIN."""

    @field_validator("access_pin", mode="before")
    @classmethod
    def _check_pin(cls, value: object) -> object:
        if value is None:
            return None
        value = str(value).strip()
        if not value:
            return None
        if not 4 <= len(value) <= 32 or any(c.isspace() for c in value):
            raise ValueError("PIN must be 4 to 32 characters, without spaces")
        return value

    @field_validator("transcode_video_encoder")
    @classmethod
    def _encoder_name(cls, value: str) -> str:
        value = value.strip()
        if not value or not all(c.isalnum() or c == "_" for c in value):
            raise ValueError(f"invalid ffmpeg encoder name: {value!r}")
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("host")
    @classmethod
    def _non_empty_host(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("host must not be empty")
        return value

    @field_validator("library_paths", mode="before")
    @classmethod
    def _paths_as_list(cls, value: object) -> object:
        if isinstance(value, (str, Path)):
            value = [value]
        return () if value is None else value

    @field_validator("index_path")
    @classmethod
    def _resolve_index_path(cls, value: Path) -> Path:
        value = resolve_index_path(value)
        if value.is_dir():
            raise ValueError(f"index_path must be a file, not a folder: {value}")
        return value

    @field_validator("cache_dir")
    @classmethod
    def _resolve_cache_dir(cls, value: Path) -> Path:
        value = resolve_index_path(value)  # same rule: relative to the project folder
        if value.exists() and not value.is_dir():
            raise ValueError(f"cache_dir must be a folder: {value}")
        return value

    @field_validator("ffmpeg_path", "ffprobe_path")
    @classmethod
    def _check_tool(cls, value: Path | None) -> Path | None:
        if value is None or str(value).strip() == "":
            return None
        path = Path(os.path.expandvars(str(value))).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"file not found: {path}")
        return path

    @field_validator("library_paths")
    @classmethod
    def _check_paths(cls, value: tuple[Path, ...], info: ValidationInfo) -> tuple[Path, ...]:
        if (info.context or {}).get(SAVED_PATHS_CONTEXT):
            # Saved from the admin page, where they were checked. A folder that is missing
            # now (drive unplugged, share offline) must not stop the hub from starting: the
            # admin page shows it, the scan marks its videos unavailable.
            return tuple(dict.fromkeys(_resolve(p) if Path(p).exists() else Path(p) for p in value))
        known = {os.path.normcase(str(p)) for p in (info.context or {}).get(KNOWN_ROOTS_CONTEXT, ())}
        resolved: list[Path] = []
        for path in value:
            candidate = _resolve(path)
            if not candidate.exists() and os.path.normcase(str(candidate)) in known:
                directory = candidate  # indexed before, now disconnected: allowed
            else:
                directory = check_library_dir(path)
            if directory not in resolved:
                resolved.append(directory)
        for i, outer in enumerate(resolved):
            for inner in resolved[i + 1 :]:
                if inner.is_relative_to(outer) or outer.is_relative_to(inner):
                    raise ValueError(
                        f"library folders overlap: {outer} and {inner}; "
                        "keep only the parent folder"
                    )
        return tuple(resolved)

    @field_validator("video_extensions", mode="before")
    @classmethod
    def _normalize_extensions(cls, value: object) -> object:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)):
            return value
        normalized: list[str] = []
        for ext in value:
            if not isinstance(ext, str) or not ext.strip(". \t"):
                raise ValueError(f"invalid video extension: {ext!r}")
            ext = "." + ext.strip().lstrip(".").lower()
            if ext not in normalized:
                normalized.append(ext)
        if not normalized:
            raise ValueError("video_extensions must not be empty")
        return tuple(normalized)
