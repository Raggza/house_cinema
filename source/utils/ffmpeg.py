"""Locate ffmpeg/ffprobe and run them safely (timeout, no console window, UTF-8 output)."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from source.schemas.settings import Settings
from source.utils.paths import bundled_tool

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # Windows: no flashing console


@dataclass(frozen=True)
class FFTools:
    ffmpeg: str | None
    ffprobe: str | None

    @property
    def missing(self) -> list[str]:
        return [name for name, path in (("ffmpeg", self.ffmpeg), ("ffprobe", self.ffprobe)) if not path]


def find_tools(settings: Settings) -> FFTools:
    """ffmpeg: configured path, else the copy shipped in the packaged app, else PATH.
    ffprobe: configured path, else the ffprobe next to the chosen ffmpeg (keeps the two from
    the same build), else PATH."""
    if settings.ffmpeg_path:
        ffmpeg: str | None = str(settings.ffmpeg_path)
    else:
        ffmpeg = bundled_tool("ffmpeg") or shutil.which("ffmpeg")
    if settings.ffprobe_path:
        ffprobe: str | None = str(settings.ffprobe_path)
    else:
        ffprobe = None
        if ffmpeg:
            exe = "ffprobe.exe" if os.name == "nt" else "ffprobe"
            sibling = Path(ffmpeg).with_name(exe)
            if sibling.is_file():
                ffprobe = str(sibling)
        ffprobe = ffprobe or shutil.which("ffprobe")
    return FFTools(ffmpeg=ffmpeg, ffprobe=ffprobe)


INSTALL_HINT = ("Cài ffmpeg (có kèm ffprobe): trong PowerShell chạy `winget install Gyan.FFmpeg`, mở terminal mới "
                "rồi chạy lại hub; hoặc đặt media.ffmpeg_path trong source/configs/config.yml.")


def disabled_features(tools: FFTools) -> list[str]:
    """What the hub cannot do without the missing tool(s), for the person starting it (plan step
    6.1). Empty when ffmpeg and ffprobe are both there. The rest of the hub (library, playing
    the files browsers can play, subtitles, PIN, TV) works without them."""
    features = []
    if not tools.ffprobe:
        features.append("Thông tin video (thời lượng, độ phân giải, codec) — cần ffprobe")
    if not tools.ffmpeg:
        features.append("Ảnh thu nhỏ: video dùng ảnh mặc định — cần ffmpeg")
    if tools.missing:
        features.append("Chuyển đổi video trình duyệt không phát được (MKV trên iPhone, HEVC, tiếng AC-3…) — "
                        "cần ffmpeg và ffprobe")
    if not tools.ffmpeg:
        features.append("Video mẫu của trang kiểm tra thiết bị /diagnostics — cần ffmpeg")
    return features


class ToolError(Exception):
    """ffmpeg/ffprobe failed; the message is a short, readable reason."""


def run_tool(args: list[str], *, timeout: float) -> str:
    """Run a tool and return stdout. Raises ToolError with the first stderr line on failure."""
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=_NO_WINDOW, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"timed out after {timeout:.0f} s") from None
    except OSError as exc:
        raise ToolError(f"cannot run {Path(args[0]).name}: {exc.strerror or exc}") from None
    if result.returncode != 0:
        raise ToolError(_first_error_line(result.stderr) or f"exit code {result.returncode}")
    return result.stdout


def _first_error_line(stderr: str) -> str:
    """ffmpeg prints '[mov,mp4 @ 0x..] moov atom not found' then the path; keep the message."""
    for line in stderr.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("[") and "] " in line:
            line = line.split("] ", 1)[1]
        return line[:200]
    return ""
