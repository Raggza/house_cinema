"""Conversions in progress: start, share, throttle and stop the ffmpeg processes (step 5.1).

Two kinds, both producing HLS pieces (fMP4) under ``<cache>/transcode/<media id>/``:

- **Remux job** (``remux-copy`` / ``remux-aac``): the picture is copied; shared by every
  viewer of that video, kept in the cache when finished (up to ``transcode_cache_gb``).
  ffmpeg's own playlist is served while it grows.
- **Transcode session** (``sessions/<sid>``): one per viewer. The playlist is written by the
  hub up front (piece n = n × 6 s … (n+1) × 6 s), so the whole video can be seeked at once.
  ffmpeg starts at the piece that is asked for, and is restarted where the viewer seeks to;
  it is stopped when it gets too far ahead, when the viewer leaves, or after
  ``transcode_idle_seconds`` without any request. The files are deleted afterwards.

At most ``transcode_max_sessions`` conversions run at once (transcode sessions + running
remux jobs); one more gets ``Busy``.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import secrets
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from source.modules.transcode.plan import SEGMENT_SECONDS, Decision
from source.modules.transcode.process import Encoder
from source.schemas.settings import Settings
from source.utils.ffmpeg import FFTools

logger = logging.getLogger("house_cinema.transcode")

SEGMENT_NAME = re.compile(r"seg(\d{5})\.m4s")
SESSION_ID = re.compile(r"[0-9a-f]{16}")
REMUX_VARIANTS = ("copy", "aac")
AHEAD_SEGMENTS = 20
"""ffmpeg is stopped when it is this many pieces (2 min) ahead of the viewer."""
RESTART_GAP = 3
"""A piece at most this far after what ffmpeg made so far is waited for, not restarted."""
KEEP_BEHIND = 30
"""Pieces more than this far behind the viewer are deleted (made again on a seek back)."""
TOMBSTONE_SECONDS = 6 * 3600
"""An ended session can be picked up again for this long (a paused tab that resumes)."""
TICK_SECONDS = 1.0


class Busy(Exception):
    def __init__(self, active: int, limit: int):
        super().__init__(f"{active} conversions running (limit {limit})")
        self.active, self.limit = active, limit


class SessionGone(Exception):
    """Unknown session / remux job / file."""


class ConvertFailed(Exception):
    """ffmpeg failed; the message is short and holds no path."""


@dataclass
class RemuxJob:
    media_id: str
    variant: str
    folder: Path
    signature: str
    done: bool = False
    encoder: Encoder | None = None
    error: str | None = None
    last_seen: float = field(default_factory=time.monotonic)
    last_saved: float = 0.0

    @property
    def running(self) -> bool:
        return self.encoder is not None and self.encoder.running()


@dataclass
class TranscodeSession:
    sid: str
    media_id: str
    title: str
    source: Path
    duration: float
    has_audio: bool
    folder: Path
    client: str
    segments: int
    active: bool = True
    encoder: Encoder | None = None
    enc_start: int = 0
    produced: int = -1
    """Last piece of the current run known to exist (contiguous from enc_start)."""
    last_requested: int = 0
    error: str | None = None
    last_seen: float = field(default_factory=time.monotonic)
    ended_at: float = 0.0
    closed: bool = False
    """Stopped by the viewer (tab closed, next video of a playlist): never picked up again. Only a
    session ended for being idle can resume (a paused tab coming back)."""


def segment_count(duration: float) -> int:
    # A tail shorter than 0.25 s may hold no frame at all: ffmpeg would never write it.
    return max(1, math.ceil((duration - 0.25) / SEGMENT_SECONDS))


def vod_playlist(duration: float) -> str:
    n = segment_count(duration)
    lines = ["#EXTM3U", "#EXT-X-VERSION:7", f"#EXT-X-TARGETDURATION:{SEGMENT_SECONDS}",
             "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD", "#EXT-X-INDEPENDENT-SEGMENTS",
             '#EXT-X-MAP:URI="init.mp4"']
    for i in range(n):
        length = SEGMENT_SECONDS if i < n - 1 else max(0.1, duration - SEGMENT_SECONDS * (n - 1))
        lines += [f"#EXTINF:{length:.6f},", f"seg{i:05d}.m4s"]
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def read_shared(path: Path) -> bytes | None:
    """Read a file that ffmpeg may replace at any moment (its playlist). On Windows a normal
    open() blocks ffmpeg's rename-over-it, so the file is opened with FILE_SHARE_DELETE."""
    if os.name != "nt":
        try:
            return path.read_bytes()
        except OSError:
            return None
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                     wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    handle = kernel32.CreateFileW(str(path), 0x80000000, 0x7, None, 3, 0, None)  # GENERIC_READ, share all
    if handle in (None, wintypes.HANDLE(-1).value):
        return None
    fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    with os.fdopen(fd, "rb") as f:
        return f.read()


