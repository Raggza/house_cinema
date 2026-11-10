"""Soak test for plan step 6.2: several simulated devices use the hub for a long time while
the hub's memory, handles, threads and ffmpeg processes are logged. A leak shows as a line
that keeps going up; a healthy hub stays flat and is back to its start level when idle.

Each simulated device repeats, at random:
- browsing: library page, folders, video details, thumbnails, subtitle tracks and files;
- playing: asks POST /api/play like the player page, then
  - direct: Range requests at random places, each read cut short (a seek / closed tab),
  - remux / transcode: the HLS playlist, init and pieces, a seek, then the tab is closed
    (POST .../stop) or simply abandoned (the hub must stop ffmpeg on its own);
- the TV remote: the TV page's command stream (SSE) and a command sent to it.
Device 1 also asks for a rescan now and then.

Usage (from the project folder):
    uv run python demo/soak.py --minutes 60
        starts its own hub on 127.0.0.1 with generated test videos (in --work);
    uv run python demo/soak.py --hub http://127.0.0.1:8000 --process HouseCinema.exe
        uses a hub that is already running (e.g. the installed app), measured by process name.

Writes into --work: resources.csv (one row per sample), errors.log, summary.json and, for
its own hub, hub.log. Exit code 0 when the verdict is PASS, 1 otherwise.
"""

from __future__ import annotations

import argparse
import csv
import http.client
import json
import os
import random
import re
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "test" / "fixtures" / "library"
BROWSER = {"video": ["h264"], "audio": ["aac", "mp3"]}  # a browser without HEVC / AC-3
DEFAULT_WORK = ROOT / "build" / "soak"

# ============================================================================ processes


@dataclass
class Proc:
    pid: int
    ppid: int
    name: str
    threads: int | None


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _ENTRY(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    class _PMC(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (n, ctypes.c_size_t) for n in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                           "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                                           "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage",
                                           "PrivateUsage")]

    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ENTRY)]
    _k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ENTRY)]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]

    def processes() -> list[Proc]:
        snap = _k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
        if snap in (None, wintypes.HANDLE(-1).value):
            return []
        out = []
        try:
            entry = _ENTRY()
            entry.dwSize = ctypes.sizeof(_ENTRY)
            ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                out.append(Proc(entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile, entry.cntThreads))
                ok = _k32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            _k32.CloseHandle(snap)
        return out

    def proc_stats(pid: int) -> dict:
        h = _k32.OpenProcess(0x1000 | 0x0010, False, pid)  # QUERY_LIMITED_INFORMATION | VM_READ
        if not h:
            return {}
        try:
            count = wintypes.DWORD()
            _k32.GetProcessHandleCount(h, ctypes.byref(count))
            pmc = _PMC()
            pmc.cb = ctypes.sizeof(_PMC)
            _k32.K32GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb)
            return {"rss_mb": pmc.WorkingSetSize / 2**20, "private_mb": pmc.PrivateUsage / 2**20,
                    "handles": count.value}
        finally:
            _k32.CloseHandle(h)
else:
    def processes() -> list[Proc]:
        try:
            text = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,comm="], capture_output=True, text=True).stdout
        except OSError:
            return []
        out = []
        for line in text.splitlines():
            parts = line.split(None, 2)
            if len(parts) == 3 and parts[0].isdigit():
                out.append(Proc(int(parts[0]), int(parts[1]), Path(parts[2]).name, None))
        return out

    def proc_stats(pid: int) -> dict:
        status = Path(f"/proc/{pid}/status")
        if status.is_file():  # Linux
            fields = dict(line.split(":", 1) for line in status.read_text().splitlines() if ":" in line)
            return {"rss_mb": int(fields["VmRSS"].split()[0]) / 1024, "private_mb": None,
                    "handles": len(os.listdir(f"/proc/{pid}/fd"))}
        try:  # macOS: no handle count without lsof
            rss = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True).stdout
            return {"rss_mb": int(rss.strip()) / 1024, "private_mb": None, "handles": None}
        except (OSError, ValueError):
            return {}


def is_ffmpeg(p: Proc) -> bool:
    return p.name.lower().startswith("ffmpeg")


