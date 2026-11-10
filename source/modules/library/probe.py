"""Read technical details of a video with ffprobe, and decide if browsers can play it.

``browser_playable`` is deliberately conservative: True only for formats that the target
clients of this hub (Chrome/Edge/Firefox on PC, Chrome on Android, Safari on iOS) all play
without help. Anything else is False with a note, and is handled by remux/transcode in
plan step 5.1.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from source.utils.ffmpeg import ToolError, run_tool

PROBE_TIMEOUT_SECONDS = 30

# Containers by file extension; ffprobe reports MKV and WebM both as "matroska,webm".
_CONTAINER_BY_EXT = {
    ".mp4": "mp4", ".m4v": "mp4", ".mov": "mov", ".webm": "webm", ".mkv": "matroska",
    ".avi": "avi", ".wmv": "asf", ".flv": "flv", ".ts": "mpegts", ".m2ts": "mpegts",
}
_PLAYABLE = {
    # container: (video codecs, audio codecs)
    "mp4": ({"h264"}, {"aac", "mp3"}),
    "webm": ({"vp8", "vp9"}, {"opus", "vorbis"}),
}
_CODEC_NOTES = {
    "hevc": "HEVC/H.265 video (most browsers cannot play it)",
    "av1": "AV1 video (older phones and Safari cannot play it)",
    "mpeg4": "MPEG-4 Part 2 / XviD video (browsers cannot play it)",
    "msmpeg4v3": "DivX 3 video (browsers cannot play it)",
    "wmv3": "WMV video (browsers cannot play it)",
    "vc1": "VC-1 video (browsers cannot play it)",
    "mpeg2video": "MPEG-2 video (browsers cannot play it)",
    "ac3": "AC-3 (Dolby Digital) audio",
    "eac3": "E-AC-3 (Dolby Digital Plus) audio",
    "dts": "DTS audio",
    "truehd": "Dolby TrueHD audio",
    "flac": "FLAC audio",
}
_CONTAINER_NOTES = {
    "matroska": "MKV container (needs remux to MP4)",
    "mov": "MOV container (needs remux to MP4)",
    "avi": "AVI container (needs remux or transcode)",
    "asf": "WMV/ASF container (needs transcode)",
    "flv": "FLV container (needs remux)",
    "mpegts": "MPEG-TS container (needs remux)",
}
_SUPPORTED_PIX_FMTS = {"yuv420p", "yuvj420p"}  # 8-bit 4:2:0


@dataclass(frozen=True)
class ProbeResult:
    duration: float | None
    width: int | None
    height: int | None
    container: str
    video_codec: str | None
    pix_fmt: str | None
    audio_codec: str | None
    browser_playable: bool
    playable_note: str | None


def container_for(extension: str, format_name: str) -> str:
    ext = extension.lower()
    if ext in _CONTAINER_BY_EXT:
        expected = _CONTAINER_BY_EXT[ext]
        # A file whose extension lies about its format (".mp4" that is really MKV).
        if expected in ("mp4", "mov") and "mp4" not in format_name and "mov" not in format_name:
            return format_name.split(",")[0] or expected
        if expected in ("webm", "matroska") and "matroska" not in format_name:
            return format_name.split(",")[0] or expected
        return expected
    return format_name.split(",")[0] or ext.lstrip(".")


def playability(container: str, video_codec: str | None, pix_fmt: str | None,
                audio_codec: str | None) -> tuple[bool, str | None]:
    """(playable, note). The note lists every reason, most important first."""
    reasons: list[str] = []
    if container not in _PLAYABLE:
        reasons.append(_CONTAINER_NOTES.get(container, f"{container.upper()} container"))
    videos, audios = _PLAYABLE.get(container, (set().union(*(v for v, _ in _PLAYABLE.values())),
                                               set().union(*(a for _, a in _PLAYABLE.values()))))
    if video_codec is None:
        reasons.append("no video stream")
    elif video_codec not in videos:
        reasons.append(_CODEC_NOTES.get(video_codec, f"{video_codec} video"))
    elif pix_fmt and pix_fmt not in _SUPPORTED_PIX_FMTS:
        depth = "10-bit" if "10" in pix_fmt or "12" in pix_fmt else pix_fmt
        reasons.append(f"{depth} video ({pix_fmt}; browsers need 8-bit 4:2:0)")
    if audio_codec is not None and audio_codec not in audios:
        reasons.append(_CODEC_NOTES.get(audio_codec, f"{audio_codec} audio"))
    return (not reasons, "; ".join(reasons) or None)


def parse_ffprobe(data: dict, extension: str) -> ProbeResult:
    """Turn ffprobe's JSON (-show_format -show_streams) into a ProbeResult."""
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    # Cover art in MKV/MP4 is a "video" stream with disposition.attached_pic: not the movie.
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = _to_float(fmt.get("duration"))
    if duration is None and video is not None:
        duration = _to_float(video.get("duration"))

    container = container_for(extension, fmt.get("format_name") or "")
    video_codec = video.get("codec_name") if video else None
    pix_fmt = video.get("pix_fmt") if video else None
    audio_codec = audio.get("codec_name") if audio else None
    playable, note = playability(container, video_codec, pix_fmt, audio_codec)
    return ProbeResult(
        duration=duration,
        width=_to_int(video.get("width")) if video else None,
        height=_to_int(video.get("height")) if video else None,
        container=container,
        video_codec=video_codec,
        pix_fmt=pix_fmt,
        audio_codec=audio_codec,
        browser_playable=playable,
        playable_note=note,
    )


def probe(ffprobe: str, path: Path | str) -> ProbeResult:
    """Run ffprobe on a file. Raises ToolError with a readable reason if it cannot be read."""
    out = run_tool(
        [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        raise ToolError("ffprobe returned unreadable output") from None
    if not data.get("streams"):
        raise ToolError("no audio or video streams found")
    return parse_ffprobe(data, Path(path).suffix)


def _to_float(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _to_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
