"""SRT → WebVTT, the only subtitle format browsers' <track> understands.

Lenient on input (what real SRT files contain), strict on output:
- CRLF/CR line ends, missing or wrong cue numbers, extra blank lines, a BOM;
- times as ``00:01:02,345``, ``00:01:02.345``, ``1:02:03,4``, ``01:02,345`` (no hours);
  SRT position hints after the time (``X1:… Y1:…``) are dropped;
- tags: <b> <i> <u> are kept (VTT has them), <font …> and ASS overrides ``{\\an8}`` removed;
  a bare ``&`` / ``<`` is escaped; ``-->`` inside text is broken so it cannot start a cue.
Blocks without a valid time line are skipped; a file with no cue at all is an error.
A ``.vtt`` file is passed through (only line ends and encoding normalized).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_TIME = r"(\d{1,3}:)?(\d{1,2}):(\d{1,2})(?:[,.:](\d{1,3}))?"
_TIMING = re.compile(rf"^\s*{_TIME}\s*-+>\s*{_TIME}(?:\s.*)?$")
_ALLOWED_TAG = re.compile(r"</?(?:b|i|u)>", re.IGNORECASE)
_ANY_TAG = re.compile(r"</?[a-zA-Z][^<>]*>")
_ASS_OVERRIDE = re.compile(r"\{\\[^{}]*\}")
_ENTITY = re.compile(r"&(?:[a-zA-Z]+|#\d+|#x[0-9a-fA-F]+);")


class SubtitleFormatError(ValueError):
    pass


@dataclass(frozen=True)
class Converted:
    vtt: str
    cues: int
    skipped_blocks: int


def _ms(hours: str | None, minutes: str, seconds: str, fraction: str | None) -> int:
    h = int(hours[:-1]) if hours else 0
    frac = int((fraction or "0").ljust(3, "0")[:3])
    return ((h * 60 + int(minutes)) * 60 + int(seconds)) * 1000 + frac


def _stamp(ms: int) -> str:
    h, rest = divmod(ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, frac = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{frac:03d}"


def _clean_text(line: str) -> str:
    line = _ASS_OVERRIDE.sub("", line)
    kept: list[str] = []

    def keep(m: re.Match) -> str:
        kept.append(m.group(0).lower())
        return f"\x00{len(kept) - 1}\x00"

    line = _ALLOWED_TAG.sub(keep, line)
    line = _ANY_TAG.sub("", line)  # <font color=…>, </font>, unknown tags
    line = _ENTITY.sub(lambda m: f"\x01{m.group(0)[1:]}", line)  # protect &amp; etc.
    line = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\x01", "&")
    line = re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], line)
    return line.replace("--&gt;", "-&gt;")


def _normalize_newlines(text: str) -> str:
    return text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")


def srt_to_vtt(text: str) -> Converted:
    lines = _normalize_newlines(text).split("\n")
    out = ["WEBVTT", ""]
    cues = skipped = 0
    i = 0
    while i < len(lines):
        if not lines[i].strip():
            i += 1
            continue
        # A block: [number] / time line / text lines until a blank line.
        timing = _TIMING.match(lines[i])
        if not timing and i + 1 < len(lines):
            timing = _TIMING.match(lines[i + 1])
            if timing:
                i += 1
        if not timing:
            skipped += 1
            while i < len(lines) and lines[i].strip():
                i += 1
            continue
        g = timing.groups()
        start, end = _ms(*g[0:4]), _ms(*g[4:8])
        i += 1
        text_lines = []
        while i < len(lines) and lines[i].strip() and not _TIMING.match(lines[i]):
            text_lines.append(_clean_text(lines[i].rstrip()))
            i += 1
        # A time line straight after text (no blank line between cues) starts the next cue;
        # step back over its number line if there is one.
        if i < len(lines) and _TIMING.match(lines[i]) and text_lines and text_lines[-1].strip().isdigit():
            text_lines.pop()
            i -= 1
        text_lines = [t for t in text_lines if t.strip()]
        if end <= start or not text_lines:
            skipped += 1
            continue
        cues += 1
        out += [f"{_stamp(start)} --> {_stamp(end)}", *text_lines, ""]
    if cues == 0:
        raise SubtitleFormatError("no subtitle cue found (is this really an SRT file?)")
    return Converted("\n".join(out), cues, skipped)


def normalize_vtt(text: str) -> Converted:
    text = _normalize_newlines(text)
    first = text.split("\n", 1)[0]
    if not (first == "WEBVTT" or first.startswith(("WEBVTT ", "WEBVTT\t"))):
        # Many ".vtt" files from the web are really SRT with another extension.
        return srt_to_vtt(text)
    cues = sum(1 for line in text.split("\n") if "-->" in line)
    return Converted(text if text.endswith("\n") else text + "\n", cues, 0)
