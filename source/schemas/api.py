"""Schemas of the JSON API (plan step 3.4). Clients (the web UI of Phase 4) rely on them:
change fields only together with test/test_api.py's contract tests.

No server path is ever sent: folders are relative to their library, libraries are named by
their folder name, URLs are relative to the hub.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

from source.schemas.subtitle import SubtitleTrackOut


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LibraryRef(_Strict):
    id: str = Field(description="Short stable ID of a library folder.")
    name: str = Field(description="Folder name of the library, e.g. 'Movies'.")
    available: bool = Field(description="False while its drive/share is disconnected.")


class MediaSummary(_Strict):
    """One video in a list."""

    id: str
    title: str = Field(description="File name without extension.")
    name: str = Field(description="File name as on disk, e.g. 'Clip ngắn.MP4'.")
    library: LibraryRef
    folder: str = Field(description="Folder inside the library, '/' separated; '' at its top.")
    extension: str
    size: int = Field(description="Bytes.")
    modified: datetime
    duration: float | None = Field(description="Seconds; null until read (or unreadable).")
    width: int | None
    height: int | None
    available: bool = Field(description="False while the library folder is disconnected.")
    browser_playable: bool | None = Field(
        description="True: plays in browsers as is; false: needs conversion; null: unknown.")
    thumbnail_url: str = Field(description="Always usable: a default image until one is made.")
    stream_url: str
    detail_url: str


class MediaDetail(MediaSummary):
    """One video with everything known about it."""

    info_status: Literal["ok", "failed", "no-tools", "pending"] = Field(
        description="ok: details read; failed: file unreadable; no-tools: ffprobe missing; "
                    "pending: not read yet (next rescan).")
    info_error: str | None = Field(description="Why the file could not be read (failed).")
    container: str | None
    video_codec: str | None
    pix_fmt: str | None
    audio_codec: str | None
    playable_note: str | None = Field(description="Why it does not play in browsers as is.")
    subtitles: list[SubtitleTrackOut] = Field(
        description="Subtitle tracks next to the video; empty when none or disconnected.")


class LibraryPage(_Strict):
    items: list[MediaSummary]
    total: int = Field(description="Matching videos in all pages.")
    offset: int
    limit: int
    next_offset: int | None = Field(description="offset of the next page; null on the last page.")


class FolderInfo(_Strict):
    library: LibraryRef
    folder: str = Field(description="'' for the top of the library.")
    videos: int = Field(description="Videos directly in this folder.")
    total_videos: int = Field(description="Videos in this folder and all folders below it.")


class FolderList(_Strict):
    libraries: list[LibraryRef]
    folders: list[FolderInfo] = Field(description="Every folder that holds videos, and their parents.")


class RescanCounts(_Strict):
    found: int
    added: int
    removed: int
    renamed: int
    changed: int
    unavailable_libraries: int
    errors: int = Field(description="Folders/files that could not be read during the scan.")
    info_read: int = Field(description="Videos whose details were read (new or changed).")
    info_failed: int
    thumbnails_created: int


class HubInfo(_Strict):
    """What the web page needs to know about the hub and about this device."""

    version: str
    is_server: bool = Field(description="True when the page is open on the server computer itself "
                                        "(only then the admin page is reachable).")
    folders: int = Field(description="Library folders configured (0: none yet, see the admin page).")
    pin: bool = Field(False, description="Other devices need the hub's PIN (step 5.2).")


class PinIn(_Strict):
    pin: str = Field(max_length=64)


class AdminFolder(_Strict):
    path: str = Field(description="Full path on the server (admin API only: never sent to other devices).")
    exists: bool
    problem: str | None = Field(description="Why it cannot be scanned now (e.g. drive unplugged).")
    videos: int = Field(description="Videos of this folder in the index (0 until scanned).")


class AdminFolders(_Strict):
    folders: list[AdminFolder]
    source: Literal["cli", "saved", "config"] = Field(
        description="cli: given with --path for this run (not editable); saved: set on this page; "
                    "config: from config.yml/.env until the first save.")
    editable: bool
    store_file: str = Field(description="Where the list is saved.")


class AdminFoldersUpdate(_Strict):
    paths: list[str] = Field(max_length=100, description="The whole new list (full paths).")


class FolderProblem(_Strict):
    path: str
    message: str


class AdminFoldersRejected(_Strict):
    detail: str
    problems: list[FolderProblem]


class DiagnosticSample(_Strict):
    key: str
    label: str
    mime: str = Field(description="Type with codecs, for canPlayType().")
    url: str
    seconds: int
    must_play: bool = Field(description="The plan's minimum: every device must play it (MP4 H.264).")


class DiagnosticSamples(_Strict):
    samples: list[DiagnosticSample]
    subtitles_url: str
    missing: dict[str, str] = Field(description="Formats left out (could not be made), and why.")


class CheckResult(_Strict):
    ok: bool | None = Field(description="null: not tested / not applicable.")
    detail: str = Field("", max_length=500)


class FormatResult(_Strict):
    key: str = Field(max_length=40)
    can_play_type: str = Field(max_length=20, description="The browser's own answer: '', 'maybe', 'probably'.")
    plays: bool | None = Field(description="Time advanced while playing it.")
    picture: bool | None = Field(description="A picture was decoded (not sound only).")
    sound: bool | None = Field(None, description="The sound was decoded; null: the browser cannot tell.")
    detail: str = Field("", max_length=300)


RemoteButton = Literal["up", "down", "left", "right", "ok", "back", "play", "pause", "stop", "rewind",
                       "fast_forward"]
KeyVia = Literal["keydown", "keyup", "popstate", "click"]


class RemoteKeyResult(_Strict):
    """What the browser received when the tester pressed one remote button (plan step 5.4)."""

    button: RemoteButton
    received: bool | None = Field(description="false: nothing arrived in time; null: skipped by the tester.")
    via: KeyVia | None = Field(None, description="keydown/keyup: a key event; popstate: the browser went back "
                                                 "in its history (Back); click: a pointer click (cursor mode).")
    key: str = Field("", max_length=40, description="KeyboardEvent.key, e.g. 'ArrowUp', 'MediaPlayPause'.")
    code: str = Field("", max_length=40, description="KeyboardEvent.code.")
    key_code: int = Field(0, ge=0, le=100000, description="KeyboardEvent.keyCode (old TV browsers rely on it).")


class RemoteKeyEvent(_Strict):
    """One raw event seen during the remote test (also the ones not tied to a button)."""

    type: KeyVia
    key: str = Field("", max_length=40)
    code: str = Field("", max_length=40)
    key_code: int = Field(0, ge=0, le=100000)
    ms: int = Field(0, ge=0, le=3_600_000, description="Time since the test started.")


class DeviceReport(_Strict):
    """What a device's test run found (plan step 4.4). Sent by /diagnostics.

    A remote-key test (plan step 5.4) is sent as its own report: ``keys`` filled, ``checks``
    and ``formats`` empty."""

    device: str = Field(min_length=1, max_length=80, description="Name typed by the tester, e.g. 'iPhone 13 – Safari'.")
    user_agent: str = Field(max_length=600)
    auto: bool = Field(False, description="Run without a person (no yes/no answers).")
    screen: str = Field("", max_length=40)
    touch: bool = False
    checks: dict[str, CheckResult] = Field(max_length=20)
    formats: list[FormatResult] = Field(max_length=20)
    confirmations: dict[str, bool | None] = Field(default_factory=dict, max_length=10,
                                                  description="Yes/no answers of the tester.")
    keys: list[RemoteKeyResult] = Field(default_factory=list, max_length=20,
                                        description="Remote-key test: one entry per button asked for.")
    key_log: list[RemoteKeyEvent] = Field(default_factory=list, max_length=60,
                                          description="Remote-key test: the raw events, oldest first.")
    notes: str = Field("", max_length=1000)


CodecName = Literal["h264", "hevc", "av1", "vp9", "aac", "mp3", "ac3", "eac3", "opus", "flac"]


class PlayRequest(_Strict):
    """What this browser can decode in MP4 (plan step 5.1), measured by the player page."""

    video: list[CodecName] = Field(default_factory=list, max_length=10)
    audio: list[CodecName] = Field(default_factory=list, max_length=10)
    avoid_direct: bool = Field(False, description="The file itself already failed in this browser: "
                                                  "convert even if it looks playable.")


class PlayPlan(_Strict):
    mode: Literal["direct", "remux", "transcode"] = Field(
        description="direct: play `url` as it is; remux: picture copied into MP4 pieces (HLS); "
                    "transcode: converted to H.264 while watching (HLS).")
    url: str = Field(description="The file (direct) or the HLS playlist (.m3u8).")
    mime: str | None = Field(None, description="MP4 type of the pieces, for MediaSource.")
    session: str | None = Field(None, description="Transcode session; `POST /api/play/sessions/<id>/stop` "
                                                  "ends it at once (the player does it when the tab closes).")
    reason: str = Field("", description="What the hub does, for the viewer.")


class Conversion(_Strict):
    kind: Literal["transcode", "remux"]
    id: str
    media_id: str
    title: str
    client: str
    running: bool = Field(description="An ffmpeg process is working on it now.")
    pid: int | None
    position_seconds: int | None = Field(description="Where the viewer is (transcode).")
    idle_seconds: float = Field(description="Since the last request of the viewer.")
    error: str | None


class Conversions(_Strict):
    active: int
    limit: int
    items: list[Conversion]


class NetworkAddress(_Strict):
    ip: str
    name: str = Field(description="Adapter name, e.g. 'Wi-Fi', 'Ethernet 2'.")
    kind: Literal["ethernet", "wifi", "vpn", "virtual", "loopback", "other"]
    up: bool
    url: str | None = Field(None, description="The hub's address on it (for the addresses to use).")


class NetworkInfo(_Strict):
    port: int
    addresses: list[NetworkAddress] = Field(description="What phones should open, best first.")
    others: list[NetworkAddress] = Field(description="Other addresses of this computer (VPN, virtual, unplugged).")
    qr_svg: str | None = Field(description="QR code of the first address (an SVG data: URI).")
    warnings: list[str]


class PickedFolder(_Strict):
    """The folder chosen in the server's own folder picker (Explorer / Finder). Admin only."""

    path: str | None = Field(description="The chosen folder; null when the picker was cancelled.")
    cancelled: bool


