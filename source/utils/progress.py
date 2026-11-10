"""A single, self-overwriting progress line for the terminal.

Written to stderr with a carriage return (no ANSI codes), so it works in Windows
Terminal, the classic console, VS Code and Git Bash (mintty). Results printed to stdout
stay clean when redirected (`> scan.txt`).
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import time
from collections.abc import Callable, Mapping
from typing import TextIO

ELLIPSIS = "…"
SEPARATOR = " · "


def stream_is_terminal(stream: TextIO, environ: Mapping[str, str] | None = None) -> bool:
    """True if ``stream`` is shown to a person (worth drawing progress on).

    Git Bash / MSYS2 (mintty) talks to programs through pipes, so ``isatty()`` is False
    there even though a person is watching. Under MSYSTEM we therefore also accept a pipe;
    a redirect to a file (`2> log.txt`) is still detected as "not a terminal".
    """
    environ = os.environ if environ is None else environ
    try:
        if stream.isatty():
            return True
    except (AttributeError, ValueError):
        return False
    if os.name == "nt" and environ.get("MSYSTEM"):
        try:
            mode = os.fstat(stream.fileno()).st_mode
        except (AttributeError, OSError, ValueError):
            return False
        return stat.S_ISFIFO(mode)
    return False


def shorten_left(text: str, width: int) -> str:
    """Keep the end of ``text`` (the informative part of a path) within ``width`` chars."""
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return ELLIPSIS
    return ELLIPSIS + text[-(width - 1):]


class _ClearBeforeLog(logging.Filter):
    """Erase the progress line before a log record is written to the same stream."""

    def __init__(self, line: ProgressLine):
        super().__init__()
        self.line = line

    def filter(self, record: logging.LogRecord) -> bool:
        self.line.clear()
        return True


class ProgressLine:
    """Draw ``text · tail`` on one line, rewriting it in place.

    - Nothing is drawn during the first ``start_delay`` seconds, so fast runs do not flicker.
    - Redraws are throttled to one per ``min_interval`` seconds.
    - ``tail`` (e.g. the current folder) is shortened from the left to fit the terminal.
    - Used as a context manager, it clears itself on exit and keeps log records written to
      the same stream from colliding with the line.
    """

    def __init__(
        self,
        stream: TextIO,
        *,
        enabled: bool,
        start_delay: float = 0.5,
        min_interval: float = 0.1,
        width: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.stream = stream
        self.enabled = enabled
        self.start_delay = start_delay
        self.min_interval = min_interval
        self._width = width
        self._clock = clock
        self._started = clock()
        self._last_draw: float | None = None
        self._drawn_len = 0
        self._filters: list[tuple[logging.Handler, logging.Filter]] = []

    @property
    def width(self) -> int:
        if self._width is not None:
            return self._width
        # One column less than the terminal so the cursor never wraps to a new line.
        return max(shutil.get_terminal_size(fallback=(80, 24)).columns - 1, 10)

    def update(self, text: str, tail: str = "") -> None:
        if not self.enabled:
            return
        now = self._clock()
        if now - self._started < self.start_delay:
            return
        if self._last_draw is not None and now - self._last_draw < self.min_interval:
            return
        self._draw(text, tail)
        self._last_draw = now

    def _draw(self, text: str, tail: str) -> None:
        width = self.width
        line = text[:width]
        if tail:
            room = width - len(line) - len(SEPARATOR)
            if room >= 8:  # not worth showing a 3-character path fragment
                line += SEPARATOR + shorten_left(tail, room)
        pad = " " * max(self._drawn_len - len(line), 0)
        self.stream.write("\r" + line + pad)
        self.stream.flush()
        self._drawn_len = len(line)

    def clear(self) -> None:
        if self._drawn_len:
            self.stream.write("\r" + " " * self._drawn_len + "\r")
            self.stream.flush()
            self._drawn_len = 0
            self._last_draw = None  # redraw at the next update

    def __enter__(self) -> ProgressLine:
        if self.enabled:
            for handler in logging.getLogger().handlers:
                if getattr(handler, "stream", None) is self.stream:
                    log_filter = _ClearBeforeLog(self)
                    handler.addFilter(log_filter)
                    self._filters.append((handler, log_filter))
        return self

    def __exit__(self, *exc_info) -> None:
        for handler, log_filter in self._filters:
            handler.removeFilter(log_filter)
        self._filters.clear()
        self.clear()