RMTREE_TRIES = 4


def _rmtree(path: Path, tries: int = RMTREE_TRIES) -> str | None:
    """Delete a folder of ffmpeg output; None when it is gone, else the last problem.

    On Windows a deleted file stays "delete pending" while anyone still has it open (a piece
    being sent to a player, the antivirus, the indexer), and removing its folder fails with
    "directory is not empty" (WinError 145). A short retry covers most cases; the manager
    tries the rest again later (``TranscodeManager._delete``). Found in step 6.2."""
    problem = "still there"
    for attempt in range(tries):
        problems: list[str] = []
        shutil.rmtree(path, onexc=lambda func, p, exc: problems.append(f"{Path(p).name}: {exc}"))
        if not path.exists():
            return None
        problem = problems[-1] if problems else problem
        if attempt + 1 < tries:
            time.sleep(0.05 * (attempt + 1))
    return problem


class TranscodeManager:
    def __init__(self, settings: Settings, tools: FFTools):
        self.settings = settings
        self.tools = tools
        self.root = settings.cache_dir / "transcode"
        self.limit = settings.transcode_max_sessions
        self._lock = threading.RLock()
        self._remux: dict[tuple[str, str], RemuxJob] = {}
        self._sessions: dict[str, TranscodeSession] = {}
        self._stop = threading.Event()
        self._reaper: threading.Thread | None = None
        self._last_evict = 0.0
        self._trash: set[Path] = set()
        """Folders that could not be deleted yet (files in use): tried again every tick."""
        self._cleanup_leftovers()

    def _delete(self, folder: Path) -> None:
        problem = _rmtree(folder)
        if problem is not None:
            self._trash.add(folder)
            logger.info("Could not delete %s yet (%s): trying again later", folder, problem)

    def _retry_trash(self, final: bool = False) -> None:
        for folder in list(self._trash):
            problem = _rmtree(folder, tries=RMTREE_TRIES if final else 1)
            if problem is None:
                self._trash.discard(folder)
                logger.info("Deleted %s (its files were in use before)", folder)
            elif final:
                logger.warning("Could not delete %s: %s (removed at the next start)", folder, problem)

    # ------------------------------------------------------------------ state

    def unavailable_reason(self) -> str | None:
        if not self.settings.transcode_enabled:
            return "Chức năng chuyển đổi định dạng đang tắt (transcode.enabled trong config.yml)."
        if not self.tools.ffmpeg:
            return "Máy chủ chưa cài ffmpeg nên không chuyển đổi được định dạng này."
        return None

    def active_count(self) -> int:
        with self._lock:
            return (sum(1 for s in self._sessions.values() if s.active)
                    + sum(1 for j in self._remux.values() if j.running))

    def running_pids(self) -> list[int]:
        with self._lock:
            encoders = [s.encoder for s in self._sessions.values()] + [j.encoder for j in self._remux.values()]
            return [e.pid for e in encoders if e is not None and e.running()]

    def status(self) -> list[dict]:
        now = time.monotonic()
        with self._lock:
            rows = [{"kind": "transcode", "id": s.sid, "media_id": s.media_id, "title": s.title,
                     "client": s.client, "running": bool(s.encoder and s.encoder.running()),
                     "pid": s.encoder.pid if s.encoder and s.encoder.running() else None,
                     "position_seconds": s.last_requested * SEGMENT_SECONDS,
                     "idle_seconds": round(now - s.last_seen, 1), "error": s.error}
                    for s in self._sessions.values() if s.active]
            rows += [{"kind": "remux", "id": f"{j.media_id}/{j.variant}", "media_id": j.media_id, "title": "",
                      "client": "", "running": j.running, "pid": j.encoder.pid if j.running else None,
                      "position_seconds": None, "idle_seconds": round(now - j.last_seen, 1), "error": j.error}
                     for j in self._remux.values() if j.running or j.error]
        return rows

    # ------------------------------------------------------------------ start

    def open(self, *, media_id: str, title: str, source: Path, signature: str, duration: float | None,
             decision: Decision, client: str) -> str:
        """Start (or join) a conversion; returns the playlist URL. Raises Busy."""
        self._ensure_reaper()
        if decision.mode == "remux":
            return self._open_remux(media_id, source, signature, decision)
        assert duration
        with self._lock:
            if self.active_count() >= self.limit:
                raise Busy(self.active_count(), self.limit)
            sid = secrets.token_hex(8)
            folder = self.root / media_id / "sessions" / sid
            session = TranscodeSession(sid=sid, media_id=media_id, title=title, source=source,
                                       duration=duration, has_audio=decision.has_audio, folder=folder,
                                       client=client, segments=segment_count(duration))
            self._prepare(session)
            self._sessions[sid] = session
        logger.info("Transcode session %s for %s (%s) from %s", sid, media_id, title, client)
        return f"/hls/s/{sid}/index.m3u8"

    def _prepare(self, session: TranscodeSession) -> None:
        self._trash.discard(session.folder)  # a resumed session uses its folder again
        session.folder.mkdir(parents=True, exist_ok=True)
        (session.folder / "index.m3u8").write_text(vod_playlist(session.duration), encoding="utf-8")

    def _open_remux(self, media_id: str, source: Path, signature: str, decision: Decision) -> str:
        variant = "copy" if decision.copy_audio else "aac"
        url = f"/hls/r/{media_id}/{variant}/index.m3u8"
        with self._lock:
            job = self._remux.get((media_id, variant)) or self._load_remux(media_id, variant)
            if job is not None and job.signature != signature:  # the file changed since
                self._drop_remux(job, "file changed")
                job = None
            if job is not None and (job.done or job.running):
                job.last_seen = time.monotonic()
                self._remux[(media_id, variant)] = job
                return url
            if self.active_count() >= self.limit:
                raise Busy(self.active_count(), self.limit)
            folder = self.root / media_id / f"remux-{variant}"
            self._trash.discard(folder)  # in use again: a later retry must not delete the new job
            _rmtree(folder)
            folder.mkdir(parents=True, exist_ok=True)
            job = RemuxJob(media_id=media_id, variant=variant, folder=folder, signature=signature)
            self._save_meta(job)
            job.encoder = Encoder(self._remux_args(source, folder, decision), folder / "ffmpeg.log",
                                  label=f"remux {media_id} ({variant})")
            self._remux[(media_id, variant)] = job
        return url

    def _remux_args(self, source: Path, folder: Path, d: Decision) -> list[str]:
        args = [self.tools.ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-i", str(source),
                "-map", "0:v:0"]
        if d.has_audio:
            args += ["-map", "0:a:0"]
        args += ["-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1", "-c:v", "copy"]
        if "hvc1" in d.mime:
            args += ["-tag:v", "hvc1"]  # Safari plays HEVC in MP4 only with this tag
        if d.has_audio:
            args += ["-c:a", "copy"] if d.copy_audio else ["-c:a", "aac", "-ac", "2", "-b:a", "192k"]
        return args + self._hls_out(folder, "init.mp4", folder / "index.m3u8")

    def _transcode_args(self, s: TranscodeSession, start: int) -> list[str]:
        t = start * SEGMENT_SECONDS
        h = self.settings.transcode_max_height
        args = [self.tools.ffmpeg, "-nostdin", "-hide_banner", "-v", "error"]
        if t:
            args += ["-ss", str(t)]
        args += ["-i", str(s.source), "-map", "0:v:0"]
        if s.has_audio:
            args += ["-map", "0:a:0"]
        encoder = self.settings.transcode_video_encoder
        args += ["-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1",
                 "-vf", f"scale=-2:'trunc(min({h},ih)/2)*2',format=yuv420p", "-c:v", encoder]
        if encoder == "libx264":
            args += ["-preset", "veryfast", "-crf", "23", "-profile:v", "high"]
        else:
            args += ["-b:v", "6M"]
        # A key frame exactly every 6 s: piece n of any run starts at n × 6 s.
        args += ["-force_key_frames", f"expr:gte(t,n_forced*{SEGMENT_SECONDS})", "-sc_threshold", "0"]
        if s.has_audio:
            args += ["-c:a", "aac", "-ac", "2", "-b:a", "160k"]
        out = self._hls_out(s.folder, f"init-{start}.mp4", s.folder / f"run-{start}.m3u8")
        # frag_discont: each piece carries its real time (tfdt). Without it ffmpeg puts the
        # start offset in the init file's edit list, which MediaSource ignores: a piece made
        # after a seek to 0:42 would land at 0:00.
        return args + ["-start_number", str(start), "-output_ts_offset", str(t),
                       "-hls_segment_options", "movflags=+frag_discont"] + out

    @staticmethod
    def _hls_out(folder: Path, init_name: str, playlist: Path) -> list[str]:
        # Forward slashes: ffmpeg finds the folder for the init file from the playlist path by
        # its last "/"; with "\" it would write the init file into the current directory.
        return ["-f", "hls", "-hls_time", str(SEGMENT_SECONDS), "-hls_segment_type", "fmp4",
                "-hls_fmp4_init_filename", init_name,
                "-hls_segment_filename", (folder / "seg%05d.m4s").as_posix(),
                "-hls_playlist_type", "event", "-hls_flags", "temp_file", playlist.as_posix()]

    # ------------------------------------------------------------------ remux files

    def _load_remux(self, media_id: str, variant: str) -> RemuxJob | None:
        """A finished remux left in the cache by an earlier run of the hub."""
        folder = self.root / media_id / f"remux-{variant}"
        try:
            meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not meta.get("done"):
            return None
        self._promote_playlist(folder)  # cached by a hub version without this fix
        return RemuxJob(media_id=media_id, variant=variant, folder=folder, signature=meta.get("signature", ""),
                        done=True)

    def _save_meta(self, job: RemuxJob) -> None:
        job.last_saved = time.monotonic()
        try:
            (job.folder / "meta.json").write_text(json.dumps(
                {"media_id": job.media_id, "signature": job.signature, "done": job.done,
                 "last_used": time.time()}), encoding="utf-8")
        except OSError as exc:
            logger.warning("Cannot write %s: %s", job.folder / "meta.json", exc)

    @staticmethod
    def _promote_playlist(folder: Path) -> None:
        """ffmpeg writes its playlist to index.m3u8.tmp, then renames it over index.m3u8. On
        Windows that rename fails while the hub is reading index.m3u8 for a player, and ffmpeg
        neither retries nor reports it: the final playlist (with #EXT-X-ENDLIST) can stay in
        the .tmp file, so the remux never looks finished (found in step 6.2: 19 of 60 runs with
        the playlist read in a loop). Called once ffmpeg has exited."""
        tmp, playlist = folder / "index.m3u8.tmp", folder / "index.m3u8"
        if not tmp.is_file():
            return
        for attempt in range(RMTREE_TRIES):
            try:
                os.replace(tmp, playlist)
                logger.info("Final playlist of %s taken from %s (ffmpeg could not rename it)", folder, tmp.name)
                return
            except OSError as exc:  # being read right now: again shortly
                problem = exc
                time.sleep(0.05 * (attempt + 1))
        logger.warning("Cannot finish the playlist in %s: %s", folder, problem)

    def _drop_remux(self, job: RemuxJob, why: str) -> None:
        if job.encoder is not None:
            job.encoder.stop(why)
        self._remux.pop((job.media_id, job.variant), None)
        self._delete(job.folder)

    def remux_file(self, media_id: str, variant: str, name: str) -> Path | bytes | None:
        """The playlist (bytes), a piece (path), or None = not made yet (ask again)."""
        with self._lock:
            job = self._remux.get((media_id, variant)) or self._load_remux(media_id, variant)
            if job is None:
                raise SessionGone(name)
            self._remux[(media_id, variant)] = job
            job.last_seen = time.monotonic()
            self._check_remux(job)
            if job.error:
                raise ConvertFailed(job.error)
            if job.done and time.monotonic() - job.last_saved > 60:
                self._save_meta(job)  # remember when it was last used (cache eviction)
            running = job.running
        path = job.folder / name
        if name == "index.m3u8":
            data = read_shared(path)
            return data if data and b"#EXTINF" in data else None
        if name == "init.mp4" and not job.done and not (job.folder / "seg00000.m4s").is_file():
            return None  # still being written (see transcode_file)
        if path.is_file():
            return path
        if not running and job.done:
            raise SessionGone(name)
        return None

    def _check_remux(self, job: RemuxJob) -> None:
        if job.encoder is None or job.encoder.running():
            return
        code = job.encoder.returncode
        if code == 0:
            self._promote_playlist(job.folder)
            job.done = True
            job.encoder.close()
            job.encoder = None
            self._save_meta(job)
            logger.info("Remux of %s (%s) finished", job.media_id, job.variant)
        elif job.error is None:
            job.error = f"ffmpeg dừng với lỗi: {job.encoder.error_text()}"
            logger.error("Remux of %s failed: %s", job.media_id, job.encoder.error_text())

    # ------------------------------------------------------------------ transcode files

    def transcode_file(self, sid: str, name: str) -> Path | None:
        """A file of a transcode session, or None = being made (ask again shortly)."""
        with self._lock:
            s = self._sessions.get(sid)
            if s is None or s.closed:
                # closed: a request still on its way when the viewer left must not start
                # ffmpeg again (found in step 6.5: the next video of a playlist).
                raise SessionGone(name)
            if not s.active:  # ended for being idle and asked again: pick it up
                if self.active_count() >= self.limit:
                    raise Busy(self.active_count(), self.limit)
                s.active, s.error = True, None
                self._prepare(s)
                logger.info("Transcode session %s resumed", sid)
            s.last_seen = time.monotonic()
            if name == "index.m3u8":
                return s.folder / name
            if name == "init.mp4":
                # The init file of run N is complete once its first piece exists (ffmpeg
                # writes it in place, not through a temp file like the pieces).
                inits = [p for p in s.folder.glob("init-*.mp4")
                         if (s.folder / f"seg{int(p.stem.split('-')[1]):05d}.m4s").is_file()]
                if inits:
                    return max(inits, key=lambda p: p.stat().st_mtime)
                if s.encoder is None or not s.encoder.running():
                    self._start_run(s, s.last_requested)
                return None
            m = SEGMENT_NAME.fullmatch(name)
            if not m or int(m.group(1)) >= s.segments:
                raise SessionGone(name)
            n = int(m.group(1))
            s.last_requested = n
            path = s.folder / name
            if path.is_file():
                return path
            enc = s.encoder
            if enc is not None and enc.running():
                self._update_produced(s)
                if s.enc_start <= n <= s.produced + RESTART_GAP:
                    return None  # coming soon
            elif enc is not None and s.enc_start <= n:
                if enc.returncode != 0:
                    s.error = f"ffmpeg dừng với lỗi: {enc.error_text()}"
                    logger.error("Transcode %s failed: %s", sid, enc.error_text())
                    raise ConvertFailed(s.error)
                self._update_produced(s)
                if n > s.produced:  # ffmpeg reached the end without this piece
                    raise SessionGone(name)
            self._start_run(s, n)
            return None

    def _start_run(self, s: TranscodeSession, start: int) -> None:
        if s.encoder is not None:
            s.encoder.stop("seek" if s.encoder.running() else "")
        for tmp in s.folder.glob("*.tmp"):
            tmp.unlink(missing_ok=True)
        s.enc_start, s.produced = start, start - 1
        s.encoder = Encoder(self._transcode_args(s, start), s.folder / "ffmpeg.log",
                            label=f"transcode {s.media_id} from {start * SEGMENT_SECONDS}s (session {s.sid})")

    @staticmethod
    def _update_produced(s: TranscodeSession) -> None:
        n = max(s.produced + 1, s.enc_start)
        while n < s.segments and (s.folder / f"seg{n:05d}.m4s").is_file():
            n += 1
        s.produced = n - 1

    def stop_session(self, sid: str, why: str = "viewer left") -> bool:
        with self._lock:
            s = self._sessions.get(sid)
            if s is None:
                return False
            s.closed = True  # also when it had already ended for being idle
            if not s.active:
                return False
            self._end_session(s, why)
            return True

    def _end_session(self, s: TranscodeSession, why: str) -> None:
        if s.encoder is not None:
            s.encoder.stop(why)
            s.encoder = None
        s.active, s.ended_at = False, time.monotonic()
        self._delete(s.folder)
        logger.info("Transcode session %s ended (%s)", s.sid, why)

    # ------------------------------------------------------------------ housekeeping

    def _ensure_reaper(self) -> None:
        with self._lock:
            if self._reaper is None or not self._reaper.is_alive():
                self._stop.clear()
                self._reaper = threading.Thread(target=self._reap_loop, name="transcode-reaper", daemon=True)
                self._reaper.start()

    def _reap_loop(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            try:
                self.reap()
            except Exception:  # keep housekeeping alive whatever happens
                logger.exception("Transcode housekeeping failed")

    def reap(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        idle = self.settings.transcode_idle_seconds
        with self._lock:
            for s in list(self._sessions.values()):
                if not s.active:
                    if now - s.ended_at > TOMBSTONE_SECONDS:
                        del self._sessions[s.sid]
                    continue
                if now - s.last_seen > idle:
                    self._end_session(s, f"no request for {idle} s")
                    continue
                if s.encoder is not None and s.encoder.running():
                    self._update_produced(s)
                    if s.produced - s.last_requested > AHEAD_SEGMENTS:
                        s.encoder.stop("far enough ahead of the viewer")
                        s.encoder = None  # stopped on purpose: not an error; restarted on demand
                for old in s.folder.glob("seg*.m4s"):
                    m = SEGMENT_NAME.fullmatch(old.name)
                    if m and int(m.group(1)) < s.last_requested - KEEP_BEHIND:
                        try:
                            old.unlink()
                        except OSError:
                            pass  # being sent right now: next time
            for job in list(self._remux.values()):
                # Look at ffmpeg once: it can exit at any moment. Checking "finished?" and then
                # "running?" separately missed a remux ending in between (neither marked done
                # nor dropped until the next tick: the flaky test of step 5.1, found in 6.2).
                running = job.running
                if not running:
                    self._check_remux(job)  # finished: done (kept as cache) or failed
                if running and now - job.last_seen > idle:
                    self._drop_remux(job, f"nobody watching for {idle} s")
                elif job.error and now - job.last_seen > idle:
                    self._drop_remux(job, "failed")
            if now - self._last_evict > 60:
                self._last_evict = now
                self._evict_cache(now)
            self._retry_trash()

    def _evict_cache(self, now: float) -> None:
        """Keep finished remuxes within transcode_cache_gb, least recently used out first."""
        limit = self.settings.transcode_cache_gb * 1e9
        done: list[tuple[float, int, Path]] = []
        for meta_file in self.root.glob("*/remux-*/meta.json"):
            folder = meta_file.parent
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not meta.get("done"):
                continue
            key = (folder.parent.name, folder.name.removeprefix("remux-"))
            job = self._remux.get(key)
            if job is not None and now - job.last_seen <= self.settings.transcode_idle_seconds:
                continue  # being watched
            size = sum(f.stat().st_size for f in folder.iterdir() if f.is_file())
            done.append((float(meta.get("last_used", 0)), size, folder))
        total = sum(size for _, size, _ in done)
        for _, size, folder in sorted(done):
            if total <= limit:
                break
            self._remux.pop((folder.parent.name, folder.name.removeprefix("remux-")), None)
            self._delete(folder)
            total -= size
            logger.info("Removed cached remux %s (%.1f MB) to stay under %g GB", folder.parent.name,
                        size / 1e6, self.settings.transcode_cache_gb)

    def _cleanup_leftovers(self) -> None:
        """Sessions and unfinished remuxes of an earlier run of the hub are useless now."""
        if not self.root.is_dir():
            return
        for sessions in self.root.glob("*/sessions"):
            _rmtree(sessions)
        for folder in self.root.glob("*/remux-*"):
            if self._load_remux(folder.parent.name, folder.name.removeprefix("remux-")) is None:
                _rmtree(folder)

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            for s in self._sessions.values():
                if s.active:
                    self._end_session(s, "hub stopping")
            for job in list(self._remux.values()):
                if job.running:
                    self._drop_remux(job, "hub stopping")
        if self._reaper is not None:
            self._reaper.join(timeout=5)
        with self._lock:
            self._retry_trash(final=True)