class ToolsInfo(_Strict):
    """ffmpeg/ffprobe on the server and what is off without them (plan step 6.1). Admin only."""

    ffmpeg: str | None = Field(description="Path used, or null when not found.")
    ffprobe: str | None
    disabled: list[str] = Field(description="Features that are off because a tool is missing.")
    hint: str = Field(description="How to install ffmpeg.")


class RescanStatus(_Strict):
    state: Literal["idle", "running", "done", "failed"]
    started: bool = Field(False, description="True if this request started a rescan; false if one "
                                             "was already running (the status is of that one).")
    phase: Literal["scanning", "media", None] = Field(
        None, description="scanning: listing folders; media: reading details / thumbnails.")
    progress_done: int | None = None
    progress_total: int | None = Field(None, description="Known during the media phase.")
    started_at: datetime | None = None
    finished_at: datetime | None = None
    counts: RescanCounts | None = Field(None, description="When done.")
    error: str | None = Field(None, description="When failed.")


# ---------------------------------------------------------------------- TV screens (plan step 5.4)
# A smart TV opens /tv and becomes a "screen"; a phone (/remote) sends it commands.

SCREEN_TOKEN = r"^[A-Za-z0-9_-]{16,64}$"


class ScreenRegister(_Strict):
    """Sent by the TV page. With the `id` + `key` it had before (page reload, hub restart), the
    same screen comes back, so phones need not pick the TV again."""

    id: str | None = Field(None, pattern=SCREEN_TOKEN)
    key: str | None = Field(None, pattern=SCREEN_TOKEN)
    name: str = Field("TV", min_length=1, max_length=60)