def follow_launcher(pid: int) -> int:
    """`.venv\\Scripts\\python.exe` (uv) and `uv run` start the real interpreter as a child:
    measure that one."""
    for _ in range(4):
        kids = [p for p in processes() if p.ppid == pid and re.match(r"(python|housecinema)", p.name.lower())]
        if len(kids) != 1:
            break
        pid = kids[0].pid
    return pid


def find_by_name(name: str) -> int:
    found = [p for p in processes() if p.name.lower() == name.lower()]
    if not found:
        raise SystemExit(f"No running process named {name}")
    # A packaged app may be a launcher + child: take the one whose parent is not the same exe.
    pids = {p.pid for p in found}
    roots = [p for p in found if p.ppid not in pids]
    return follow_launcher(roots[0].pid if roots else found[0].pid)


# ============================================================================ HTTP


class Stats:
    def __init__(self, error_log: Path):
        self.lock = threading.Lock()
        self.requests = self.errors = self.busy = self.not_ready = self.aborted = 0
        self.tv_missed = 0
        self.slow: list[float] = []  # seconds of API requests in the current sample window
        self.error_log = error_log
        self.error_log.write_text("", encoding="utf-8")

    def error(self, what: str) -> None:
        with self.lock:
            self.errors += 1
            with open(self.error_log, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%H:%M:%S')} {what}\n")

    def add(self, name: str, n: int = 1) -> None:
        with self.lock:
            setattr(self, name, getattr(self, name) + n)


class Http:
    def __init__(self, base: str, stats: Stats):
        u = urlsplit(base)
        self.host, self.port = u.hostname, u.port or 80
        self.stats = stats

    def request(self, method: str, path: str, *, body=None, headers=None, read: int | None = None,
                ok=(200,), timeout: float = 60) -> tuple[int, bytes, dict]:
        """``read``: stop after this many bytes and close the connection (a cut-short read)."""
        headers = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        started = time.monotonic()
        try:
            conn.request(method, path, body=data, headers=headers)
            r = conn.getresponse()
            if read is None:
                payload = r.read()
            else:
                payload = r.read(read)
                if not r.isclosed() and len(payload) >= read:
                    self.stats.add("aborted")
            status, rheaders = r.status, {k.lower(): v for k, v in r.getheaders()}
        except (OSError, http.client.HTTPException) as exc:
            self.stats.add("requests")
            self.stats.error(f"{method} {path}: {type(exc).__name__}: {exc}")
            return 0, b"", {}
        finally:
            conn.close()
        self.stats.add("requests")
        if path.startswith("/api/library"):
            with self.stats.lock:
                self.stats.slow.append(time.monotonic() - started)
        if status == 503:
            # "Hub đang bận" (limit of conversions, its body has "limit") or "not ready yet, ask
            # again": both are normal answers, counted apart.
            self.stats.add("busy" if b'"limit"' in payload else "not_ready")
        elif status not in ok:
            self.stats.error(f"{method} {path} -> {status} {payload[:200]!r}")
        return status, payload, rheaders

    def json(self, method: str, path: str, **kw):
        status, payload, _ = self.request(method, path, **kw)
        try:
            return status, json.loads(payload) if payload else None
        except ValueError:
            return status, None


# ============================================================================ devices


class Device(threading.Thread):
    def __init__(self, n: int, http_: Http, items: list[dict], stop: threading.Event, stats: Stats,
                 rescan_every: float):
        super().__init__(name=f"device-{n}", daemon=True)
        self.n, self.http, self.items, self.stop_event, self.stats = n, http_, items, stop, stats
        self.rng = random.Random(1000 + n)
        self.screen: tuple[str, str] | None = None
        self.rescan_every = rescan_every
        self.last_rescan = time.monotonic()

    def pause(self, a: float, b: float) -> None:
        self.stop_event.wait(self.rng.uniform(a, b))

    def run(self) -> None:
        actions = [(self.browse, 3), (self.play, 6), (self.tv, 1)]
        while not self.stop_event.is_set():
            action = self.rng.choices([a for a, _ in actions], [w for _, w in actions])[0]
            try:
                action()
                if self.n == 1 and time.monotonic() - self.last_rescan > self.rescan_every:
                    self.last_rescan = time.monotonic()
                    self.http.request("POST", "/api/rescan", ok=(202,))
            except Exception as exc:  # a bug in this script must not end the run silently
                self.stats.error(f"device {self.n}: {action.__name__} crashed: {type(exc).__name__}: {exc}")
            self.pause(0.5, 2.0)

    # ------------------------------------------------------------------ browsing

    def browse(self) -> None:
        h = self.http
        h.request("GET", "/")
        h.request("GET", "/static/app.css")
        h.request("GET", "/api/hub")
        h.request("GET", f"/api/library?limit=60&sort={self.rng.choice(['path', 'title', 'newest'])}")
        h.request("GET", "/api/folders")
        h.request("GET", "/api/library?q=" + quote(self.rng.choice(["phim", "tap", "hevc", "cuoc chien"])))
        for item in self.rng.sample(self.items, min(3, len(self.items))):
            mid = item["id"]
            status, detail = h.json("GET", f"/api/media/{mid}")
            h.request("GET", f"/thumbnails/{mid}.jpg")
            for track in (detail or {}).get("subtitles", [])[:1]:
                h.request("GET", track["url"])
            self.pause(0.3, 1.5)

    # ------------------------------------------------------------------ playing

    def play(self) -> None:
        item = self.rng.choice([i for i in self.items if i.get("duration")])
        status, plan = self.http.json("POST", f"/api/play/{item['id']}", body=BROWSER, ok=(200,))
        if status != 200 or not plan:
            return  # busy (counted) or an error (logged)
        if plan["mode"] == "direct":
            self.direct(plan["url"])
        else:
            self.hls(plan["url"], plan.get("session"))

    def direct(self, url: str) -> None:
        status, _, headers = self.http.request("HEAD", url)
        size = int(headers.get("content-length", 0))
        if status != 200 or size <= 0:
            return
        for _ in range(self.rng.randint(3, 8)):
            start = self.rng.randrange(0, size)
            self.http.request("GET", url, headers={"Range": f"bytes={start}-"}, ok=(206,),
                              read=self.rng.randint(256 * 1024, 4 * 2**20))
            self.pause(0.2, 1.5)
            if self.stop_event.is_set():
                return

    def _get_ready(self, url: str, tries: int = 4) -> tuple[int, bytes]:
        """A playlist or piece that ffmpeg may still be making: 503 = ask again."""
        for _ in range(tries):
            status, payload, _ = self.http.request("GET", url, ok=(200,))
            if status != 503:
                return status, payload
            self.pause(1, 2)
        return 503, b""

    def hls(self, url: str, sid: str | None) -> None:
        base = url.rsplit("/", 1)[0]
        status, playlist = self._get_ready(url)
        if status != 200:
            return
        status, _ = self._get_ready(f"{base}/init.mp4")
        if status != 200:
            return
        names = [line for line in playlist.decode("utf-8", "replace").splitlines() if line and not line.startswith("#")]
        want = self.rng.randint(3, 15)
        i = 0
        while i < want and not self.stop_event.is_set():
            if i >= len(names):  # a remux still growing: read its playlist again
                self.pause(1, 2)
                status, playlist = self._get_ready(url)
                new = [line for line in playlist.decode("utf-8", "replace").splitlines()
                       if line and not line.startswith("#")]
                if status != 200 or len(new) <= len(names):
                    break
                names = new
                continue
            self._get_ready(f"{base}/{names[i]}")
            i += 1
            self.pause(0.3, 2.0)
        if sid and len(names) > 10 and self.rng.random() < 0.5:  # seek far ahead: ffmpeg restarts there
            j = self.rng.randrange(len(names) // 2, len(names))
            for name in names[j:j + self.rng.randint(2, 6)]:
                self._get_ready(f"{base}/{name}")
                self.pause(0.3, 1.5)
        if sid and self.rng.random() < 0.7:
            self.http.request("POST", f"/api/play/sessions/{sid}/stop", ok=(204,))
        # otherwise abandoned: the hub must end it after transcode_idle_seconds

    # ------------------------------------------------------------------ TV remote

    def tv(self) -> None:
        body = {"name": f"Soak TV {self.n}"}
        if self.screen:
            body.update(id=self.screen[0], key=self.screen[1])
        status, reg = self.http.json("POST", "/api/screens", body=body, ok=(201,))
        if status != 201 or not reg:
            return
        self.screen = (reg["id"], reg["key"])
        conn = http.client.HTTPConnection(self.http.host, self.http.port, timeout=8)
        try:
            conn.request("GET", f"/api/screens/{reg['id']}/events?key={reg['key']}")
            r = conn.getresponse()
            self.stats.add("requests")
            if r.status != 200:
                self.stats.error(f"SSE /api/screens/{reg['id']}/events -> {r.status}")
                return
            r.read1(4096)  # hello
            action = self.rng.choice(["pause", "play", "stop"])
            self.http.request("POST", f"/api/screens/{reg['id']}/commands", body={"action": action}, ok=(202,))
            seen = b""
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline and f'"{action}"'.encode() not in seen:
                chunk = r.read1(4096)
                if not chunk:
                    break
                seen += chunk
            if f'"{action}"'.encode() not in seen:
                self.stats.add("tv_missed")
            self.pause(0.5, 3)
        except (OSError, http.client.HTTPException) as exc:
            self.stats.error(f"SSE: {type(exc).__name__}: {exc}")
        finally:
            conn.close()  # the TV page goes away: the hub must drop the stream


# ============================================================================ own hub + videos


def make_videos(lib: Path, seconds: int, ffmpeg: str) -> None:
    jobs = {
        "Phim/Trực tiếp H264.mp4": ["-c:v", "libx264", "-preset", "ultrafast", "-g", "50", "-pix_fmt", "yuv420p",
                                   "-c:a", "aac"],
        "Phim/Remux H264.mkv": ["-c:v", "libx264", "-preset", "ultrafast", "-g", "50", "-pix_fmt", "yuv420p",
                                "-c:a", "aac"],
        "Phim/Âm thanh AC3.mp4": ["-c:v", "libx264", "-preset", "ultrafast", "-g", "50", "-pix_fmt", "yuv420p",
                                  "-c:a", "ac3"],
        "Phim/HEVC 10-bit.mkv": ["-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=error",
                                 "-pix_fmt", "yuv420p10le", "-c:a", "aac"],
    }
    src = ["-f", "lavfi", "-i", f"testsrc2=duration={seconds}:size=640x360:rate=25",
           "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    for rel, codec in jobs.items():
        out = lib / rel
        if out.is_file():
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name("tmp-" + out.name)
        print(f"  making {rel} ({seconds} s)…", flush=True)
        subprocess.run([ffmpeg, "-y", "-v", "error", *src, *codec, "-shortest", str(tmp)], check=True)
        tmp.replace(out)
    samples = lib / "Mẫu"
    if not samples.exists():
        shutil.copytree(FIXTURES, samples)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_hub(work: Path, lib: Path, idle: int) -> tuple[subprocess.Popen, str]:
    port = free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("HUB_")}
    env.pop("HOUSE_CINEMA_HOME", None)  # the shipped config.yml; index and cache go to `work`
    env.update(HUB_INDEX_PATH=str(work / "index" / "library.db"),
               HUB_CACHE_DIR=str(work / "cache"), HUB_TRANSCODE_IDLE_SECONDS=str(idle),
               HUB_TRANSCODE_CACHE_GB="0",  # finished remuxes are dropped: ffmpeg runs again and again
               PYTHONUNBUFFERED="1")
    cmd = [sys.executable, "-m", "source.pipeline", "--path", str(lib), "--host", "127.0.0.1",
           "--port", str(port), "--log-level", "INFO", "--env-file", str(work / "none.env")]
    log = open(work / "hub.log", "w", encoding="utf-8")
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
    return proc, f"http://127.0.0.1:{port}"


def stop_hub(proc: subprocess.Popen) -> str:
    try:
        if os.name == "nt":
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
        else:
            proc.send_signal(signal.SIGINT)
        proc.wait(timeout=30)
        return f"stopped (exit {proc.returncode})"
    except (OSError, subprocess.TimeoutExpired) as exc:
        proc.kill()
        proc.wait(timeout=10)
        return f"killed after {type(exc).__name__}"


def wait_health(http_: Http, timeout: float = 60) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            conn = http.client.HTTPConnection(http_.host, http_.port, timeout=2)
            conn.request("GET", "/health")
            if conn.getresponse().status == 200:
                return
        except OSError:
            pass
        time.sleep(0.5)
    raise SystemExit("The hub did not answer /health")


# ============================================================================ sampling


class Sampler(threading.Thread):
    COLUMNS = ["time", "elapsed_s", "phase", "rss_mb", "private_mb", "handles", "threads", "ffmpeg",
               "ffprobe", "conversions", "transcode_files", "session_dirs", "half_deleted", "requests",
               "errors", "busy",
               "not_ready", "aborted", "tv_missed", "library_ms_p95"]

    def __init__(self, pid: int, http_: Http, stats: Stats, csv_path: Path, cache: Path | None, every: float):
        super().__init__(name="sampler", daemon=True)
        self.pid, self.http, self.stats, self.cache, self.every = pid, http_, stats, cache, every
        self.rows: list[dict] = []
        self.phase = "warmup"
        self.started = time.monotonic()
        self.stop_event = threading.Event()
        self.ffmpeg_seen: set[int] = set()
        self.file = open(csv_path, "w", newline="", encoding="utf-8")
        self.writer = csv.DictWriter(self.file, self.COLUMNS)
        self.writer.writeheader()

    def sample(self) -> dict:
        procs = processes()
        me = next((p for p in procs if p.pid == self.pid), None)
        kids = [p for p in procs if p.ppid == self.pid]
        ffmpeg = [p for p in kids if is_ffmpeg(p)]
        self.ffmpeg_seen.update(p.pid for p in ffmpeg)
        stats = proc_stats(self.pid)
        conversions = None
        try:
            conn = http.client.HTTPConnection(self.http.host, self.http.port, timeout=5)
            conn.request("GET", "/api/admin/transcode")
            r = conn.getresponse()
            if r.status == 200:
                conversions = json.loads(r.read())["active"]
            conn.close()
        except (OSError, ValueError, KeyError, http.client.HTTPException):
            pass
        files = dirs = half = None
        if self.cache is not None:
            root = self.cache / "transcode"
            files = sum(1 for p in root.rglob("*") if p.is_file()) if root.is_dir() else 0
            dirs = sum(1 for d in root.glob("*/sessions/*") if d.is_dir()) if root.is_dir() else 0
            # A remux folder without meta.json: deleting it failed half way (Windows file in use)
            half = sum(1 for d in root.glob("*/remux-*") if d.is_dir() and not (d / "meta.json").exists()) \
                if root.is_dir() else 0
        with self.stats.lock:
            slow, self.stats.slow = self.stats.slow, []
            row = {"requests": self.stats.requests, "errors": self.stats.errors, "busy": self.stats.busy,
                   "not_ready": self.stats.not_ready, "aborted": self.stats.aborted,
                   "tv_missed": self.stats.tv_missed}
        p95 = round(sorted(slow)[int(len(slow) * 0.95)] * 1000) if slow else None
        row.update(time=time.strftime("%H:%M:%S"), elapsed_s=round(time.monotonic() - self.started),
                   phase=self.phase, rss_mb=_r(stats.get("rss_mb")), private_mb=_r(stats.get("private_mb")),
                   handles=stats.get("handles"), threads=me.threads if me else None, ffmpeg=len(ffmpeg),
                   ffprobe=sum(1 for p in kids if p.name.lower().startswith("ffprobe")),
                   conversions=conversions, transcode_files=files, session_dirs=dirs, half_deleted=half,
                   library_ms_p95=p95)
        return row

    def record(self) -> dict:
        row = self.sample()
        self.rows.append(row)
        self.writer.writerow(row)
        self.file.flush()
        print(f"[{row['time']}] {row['phase']:<7} {row['elapsed_s']:>6}s  RAM {row['rss_mb']} MB  "
              f"handles {row['handles']}  threads {row['threads']}  ffmpeg {row['ffmpeg']}  "
              f"conv {row['conversions']}  files {row['transcode_files']}  req {row['requests']}  "
              f"err {row['errors']}  busy {row['busy']}", flush=True)
        return row

    def run(self) -> None:
        while not self.stop_event.wait(self.every):
            try:
                self.record()
            except Exception as exc:  # keep sampling
                print(f"sampler: {type(exc).__name__}: {exc}", flush=True)


def _r(x):
    return None if x is None else round(x, 1)


# ============================================================================ verdict


def slope_per_hour(rows: list[dict], key: str) -> float | None:
    pts = [(r["elapsed_s"], r[key]) for r in rows if r.get(key) is not None]
    if len(pts) < 3:
        return None
    xs, ys = zip(*pts)
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return None if den == 0 else round(sum((x - mx) * (y - my) for x, y in pts) / den * 3600, 1)


def verdict(sampler: Sampler, stats: Stats, idle_rows: list[dict], gone: list[int], hub_log: Path | None) -> dict:
    load = [r for r in sampler.rows if r["phase"] == "load"]
    q = max(1, len(load) // 4)
    first, last = load[:q], load[-q:]

    def med(rows, key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return statistics.median(vals) if vals else None

    base_h, end_h, base_m, end_m = med(first, "handles"), med(last, "handles"), med(first, "rss_mb"), med(last, "rss_mb")
    final = idle_rows[-1] if idle_rows else {}
    checks = {
        "no request errors": stats.errors == 0,
        "no ffmpeg left when idle": final.get("ffmpeg") == 0,
        "no conversion left when idle": final.get("conversions") in (0, None),
        "no session folder left when idle": final.get("session_dirs") in (0, None),
        "no half-deleted remux folder when idle": final.get("half_deleted") in (0, None),
        "every ffmpeg seen has exited": not gone,
    }
    if base_h is not None and final.get("handles") is not None:
        checks["handles back near start when idle"] = final["handles"] <= base_h * 1.2 + 50
    if base_m is not None and end_m is not None:
        checks["memory flat under load"] = end_m - base_m <= max(60.0, base_m * 0.3)
    log_errors = delete_retries = 0
    if hub_log is not None and hub_log.is_file():
        text = hub_log.read_text(encoding="utf-8", errors="replace")
        log_errors = len(re.findall(r"\bERROR\b|Traceback", text))
        delete_retries = text.count("trying again later")
        checks["no ERROR / Traceback in hub log"] = log_errors == 0
        checks["no folder left undeleted (hub log)"] = not re.search(
            r"WARNING\s+house_cinema\.transcode: Could not delete", text)
    return {
        "verdict": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "requests": stats.requests, "errors": stats.errors, "busy": stats.busy, "not_ready": stats.not_ready,
        "aborted_reads": stats.aborted, "tv_commands_missed": stats.tv_missed,
        "ffmpeg_processes_started": len(sampler.ffmpeg_seen), "ffmpeg_still_alive": gone,
        "hub_log_error_lines": log_errors, "folder_deletes_retried_later": delete_retries,
        "handles": {"load_start": base_h, "load_end": end_h, "idle_end": final.get("handles"),
                    "slope_per_hour_under_load": slope_per_hour(load, "handles")},
        "rss_mb": {"load_start": base_m, "load_end": end_m, "idle_end": final.get("rss_mb"),
                   "slope_per_hour_under_load": slope_per_hour(load, "rss_mb")},
        "threads": {"load_start": med(first, "threads"), "load_end": med(last, "threads"),
                    "idle_end": final.get("threads")},
        "max_ffmpeg_at_once": max((r["ffmpeg"] for r in sampler.rows), default=0),
    }


# ============================================================================ main


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--minutes", type=float, default=60, help="Time under load (default 60).")
    ap.add_argument("--devices", type=int, default=4)
    ap.add_argument("--sample", type=float, default=15, help="Seconds between resource samples.")
    ap.add_argument("--warmup", type=float, default=3, help="Minutes before the load samples count.")
    ap.add_argument("--work", type=Path, default=DEFAULT_WORK, help=f"Output folder (default {DEFAULT_WORK}).")
    ap.add_argument("--video-seconds", type=int, default=600, help="Length of each generated video.")
    ap.add_argument("--idle-seconds", type=int, default=20, help="transcode_idle_seconds of the own hub.")
    ap.add_argument("--rescan-minutes", type=float, default=10)
    ap.add_argument("--hub", help="URL of a running hub (then no own hub, no generated videos).")
    ap.add_argument("--process", help="With --hub: process name to measure, e.g. HouseCinema.exe.")
    ap.add_argument("--pid", type=int, help="With --hub: process id to measure.")
    ap.add_argument("--cache-dir", type=Path, help="With --hub: the hub's cache folder (to count files).")
    args = ap.parse_args(argv)

    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    stats = Stats(work / "errors.log")
    proc = None
    if args.hub:
        base, cache = args.hub.rstrip("/"), args.cache_dir
        idle = 90  # unknown: give the hub's own idle timeout (60 s by default) time to act
    else:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise SystemExit("ffmpeg is needed to make the test videos")
        print("Test videos:", flush=True)
        make_videos(work / "library", args.video_seconds, ffmpeg)
        for leftover in ("index", "cache", "home"):
            shutil.rmtree(work / leftover, ignore_errors=True)
        proc, base = start_hub(work, work / "library", args.idle_seconds)
        cache, idle = work / "cache", args.idle_seconds + 15
    http_ = Http(base, stats)
    try:
        wait_health(http_)
        if proc is not None:
            pid = follow_launcher(proc.pid)
        elif args.pid:
            pid = args.pid
        elif args.process:
            pid = find_by_name(args.process)
        else:
            raise SystemExit("With --hub, give --process or --pid so the hub can be measured")
        print(f"Hub {base}, measuring pid {pid}", flush=True)

        if proc is not None:  # index the generated library (thumbnails, details) first
            http_.request("POST", "/api/rescan", ok=(202,))
            end = time.monotonic() + 600
            while time.monotonic() < end:
                _, st = http_.json("GET", "/api/rescan")
                if st and st["state"] in ("done", "failed"):
                    print(f"Rescan: {st['state']} {st.get('counts') or st.get('error')}", flush=True)
                    break
                time.sleep(1)
        items, offset = [], 0
        while offset is not None:
            _, page = http_.json("GET", f"/api/library?limit=200&offset={offset}")
            items += page["items"]
            offset = page["next_offset"]
        if not items:
            raise SystemExit("The library is empty")
        print(f"{len(items)} videos; {args.devices} devices for {args.minutes:g} min", flush=True)

        sampler = Sampler(pid, http_, stats, work / "resources.csv", cache, args.sample)
        sampler.record()
        sampler.start()
        stop = threading.Event()
        devices = [Device(n + 1, http_, items, stop, stats, args.rescan_minutes * 60) for n in range(args.devices)]
        for d in devices:
            d.start()
            time.sleep(1)
        started = time.monotonic()
        try:
            while time.monotonic() - started < args.minutes * 60:
                if time.monotonic() - started > args.warmup * 60:
                    sampler.phase = "load"
                if proc is not None and proc.poll() is not None:
                    stats.error(f"the hub exited with code {proc.returncode}")
                    break
                time.sleep(1)
        except KeyboardInterrupt:
            print("Interrupted: stopping the devices", flush=True)
        stop.set()
        for d in devices:
            d.join(timeout=90)
        sampler.phase = "idle"
        print(f"Load over; waiting {idle} s for the hub to stop idle conversions…", flush=True)
        time.sleep(idle)
        sampler.stop_event.set()
        sampler.join(timeout=30)
        idle_rows = []
        for _ in range(3):
            idle_rows.append(sampler.record())
            time.sleep(2)
        # By parent, not by pid: Windows reuses pids (another hub's ffmpeg was taken for ours).
        # A child keeps its parent pid on Windows even after the parent has exited.
        gone_wrong = sorted(p.pid for p in processes() if is_ffmpeg(p) and p.ppid == pid)
    finally:
        hub_state = stop_hub(proc) if proc is not None else None
    if proc is not None:  # after the hub stops, none of its ffmpeg may survive
        time.sleep(2)
        gone_wrong = sorted(set(gone_wrong) | {p.pid for p in processes() if is_ffmpeg(p) and p.ppid == pid})
    summary = verdict(sampler, stats, idle_rows, gone_wrong, work / "hub.log" if proc is not None else None)
    summary.update(minutes=args.minutes, devices=args.devices, videos=len(items), hub=base, hub_stop=hub_state,
                   finished=time.strftime("%Y-%m-%d %H:%M:%S"))
    (work / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"\nFiles: {work}")
    return 0 if summary["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
