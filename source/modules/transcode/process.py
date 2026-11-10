"""Run one ffmpeg process in the background, and make sure it never outlives the hub.

On Windows every ffmpeg is put in a Job Object with "kill on close": if the hub process
ends in any way (Ctrl+C, crash, closed console), Windows stops its ffmpeg processes too.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path

logger = logging.getLogger("house_cinema.transcode")

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_job_lock = threading.Lock()
_job_handle = None


def _windows_job():
    """A Job Object that kills its processes when the hub exits (None if unavailable)."""
    global _job_handle
    if os.name != "nt":
        return None
    with _job_lock:
        if _job_handle is not None:
            return _job_handle or None
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE

            class BasicLimits(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class IoCounters(ctypes.Structure):
                _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOps", "WriteOps", "OtherOps", "ReadBytes",
                                                           "WriteBytes", "OtherBytes")]

            class ExtendedLimits(ctypes.Structure):
                _fields_ = [("Basic", BasicLimits), ("Io", IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t),
                            ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                            ("PeakJobMemoryUsed", ctypes.c_size_t)]

            job = kernel32.CreateJobObjectW(None, None)
            if not job:
                raise OSError(ctypes.get_last_error())
            limits = ExtendedLimits()
            limits.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise OSError(ctypes.get_last_error())
            _job_handle = (kernel32, job)
        except Exception as exc:  # never stop playback for this safety net
            logger.warning("ffmpeg processes may outlive the hub if it crashes (no Job Object: %s)", exc)
            _job_handle = False
        return _job_handle or None


class Encoder:
    """One ffmpeg run; its messages go to ``log_path``."""

    def __init__(self, args: list[str], log_path: Path, *, label: str):
        self.label = label
        self.args = args
        self.log_path = log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = open(log_path, "ab")
        try:
            self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=self._log, creationflags=_NO_WINDOW)
        except OSError:
            self._log.close()
            raise
        job = _windows_job()
        if job is not None:
            kernel32, handle = job
            kernel32.AssignProcessToJobObject(handle, int(self.proc._handle))  # type: ignore[attr-defined]
        logger.info("ffmpeg started (pid %s): %s", self.proc.pid, label)

    @property
    def pid(self) -> int:
        return self.proc.pid

    def running(self) -> bool:
        return self.proc.poll() is None

    @property
    def returncode(self) -> int | None:
        return self.proc.poll()

    def error_text(self) -> str:
        """Last meaningful line ffmpeg printed (for the log and a readable error)."""
        try:
            lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        lines = [line.strip() for line in lines if line.strip()]
        if not lines:
            return f"exit code {self.returncode}"
        line = lines[-1]
        if line.startswith("[") and "] " in line:
            line = line.split("] ", 1)[1]
        return line[:200]

    def stop(self, why: str = "") -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                logger.error("ffmpeg pid %s did not stop", self.proc.pid)
            logger.info("ffmpeg stopped (pid %s)%s: %s", self.proc.pid, f", {why}" if why else "", self.label)
        self.close()

    def close(self) -> None:
        if not self._log.closed:
            self._log.close()
