"""Find the file to serve for a media ID, from the library index.

The server only ever receives IDs; the path comes from the index (plan step 3.2 adds the
"must be inside the configured library folders" check on top of this).
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import re
import threading
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from source.modules.library.index import LibraryIndex
from source.modules.library.scanner import locate
from source.schemas.media import MediaItem

logger = logging.getLogger("house_cinema.server")

MEDIA_ID = re.compile(r"[0-9a-f]{16}")

# What browsers expect. Python's mimetypes on Windows reads the registry, which may say
# e.g. "video/x-matroska" or nothing at all, depending on what is installed.
VIDEO_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".ts": "video/mp2t",
    ".m2ts": "video/mp2t",
    ".wmv": "video/x-ms-wmv",
    ".flv": "video/x-flv",
    ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg",
    ".3gp": "video/3gpp",
    ".ogv": "video/ogg",
}


def content_type_for(extension: str) -> str:
    ext = extension.lower()
    return VIDEO_TYPES.get(ext) or mimetypes.guess_type(f"x{ext}")[0] or "application/octet-stream"


@dataclass(frozen=True)
class StreamSource:
    path: Path
    size: int
    mtime_ns: int
    name: str
    """Display name (NFC, as in the index); ``path`` has the name as written on disk."""
    content_type: str


class MediaNotFound(Exception):
    """No file to serve. ``reason`` is for the log only, never sent to the client."""

    def __init__(self, reason: str, *, suspicious: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.suspicious = suspicious
        """True when this looks like an attempt to reach a file outside the library (logged
        as a warning), False for ordinary misses such as a stale ID or an unplugged drive."""


def _key(path: str) -> str:
    """Comparable form of a path: absolute, real (links resolved), case/NFC-normalized."""
    return unicodedata.normalize("NFC", os.path.normcase(os.path.realpath(path)))


def check_root(library_root: str, allowed_roots: Sequence[str]) -> str:
    """The comparable key of ``library_root`` if it is a configured library folder."""
    root_key = _key(library_root)
    if root_key not in {_key(r) for r in allowed_roots}:
        raise MediaNotFound(f"library folder is not in the current config: {library_root}",
                            suspicious=True)
    return root_key


def check_relative(relative_path: str) -> list[str]:
    """The parts of an indexed relative path; refuses '..', '.', empty parts (a leading '/'),
    backslashes, drive letters and NUL. Must run before the path is used at all."""
    parts = relative_path.split("/")
    if (not relative_path or any(p in ("", ".", "..") or "\\" in p or ":" in p or "\0" in p
                                 for p in parts)):
        raise MediaNotFound(f"invalid path in the index: {relative_path!r}", suspicious=True)
    return parts


def check_inside(library_root: str, relative_path: str, on_disk: Path,
                 allowed_roots: Sequence[str]) -> Path:
    """Plan step 3.2: return the real path of ``on_disk`` if it is the indexed file inside an
    allowed library folder, reached without any link. Raises MediaNotFound(suspicious=True).

    - ``library_root`` must be one of the configured folders (the index can still hold a
      folder removed from the config, until the next scan drops it).
    - The real path (all symlinks/junctions resolved) must be inside that folder and equal
      to root + relative_path: a file or folder swapped for a link after the scan, pointing
      inside or outside the library, is refused, like the scanner refuses links.
    - ``relative_path`` comes from the index, never from the request; it is still checked
      for '..', drive letters and absolute parts in case the index file was tampered with.
    """
    root_key = check_root(library_root, allowed_roots)
    parts = check_relative(relative_path)
    real = _key(str(on_disk))
    expected = unicodedata.normalize("NFC", os.path.normcase(os.path.join(root_key, *parts)))
    if os.path.commonpath([root_key, real]) != root_key:
        raise MediaNotFound(f"resolves outside the library: {on_disk} -> {real}", suspicious=True)
    if real != expected:
        raise MediaNotFound(f"reached through a link: {on_disk} -> {real}", suspicious=True)
    return Path(os.path.realpath(on_disk))


def fold_text(text: str) -> str:
    """Lower case, no marks: 'Cuộc Chiến Đêm' → 'cuoc chien dem' (for search)."""
    text = unicodedata.normalize("NFD", text.replace("đ", "d").replace("Đ", "D"))
    return "".join(c for c in text if not unicodedata.combining(c)).casefold()


def library_id(root_key: str) -> str:
    return hashlib.sha1(root_key.encode("utf-8")).hexdigest()[:8]


def library_name(path: str) -> str:
    """'F:\\video\\ririsu' → 'ririsu'; a drive root 'F:\\' → 'F:'."""
    return Path(path).name or path.rstrip("\\/") or path


@dataclass(frozen=True)
class Library:
    id: str
    name: str
    path: str
    """Server path: for logs and the server's own use, never sent to clients."""
    available: bool


@dataclass(frozen=True)
class _Snapshot:
    version: tuple
    """(index data_version, configured folders) the snapshot was made for."""
    items: list[MediaItem]
    libraries: dict[str, Library]
    """root_key → Library, configured folders only."""
    search: dict[str, str]
    """id → folded 'title folder' text."""


