"""Rescan the library from the server (POST /api/rescan), in a background thread.

Same work as the `scan` command: list the folders, update the index, then read details and
make thumbnails for new/changed files. Streams are not affected: they hold their own open
file and the index is SQLite in WAL mode, so readers never wait for the scan's writes.
Only one rescan runs at a time; asking again while one runs returns its status.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from source.modules.library.folders import LibraryFolders
from source.modules.library.index import LibraryIndex
from source.modules.library.media_info import process_media
from source.modules.library.scanner import scan_library
from source.schemas.api import RescanCounts, RescanStatus
from source.schemas.settings import Settings
from source.utils.ffmpeg import find_tools

logger = logging.getLogger("house_cinema.server.rescan")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Rescanner:
    def __init__(self, settings: Settings, folders: LibraryFolders | None = None):
        """``folders``: the current library folders (admin page, step 4.2); each rescan uses
        the list as it is when it starts. Without it, ``settings.library_paths``."""
        self._settings = settings
        self._folders = folders
        self._lock = threading.Lock()
        self._status = RescanStatus(state="idle")
        self._thread: threading.Thread | None = None

    def status(self) -> RescanStatus:
        with self._lock:
            return self._status.model_copy()

    def _update(self, **fields) -> None:
        with self._lock:
            self._status = self._status.model_copy(update=fields)

    def start(self) -> RescanStatus:
        """Start a rescan unless one is running. Returns the (new or running) status."""
        with self._lock:
            if self._status.state == "running":
                return self._status.model_copy(update={"started": False})
            self._status = RescanStatus(state="running", phase="scanning", progress_done=0,
                                        started_at=_now())
            self._thread = threading.Thread(target=self._run, name="rescan", daemon=True)
            self._thread.start()
            return self._status.model_copy(update={"started": True})

    def wait(self, timeout: float | None = None) -> RescanStatus:
        """Block until the current rescan ends (for tests and shutdown)."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return self.status()

    def _run(self) -> None:
        s = self._settings
        started = time.perf_counter()
        logger.info("Rescan started")
        try:
            roots = self._folders.paths() if self._folders is not None else list(s.library_paths)
            with LibraryIndex(s.index_path) as index:
                result = scan_library(
                    roots, s.video_extensions, known_files=index.known_files(),
                    on_progress=lambda p: self._update(progress_done=p.videos_found),
                )
                diff = index.apply_scan(result)
                self._update(phase="media", progress_done=0, progress_total=None)
                media = process_media(
                    index, find_tools(s), s.cache_dir, workers=s.media_workers, renamed=diff.renamed,
                    on_progress=lambda p: self._update(progress_done=p.done, progress_total=p.total),
                )
            counts = RescanCounts(
                found=result.report.found, added=len(diff.added), removed=len(diff.removed),
                renamed=len(diff.renamed), changed=len(diff.changed),
                unavailable_libraries=len(result.report.unavailable_roots),
                errors=len(result.report.errors), info_read=media.read_ok, info_failed=media.read_failed,
                thumbnails_created=media.thumbs_created,
            )
            self._update(state="done", phase=None, finished_at=_now(), counts=counts)
            logger.info("Rescan done in %.1f s: %s; %s", time.perf_counter() - started,
                        diff.summary() if not diff.is_empty() else "no changes", media.summary())
        except Exception as exc:  # report it in the status; the server keeps running
            logger.exception("Rescan failed")
            # The message can hold server paths: details go to the log only.
            self._update(state="failed", phase=None, finished_at=_now(),
                         error=f"{type(exc).__name__} (details in the server log)")
