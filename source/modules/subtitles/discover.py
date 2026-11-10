"""Find the subtitle files that belong to a video.

``Phim.mp4`` gets, from the same folder (names compared NFC + case-insensitively):
- ``Phim.srt`` / ``Phim.vtt``                → language 'und' (not given)
- ``Phim.vi.srt``, ``Phim.eng.srt``, ``Phim.Vietnamese.vtt`` → that language
- ``Phim.vi.forced.srt``, ``Phim.en.sdh.srt`` → language + flag in the label
Not taken: ``Phim.part2.srt`` when ``Phim.part2.mp4`` exists (it belongs to that video),
hidden files, and links (the library never follows links, see plan step 3.2).

Looked up on demand (one folder listing), so a subtitle added later shows up without a
rescan.
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from source.modules.library.scanner import _hidden_reason, _is_link
from source.modules.subtitles.languages import FLAGS, parse_language
from source.schemas.subtitle import SubtitleTrack

SUBTITLE_EXTENSIONS = {".srt": "srt", ".vtt": "vtt"}
UNTAGGED_LABEL = "Phụ đề"
_KEY_CHARS = re.compile(r"[^a-z0-9]+")


def _fold(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _track_for(tag: str, fmt: str, file_name: str) -> SubtitleTrack:
    tokens = [t for t in tag.split(".") if t] if tag else []
    lang, label, flags, extra = "und", None, [], []
    for token in tokens:
        parsed = parse_language(token) if lang == "und" else None
        if parsed:
            lang, label = parsed
        elif token.casefold() in FLAGS:
            flags.append(FLAGS[token.casefold()])
        else:
            extra.append(token)
    key = _KEY_CHARS.sub("-", "-".join([lang, *[f.casefold() for f in flags],
                                        *[_fold(e) for e in extra]]).casefold()).strip("-")
    if label is None:
        label = " ".join(extra) if extra else UNTAGGED_LABEL
        extra = []
    label = " ".join([label, *extra]) + (f" ({', '.join(flags)})" if flags else "")
    return SubtitleTrack(key=key or "und", lang=lang, label=label, format=fmt, file_name=file_name)


def find_subtitles(video: Path, video_extensions: Iterable[str]) -> list[SubtitleTrack]:
    """Subtitle tracks for the video at ``video`` (its real on-disk path)."""
    video_exts = {e.casefold() for e in video_extensions}
    stem_nfc = unicodedata.normalize("NFC", os.path.splitext(video.name)[0])
    stem = stem_nfc.casefold()
    other_videos: set[str] = set()
    candidates: list[tuple[str, str, str]] = []  # (folded base name, format, real file name)
    try:
        with os.scandir(video.parent) as entries:
            for entry in entries:
                base, ext = os.path.splitext(entry.name)
                ext = ext.casefold()
                if ext in video_exts:
                    other_videos.add(_fold(base))
                    continue
                if ext not in SUBTITLE_EXTENSIONS:
                    continue
                try:
                    if _is_link(entry) or _hidden_reason(entry) or not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                candidates.append((_fold(base), SUBTITLE_EXTENSIONS[ext], entry.name))
    except OSError:
        return []
    other_videos.discard(stem)

    tracks: list[SubtitleTrack] = []
    for base, fmt, file_name in candidates:
        if base == stem:
            tag = ""
        elif base.startswith(stem + ".") and base not in other_videos:
            # Keep the original spelling of the tag for labels ("Phim.Vietnamese.srt").
            original = unicodedata.normalize("NFC", os.path.splitext(file_name)[0])
            # (casefold can change the length, e.g. 'ß' → 'ss': cut by the NFC stem instead.)
            tag = original[len(stem_nfc) + 1:]
        else:
            continue
        tracks.append(_track_for(tag, fmt, file_name))

    # Stable order, VTT before SRT for the same language; duplicates get -2, -3…
    tracks.sort(key=lambda t: (t.lang == "und", t.key, t.format != "vtt", _fold(t.file_name)))
    seen: Counter[str] = Counter()
    for track in tracks:
        seen[track.key] += 1
        if seen[track.key] > 1:
            track.key = f"{track.key}-{seen[track.key]}"
    return tracks
