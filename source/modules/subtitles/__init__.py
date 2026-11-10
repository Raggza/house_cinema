"""Subtitles next to the videos: discovery, decoding and conversion to WebVTT."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from source.modules.subtitles.convert import SubtitleFormatError, normalize_vtt, srt_to_vtt
from source.modules.subtitles.discover import SUBTITLE_EXTENSIONS, find_subtitles
from source.modules.subtitles.encoding import SubtitleEncodingError, decode_subtitle

MAX_SUBTITLE_BYTES = 10 * 1024 * 1024  # a 2-hour film's SRT is ~100 KB; refuse absurd files


class SubtitleError(ValueError):
    """The subtitle file exists but cannot be served; the message says why (for the log)."""


@dataclass(frozen=True)
class WebVtt:
    text: str
    source_encoding: str
    cues: int
    skipped_blocks: int


def load_as_vtt(path: Path, fmt: str) -> WebVtt:
    """Read a .srt/.vtt file and return it as UTF-8 WebVTT text. Raises SubtitleError."""
    try:
        size = path.stat().st_size
        if size > MAX_SUBTITLE_BYTES:
            raise SubtitleError(f"file too large for a subtitle ({size:,} bytes)")
        data = path.read_bytes()
    except OSError as exc:
        raise SubtitleError(f"cannot read: {exc.strerror or exc}") from None
    try:
        decoded = decode_subtitle(data)
        converted = srt_to_vtt(decoded.text) if fmt == "srt" else normalize_vtt(decoded.text)
    except (SubtitleEncodingError, SubtitleFormatError) as exc:
        raise SubtitleError(str(exc)) from None
    return WebVtt(converted.vtt, decoded.encoding, converted.cues, converted.skipped_blocks)


__all__ = ["MAX_SUBTITLE_BYTES", "SUBTITLE_EXTENSIONS", "SubtitleError", "WebVtt",
           "find_subtitles", "load_as_vtt"]
