"""Device test page (plan step 4.4): sample videos, and reports sent back by each device.

Open http://<LAN-IP>:8000/diagnostics on a phone/computer: the page plays these samples
(one per format), seeks, shows subtitles, asks a few yes/no questions (what only a human can
see: is the subtitle on screen, did fullscreen work) and sends the results here. They are
kept in data/device-reports/ and turned into docs/testing/device-matrix.md by
scripts/device_matrix.py.

The samples are made once with ffmpeg in <cache>/diagnostics/ (a few seconds of work).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from source.utils.ffmpeg import FFTools

logger = logging.getLogger("house_cinema.server.diagnostics")

MAX_REPORT_BYTES = 64 * 1024
MAX_REPORTS = 500


@dataclass(frozen=True)
class Sample:
    key: str
    file: str
    label: str
    mime: str
    """Type with codecs, as asked with canPlayType()."""
    args: tuple[str, ...]
    """ffmpeg output options (inputs: test pattern + tone)."""
    seconds: int = 8
    must_play: bool = False
    """The plan's minimum: every device must play this one."""


_V = ("-f", "lavfi", "-i")
SAMPLES: tuple[Sample, ...] = (
    Sample("mp4-h264", "sample-h264.mp4", "MP4 · H.264 + AAC", 'video/mp4; codecs="avc1.4d401e, mp4a.40.2"',
           ("-c:v", "libx264", "-profile:v", "main", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-g", "15",
            "-c:a", "aac", "-movflags", "+faststart"), seconds=60, must_play=True),
    Sample("webm-vp9", "sample-vp9.webm", "WebM · VP9 + Opus", 'video/webm; codecs="vp09.00.10.08, opus"',
           ("-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-pix_fmt", "yuv420p", "-b:v", "300k",
            "-c:a", "libopus")),
    Sample("mkv-h264", "sample-h264.mkv", "MKV · H.264 + AAC", 'video/x-matroska; codecs="avc1.4d401e, mp4a.40.2"',
           ("-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-c:a", "aac")),
    Sample("mp4-hevc", "sample-hevc.mp4", "MP4 · HEVC (H.265) + AAC", 'video/mp4; codecs="hvc1.1.6.L93.B0, mp4a.40.2"',
           ("-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-tag:v", "hvc1",
            "-x265-params", "log-level=error", "-c:a", "aac", "-movflags", "+faststart")),
    Sample("mkv-hevc", "sample-hevc.mkv", "MKV · HEVC (H.265) + AAC", 'video/x-matroska; codecs="hvc1.1.6.L93.B0, mp4a.40.2"',
           ("-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-x265-params", "log-level=error",
            "-c:a", "aac")),
    Sample("mp4-av1", "sample-av1.mp4", "MP4 · AV1 + AAC", 'video/mp4; codecs="av01.0.04M.08, mp4a.40.2"',
           ("-c:v", "libaom-av1", "-cpu-used", "8", "-row-mt", "1", "-b:v", "200k", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-movflags", "+faststart")),
    Sample("mp4-h264-ac3", "sample-h264-ac3.mp4", "MP4 · H.264 + AC-3 (Dolby)", 'video/mp4; codecs="avc1.4d401e, ac-3"',
           ("-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-c:a", "ac3", "-movflags", "+faststart")),
)
SAMPLES_BY_FILE = {s.file: s for s in SAMPLES}
SUBTITLE_FILE = "sample-h264.vi.vtt"
SUBTITLE_VTT = """WEBVTT

00:00:00.000 --> 00:00:04.000
Xin chào! Đây là phụ đề tiếng Việt

00:00:28.000 --> 00:00:36.000
Phụ đề ở giây thứ 30: tua tới đây thành công
"""


class SampleLibrary:
    """Makes the samples on first use (thread-safe), then serves them from the cache."""

    def __init__(self, cache_dir: Path, tools: FFTools):
        self.folder = Path(cache_dir) / "diagnostics"
        self.tools = tools
        self._lock = threading.Lock()
        self.failed: dict[str, str] = {}

    def path(self, file: str) -> Path | None:
        return self.folder / file if (file in SAMPLES_BY_FILE or file == SUBTITLE_FILE) else None

    def ensure(self) -> list[Sample]:
        """The samples available (made now if missing). Needs ffmpeg."""
        with self._lock:
            self.folder.mkdir(parents=True, exist_ok=True)
            sub = self.folder / SUBTITLE_FILE
            if not sub.exists():
                sub.write_text(SUBTITLE_VTT, encoding="utf-8")
            ready = []
            for s in SAMPLES:
                target = self.folder / s.file
                if not target.exists() and s.key not in self.failed and self.tools.ffmpeg:
                    self._make(s, target)
                if target.exists():
                    ready.append(s)
            return ready

    def _make(self, s: Sample, target: Path) -> None:
        tmp = target.with_name(f"tmp-{os.getpid()}-{target.name}")
        size = "640x360" if s.must_play else "320x180"
        cmd = [self.tools.ffmpeg, "-v", "error", "-y",
               *_V, f"testsrc2=duration={s.seconds}:size={size}:rate=15",
               *_V, f"sine=frequency=440:duration={s.seconds}",
               *s.args, "-shortest", str(tmp)]
        started = time.perf_counter()
        try:
            run = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if run.returncode != 0:
                raise RuntimeError((run.stderr or "ffmpeg failed").strip().splitlines()[-1][:300])
            os.replace(tmp, target)
            logger.info("Made test sample %s in %.1f s", target.name, time.perf_counter() - started)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            self.failed[s.key] = str(exc)
            logger.warning("Cannot make test sample %s (%s): this format is left out of the test", s.file, exc)
        finally:
            tmp.unlink(missing_ok=True)


# ------------------------------------------------------------------ reports

_SLUG = re.compile(r"[^a-z0-9]+")


def save_report(folder: Path, report: dict, client_ip: str) -> str:
    """Write one device report; returns its file name. Keeps the newest MAX_REPORTS."""
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    slug = _SLUG.sub("-", str(report.get("device", "")).lower()).strip("-")[:40] or "device"
    name = f"{stamp}-{slug}.json"
    n = 1
    while (folder / name).exists():
        n += 1
        name = f"{stamp}-{slug}-{n}.json"
    data = {**report, "client_ip": client_ip, "received_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    (folder / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    old = sorted(folder.glob("*.json"))[:-MAX_REPORTS]
    for f in old:
        f.unlink(missing_ok=True)
    logger.info("Device report from %s saved: %s", client_ip, name)
    return name
