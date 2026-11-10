"""Subtitle track schema."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SubtitleTrack(BaseModel):
    """One subtitle file next to a video, e.g. ``Phim.vi.srt`` for ``Phim.mp4``."""

    key: str = Field(description="Identifies the track in /subtitles/{id}/{key}.vtt, e.g. 'vi', 'en-forced'.")
    lang: str = Field(description="BCP 47 language for <track srclang>, 'und' when the file name has none.")
    label: str = Field(description="Name to show in the player's subtitle menu.")
    format: Literal["srt", "vtt"]
    file_name: str = Field(description="Name on disk (not sent to clients, see SubtitleTrackOut).")


class SubtitleTrackOut(BaseModel):
    """A subtitle track as the API returns it."""

    key: str
    lang: str
    label: str
    format: Literal["srt", "vtt"] = Field(description="Format of the file on disk; the URL always serves WebVTT.")
    url: str = Field(description="WebVTT for <track src>, e.g. /subtitles/<id>/vi.vtt")