class SubtitleChoice(_Strict):
    key: str = Field(max_length=100)
    label: str = Field(max_length=100)


class ScreenNotice(_Strict):
    """A message the player shows on the TV (e.g. "Hub đang bận"), repeated on the phone."""

    title: str = Field(max_length=200)
    lines: list[str] = Field(default_factory=list, max_length=8)
    kind: Literal["info", "error"] = "error"


RepeatMode = Literal["off", "all", "one"]
PLAYLIST_MAX = 500


class ScreenPlaylist(_Strict):
    """Where the TV is in the playlist it plays (step 6.5)."""

    title: str | None = Field(None, max_length=200)
    position: int = Field(ge=1, le=PLAYLIST_MAX, description="1 = the first video (in play order).")
    length: int = Field(ge=1, le=PLAYLIST_MAX)
    shuffle: bool = False
    repeat: RepeatMode = "off"
    next_title: str | None = Field(None, max_length=300, description="The video after this one, if any.")
    up_next_in: int | None = Field(None, ge=0, le=60, description="Seconds before the next video starts.")


class ScreenState(_Strict):
    """What the TV reports (PUT /api/screens/{id}/state)."""

    status: Literal["idle", "loading", "playing", "paused", "ended"] = "idle"
    media_id: str | None = Field(None, max_length=200)
    title: str | None = Field(None, max_length=300)
    position: float = Field(0, ge=0, le=10_000_000)
    duration: float | None = Field(None, ge=0, le=10_000_000)
    volume: float = Field(1, ge=0, le=1)
    muted: bool = False
    subtitles: list[SubtitleChoice] = Field(default_factory=list, max_length=30)
    subtitle: str = Field("off", max_length=100, description="Key of the subtitle shown, or 'off'.")
    notice: ScreenNotice | None = None
    sound_blocked: bool = Field(False, description="The TV browser refused sound until someone clicks on "
                                                   "the TV page: it plays muted.")
    started: bool = Field(False, description="'Bắt đầu' was pressed on the TV (fullscreen, sound allowed).")
    playlist: ScreenPlaylist | None = Field(None, description="Set while the TV plays a playlist (step 6.5).")
    problem: Literal["failed", "busy"] | None = Field(
        None, description="failed: this video cannot play (a playlist moves on); busy: 'Hub đang bận'.")


