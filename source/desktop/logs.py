"""Everything the hub prints (logs, the address and QR code) goes to a log file and to a
buffer the console window shows. The packaged app has no terminal: on Windows a windowed
program's sys.stdout / sys.stderr are None, so they are replaced by LogStream."""

from __future__ import annotations

import io
import sys
import threading
from collections import deque
from pathlib import Path

LOG_NAME = "house-cinema.log"
MAX_LINES = 5000


class LogBuffer:
    """Thread-safe store of the last MAX_LINES lines, plus a counter so a reader can ask
    for "what is new since line N"."""

    def __init__(self, max_lines: int = MAX_LINES):
        self._lines: deque[str] = deque(maxlen=max_lines)
        self._total = 0  # lines ever added
        self._partial = ""
        self._lock = threading.Lock()

    def add(self, text: str) -> None:
        with self._lock:
            text = self._partial + text.replace("\r\n", "\n").replace("\r", "\n")
            *lines, self._partial = text.split("\n")
            for line in lines:
                self._lines.append(line)
                self._total += 1

    def since(self, seen: int) -> tuple[list[str], int]:
        """Lines added after the first ``seen`` ones (those still kept), and the new count."""
        with self._lock:
            new = min(self._total - seen, len(self._lines))
            lines = list(self._lines)[len(self._lines) - new:] if new > 0 else []
            return lines, self._total


class LogStream(io.TextIOBase):
    """A text stream writing to the log file (when it could be opened) and to a LogBuffer."""

    def __init__(self, buffer: LogBuffer, file: io.TextIOBase | None):
        super().__init__()
        self._buffer = buffer
        self._file = file
        self._lock = threading.Lock()

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return "utf-8"

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return False

    def write(self, text: str) -> int:
        if not text:
            return 0
        self._buffer.add(text)
        if self._file is not None:
            with self._lock:
                try:
                    self._file.write(text)
                    self._file.flush()
                except (OSError, ValueError):
                    self._file = None  # disk full / file closed: keep the window going
        return len(text)

    def flush(self) -> None:
        pass


def open_log(folder: Path) -> tuple[Path, io.TextIOBase | None]:
    """Open <folder>/house-cinema.log for this run; the previous run's log is kept as
    house-cinema.previous.log."""
    path = folder / LOG_NAME
    try:
        folder.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.replace(folder / "house-cinema.previous.log")
        return path, open(path, "w", encoding="utf-8", errors="replace", buffering=1)  # noqa: SIM115
    except OSError:
        return path, None


def capture_output(folder: Path) -> tuple[LogBuffer, Path]:
    """Send sys.stdout and sys.stderr to the log file and a new LogBuffer."""
    buffer = LogBuffer()
    path, file = open_log(folder)
    stream = LogStream(buffer, file)
    sys.stdout = stream  # type: ignore[assignment]
    sys.stderr = stream  # type: ignore[assignment]
    return buffer, path