class MediaCatalog:
    """Thread-safe, read-only view of the index for the HTTP handlers.

    Only files inside ``library_roots`` (the configured library folders) are ever served.
    ``library_roots`` may be a function: it is then asked on every request, so folders
    changed on the admin page (step 4.2) apply at once.
    """

    def __init__(self, index_path: Path,
                 library_roots: Sequence[str | Path] | Callable[[], Sequence[str | Path]]):
        if callable(library_roots):
            self._roots_of: Callable[[], Sequence[str | Path]] = library_roots
        else:
            fixed = list(library_roots)
            self._roots_of = lambda: fixed
        self._index = LibraryIndex(index_path, any_thread=True)
        self._lock = threading.Lock()
        self._cache: _Snapshot | None = None
        with self._lock:
            count = len(self._index.known_files())
        if self._index.recovered_from is not None:
            logger.warning("The index was damaged and a new one was started; run `scan` to fill it.")
        if count == 0:
            logger.warning("The library index %s is empty: no video yet. Add library folders on the "
                           "admin page, then press “Quét lại” on the library page.", index_path)
        else:
            logger.info("Library index: %d videos (%s)", count, index_path)

    def close(self) -> None:
        with self._lock:
            self._index.close()

    # ------------------------------------------------------------- listing (API)

    @property
    def _roots(self) -> list[str]:
        return [str(r) for r in self._roots_of()]

    def _snapshot(self) -> _Snapshot:
        """Items and libraries of the configured folders, reloaded only after a scan commits
        or when the configured folders change."""
        configured = tuple(self._roots)
        with self._lock:
            version = (self._index.data_version(), configured)
            if self._cache is not None and self._cache.version == version:
                return self._cache
            roots = self._index.roots()
            items = self._index.items()
        allowed = {_key(r) for r in configured}
        libraries = {r.key: Library(id=library_id(r.key), name=library_name(r.path), path=r.path,
                                    available=r.available)
                     for r in roots if _key(r.path) in allowed}
        listed = [i for i in items if i.root_key in libraries]
        self._cache = _Snapshot(version, listed, libraries,
                                {i.id: fold_text(f"{i.title} {i.folder}") for i in listed})
        return self._cache

    def libraries(self) -> list[Library]:
        return list(self._snapshot().libraries.values())

    def library_of(self, item: MediaItem) -> Library:
        found = self._snapshot().libraries.get(item.root_key)
        if found is None:  # the folders changed between two calls of one request
            found = Library(id=library_id(item.root_key), name=library_name(item.library_root),
                            path=item.library_root, available=item.available)
        return found

    def video_counts(self) -> dict[str, int]:
        """Videos in the index per library folder path (normalized key), configured or not."""
        with self._lock:
            roots = self._index.roots()
        return {_key(r.path): r.items for r in roots}

    def items(self, query: str = "") -> list[MediaItem]:
        """Videos of the configured folders, in library order; ``query`` matches title and
        folder ignoring case and Vietnamese marks ('cuoc chien' finds 'Cuộc Chiến')."""
        snap = self._snapshot()
        words = fold_text(query).split()
        if not words:
            return list(snap.items)
        return [i for i in snap.items if all(w in snap.search[i.id] for w in words)]

    def item(self, media_id: str) -> MediaItem:
        """An indexed video of a configured folder (no disk access). Raises MediaNotFound."""
        if not MEDIA_ID.fullmatch(media_id):
            raise MediaNotFound("not a media ID", suspicious=True)
        snap = self._snapshot()
        found = next((i for i in snap.items if i.id == media_id), None)
        if found is None:
            raise MediaNotFound("ID not in the index (or its folder is not configured)")
        return found

    def source(self, media_id: str) -> StreamSource:
        """The file behind ``media_id``, checked on disk now. Raises MediaNotFound."""
        if not MEDIA_ID.fullmatch(media_id):
            # The API only takes IDs; anything else (a path, '..', 'C:\...') is a probe.
            raise MediaNotFound("not a media ID", suspicious=True)
        with self._lock:
            item = self._index.get(media_id)
        if item is None:
            raise MediaNotFound("ID not in the index")
        if not item.available:
            raise MediaNotFound(f"library folder not connected: {item.library_root}")
        check_root(item.library_root, self._roots)
        check_relative(item.relative_path)
        path = locate(item.library_root, item.relative_path)
        if not os.path.lexists(path):
            raise MediaNotFound(f"file no longer exists: {path}")
        # Open the checked real path, not the link-able one we started from.
        path = check_inside(item.library_root, item.relative_path, path, self._roots)
        try:
            st = os.stat(path)
        except OSError as exc:
            raise MediaNotFound(f"file not readable ({exc.strerror or exc}): {path}") from None
        if not os.path.isfile(path):
            raise MediaNotFound(f"not a regular file: {path}", suspicious=True)
        # Size/mtime from the disk, not the index: the file may have changed since the scan.
        return StreamSource(path=path, size=st.st_size, mtime_ns=st.st_mtime_ns,
                            name=item.name, content_type=content_type_for(item.extension))