class ScreenInfo(_Strict):
    id: str
    code: str = Field(description="Short code shown on the TV, typed on the phone.")
    name: str
    online: bool = Field(description="The TV page is connected and receives commands.")
    state: ScreenState
    state_age: float | None = Field(description="Seconds since the TV last reported its state.")


class ScreenList(_Strict):
    screens: list[ScreenInfo]


class ScreenRegistered(ScreenInfo):
    key: str = Field(description="Secret of the TV page: needed for /events and /state. Not shown to phones.")
    remote_url: str = Field(description="The phone remote of this screen (also in the QR code).")
    qr_svg: str = Field(description="QR code of remote_url (an SVG data: URI).")


class _Cmd(_Strict):
    pass


class CmdPlayMedia(_Cmd):
    action: Literal["play_media"]
    media_id: str = Field(min_length=1, max_length=200)
    at: float = Field(0, ge=0, le=10_000_000, description="Start at this second (e.g. where the phone was).")


class CmdPlayList(_Cmd):
    """Play several videos in turn on the TV (step 6.5). The hub checks every ID, drops the ones
    no longer in the library, and sends the TV each video's title."""

    action: Literal["play_list"]
    media_ids: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        min_length=1, max_length=PLAYLIST_MAX, description="In the order to play.")
    index: int = Field(0, ge=0, lt=PLAYLIST_MAX, description="The item of media_ids to start with.")
    at: float = Field(0, ge=0, le=10_000_000, description="Start that first video at this second.")
    shuffle: bool = False
    repeat: RepeatMode = "off"
    title: str = Field("", max_length=200)


class CmdPlaylistMode(_Cmd):
    action: Literal["playlist_mode"]
    shuffle: bool | None = None
    repeat: RepeatMode | None = None


class CmdSimple(_Cmd):
    """`next` / `previous`: the video after / before in the TV's playlist (step 6.5)."""

    action: Literal["play", "pause", "stop", "next", "previous"]


class CmdSeek(_Cmd):
    action: Literal["seek"]
    to: float = Field(ge=0, le=10_000_000)


class CmdSeekBy(_Cmd):
    action: Literal["seek_by"]
    by: float = Field(ge=-36_000, le=36_000)


class CmdSubtitle(_Cmd):
    action: Literal["subtitle"]
    key: str = Field(max_length=100, description="A key from the state's `subtitles`, or 'off'.")


class CmdVolume(_Cmd):
    action: Literal["volume"]
    level: float = Field(ge=0, le=1)


class CmdMute(_Cmd):
    action: Literal["mute"]
    on: bool


ScreenCommand = Annotated[
    Union[CmdPlayMedia, CmdPlayList, CmdPlaylistMode, CmdSimple, CmdSeek, CmdSeekBy, CmdSubtitle, CmdVolume,
          CmdMute],
    Field(discriminator="action"),
]
