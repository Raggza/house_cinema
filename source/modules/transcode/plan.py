"""Decide how a video reaches a browser that cannot play the file as it is (plan step 5.1).

The browser says what it can decode (``ClientCaps``, measured with ``canPlayType`` /
``MediaSource.isTypeSupported`` for MP4). Then:

- **remux**: the picture can be kept as it is → it is copied into MP4 pieces (HLS, fMP4);
  only the sound is converted to AAC when the browser cannot play it (AC-3, DTS…). Cheap:
  limited by the disk, not the CPU; no quality loss. The result is kept in the cache.
- **transcode**: the picture must be converted (HEVC, AV1, 10-bit, MPEG-4…) → H.264 + AAC,
  made piece by piece while watching, restarted where the viewer seeks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from source.schemas.media import MediaInfo

Mode = Literal["remux", "transcode"]

SEGMENT_SECONDS = 6
"""Length of one piece; the transcode grid (piece n starts at n × 6 s) relies on it."""

# Codec names in MSE / HLS ``codecs=`` strings (generic profiles: browsers check support from
# these and take the exact profile from the stream itself).
CODEC_STRINGS = {
    "h264": "avc1.640028", "hevc": "hvc1.1.6.L120.90", "av1": "av01.0.08M.08", "vp9": "vp09.00.40.08",
    "aac": "mp4a.40.2", "mp3": "mp4a.40.34", "ac3": "ac-3", "eac3": "ec-3", "opus": "opus", "flac": "flac",
}
# Video codecs that may be copied into MP4 when the browser decodes them.
COPYABLE_VIDEO = {"h264", "hevc", "av1", "vp9"}
COPYABLE_AUDIO = {"aac", "mp3", "ac3", "eac3", "opus", "flac"}
EIGHT_BIT_420 = {"yuv420p", "yuvj420p"}


@dataclass(frozen=True)
class ClientCaps:
    video: frozenset[str]
    audio: frozenset[str]


@dataclass(frozen=True)
class Decision:
    mode: Mode
    copy_video: bool
    copy_audio: bool
    has_audio: bool
    mime: str
    """MP4 type of the result, for MediaSource.addSourceBuffer."""
    reason: str
    """Why, in Vietnamese, for the player page."""


class CannotConvert(Exception):
    """Conversion is not possible for this video; the message is shown to the user."""


def _video_label(info: MediaInfo) -> str:
    codec = (info.video_codec or "?").upper()
    if info.pix_fmt and info.pix_fmt not in EIGHT_BIT_420 and info.video_codec == "h264":
        return f"{codec} {info.pix_fmt}"
    return codec


def decide(info: MediaInfo | None, caps: ClientCaps) -> Decision:
    if info is None or info.status != "ok":
        raise CannotConvert("Hub chưa đọc được thông tin của video này (cần quét lại, hoặc file bị hỏng).")
    if not info.video_codec:
        raise CannotConvert("File không có hình (chỉ có âm thanh).")
    audio = info.audio_codec
    copy_video = (info.video_codec in COPYABLE_VIDEO and info.video_codec in caps.video
                  and (info.video_codec != "h264" or (info.pix_fmt or "yuv420p") in EIGHT_BIT_420))
    copy_audio = audio is None or (audio in COPYABLE_AUDIO and audio in caps.audio)
    if copy_video:
        mode: Mode = "remux"
        video_out = info.video_codec
        what = []
        if info.container not in ("mp4", "webm"):
            what.append(f"đổi vỏ {info.container or 'file'} sang MP4")
        if not copy_audio:
            what.append(f"chuyển âm thanh {audio.upper()} sang AAC")
        reason = "Đang " + (" và ".join(what) or "đóng gói lại video") + " (giữ nguyên chất lượng hình)."
    else:
        if not info.duration:
            raise CannotConvert("Không biết thời lượng của video nên không chuyển đổi được.")
        mode, video_out, copy_audio = "transcode", "h264", False
        reason = f"Trình duyệt này không giải mã được hình {_video_label(info)}: hub đang chuyển sang H.264."
    codecs = [CODEC_STRINGS[video_out]]
    if audio is not None:
        codecs.append(CODEC_STRINGS[audio] if copy_audio else CODEC_STRINGS["aac"])
    return Decision(mode=mode, copy_video=copy_video, copy_audio=copy_audio, has_audio=audio is not None,
                    mime=f'video/mp4; codecs="{",".join(codecs)}"', reason=reason)
