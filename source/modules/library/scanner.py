"""Walk the library folders and list video files.

Rules:
- Only files whose extension is in ``extensions`` (case-insensitive) are listed.
- Hidden/system files and folders are skipped: names starting with '.', Windows
  hidden/system attributes, '$RECYCLE.BIN', 'System Volume Information'.
- Symlinks and junctions are never followed. A link to a folder inside the library
  would list the same files twice, a link outside the library breaks the
  "only serve what is in the library" rule (plan step 3.2), and a link to a parent
  folder is a loop. Each such link is reported as skipped with the reason.
- A folder or file that cannot be read is reported as an error; the scan goes on.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import stat
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from source.schemas.media import MediaItem, ScanIssue, ScanReport, ScanResult

logger = logging.getLogger("house_cinema.library")

SYSTEM_FOLDER_NAMES = {"$recycle.bin", "system volume information", "recycler"}
_HIDDEN = getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0x2)
_SYSTEM = getattr(stat, "FILE_ATTRIBUTE_SYSTEM", 0x4)
_DIGITS = re.compile(r"(\d+)")


def natural_key(text: str) -> tuple:
    """Sort key so that 'Tập 2' comes before 'Tập 10'."""
    parts = _DIGITS.split(unicodedata.normalize("NFC", text).casefold())
    return tuple(int(p) if i % 2 else p for i, p in enumerate(parts))


def root_key(library_root: str | Path) -> str:
    """Identity of a library folder that does not depend on the drive letter.

    Windows: volume serial number (``st_dev``) + the path inside that volume, so an external
    drive that comes back as G: instead of F: keeps the same key, and therefore the same
    media IDs. Elsewhere ``st_dev`` is not stable across reboots, so the real path is used.
    Raises OSError if the folder is not reachable.
    """
    real = os.path.realpath(library_root)
    if os.name == "nt":
        serial = os.stat(real).st_dev
        inside = os.path.normcase(os.path.splitdrive(real)[1]).rstrip("\\") or "\\"
        key = f"vol:{serial:x}:{inside}"
    else:
        key = f"path:{real}"
    return unicodedata.normalize("NFC", key)


def media_id(key: str, relative_path: str) -> str:
    """Stable ID from a root key (see ``root_key``) and the '/'-separated path inside the root.

    Both parts are NFC-normalized, so the same file always gets the same ID.
    """
    raw = unicodedata.normalize("NFC", f"{key}\0{relative_path}")
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def locate(library_root: str | Path, relative_path: str) -> Path:
    """The real on-disk path of an indexed file.

    The index stores names NFC-normalized (stable IDs, consistent display), but the file
    system keeps names exactly as they were written: a name copied from macOS or some
    downloaders is NFD (e.g. Japanese ボ stored as ホ + U+3099, Vietnamese ế as e + marks).
    Windows/NTFS does not treat the two forms as the same name, so the NFC path does not
    open. Each part of the path is matched against the folder listing by its NFC form.
    If nothing matches, the plain joined path is returned (and opening it fails as usual).
    """
    parts = relative_path.split("/")
    direct = Path(library_root, *parts)
    if direct.exists():
        return direct
    current = Path(library_root)
    for part in parts:
        candidate = current / part
        if not candidate.exists():
            try:
                match = next((n for n in os.listdir(current)
                              if unicodedata.normalize("NFC", n) == part), None)
            except OSError:
                return direct
            if match is None:
                return direct
            candidate = current / match
        current = candidate
    return current


def _is_link(entry: os.DirEntry) -> bool:
    if entry.is_symlink():
        return True
    is_junction = getattr(entry, "is_junction", None)  # Python 3.12+
    return bool(is_junction and is_junction())


def _hidden_reason(entry: os.DirEntry) -> str | None:
    if entry.name.startswith("."):
        return "hidden (name starts with '.')"
    if entry.name.casefold() in SYSTEM_FOLDER_NAMES:
        return "system folder"
    attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    if attrs & _SYSTEM:
        return "system file/folder (Windows attribute)"
    if attrs & _HIDDEN:
        return "hidden (Windows attribute)"
    return None


@dataclass(frozen=True)
class ScanProgress:
    """Snapshot sent to the ``on_progress`` callback while a scan runs."""

    folders_scanned: int
    videos_found: int
    skipped: int
    errors: int
    current_folder: str
    """Absolute path of the folder being scanned."""


ProgressCallback = Callable[[ScanProgress], None]

# Report progress at least every N entries inside one large folder, not only per folder.
PROGRESS_EVERY_ENTRIES = 100


KnownFiles = Mapping[str, tuple[int, float]]
"""media id -> (size, mtime) from the previous scan."""


@dataclass
class _Scan:
    extensions: frozenset[str]
    real_roots: list[str]
    on_progress: ProgressCallback | None = None
    known_files: KnownFiles | None = None
    items: list[tuple[int, MediaItem]] = field(default_factory=list)
    skipped: list[ScanIssue] = field(default_factory=list)
    errors: list[ScanIssue] = field(default_factory=list)
    empty_folders: int = 0
    folders_scanned: int = 0

    def report_progress(self, current_folder: str) -> None:
        if self.on_progress is None:
            return
        self.on_progress(ScanProgress(
            folders_scanned=self.folders_scanned,
            videos_found=len(self.items),
            skipped=len(self.skipped),
            errors=len(self.errors),
            current_folder=current_folder,
        ))

    def skip(self, path: str, reason: str) -> None:
        logger.debug("Skipped %s: %s", path, reason)
        self.skipped.append(ScanIssue(path=path, reason=reason))

    def error(self, path: str, reason: str) -> None:
        logger.warning("Cannot read %s: %s", path, reason)
        self.errors.append(ScanIssue(path=path, reason=reason))

    def is_video_name(self, name: str) -> bool:
        return os.path.splitext(name)[1].lower() in self.extensions

    def _inside_library(self, real: str) -> bool:
        real_nc = os.path.normcase(real)
        for root in self.real_roots:
            root_nc = os.path.normcase(root)
            if real_nc == root_nc or real_nc.startswith(root_nc.rstrip(os.sep) + os.sep):
                return True
        return False

    def link_reason(self, entry: os.DirEntry, current_real_dir: str) -> str | None:
        """Why a link is skipped, or None if it is not worth reporting (link to a non-video file)."""
        target = os.path.realpath(entry.path)
        if not os.path.exists(target):
            return "broken link (target does not exist)"
        is_dir = os.path.isdir(target)
        if not is_dir and not self.is_video_name(entry.name):
            return None
        cur = os.path.normcase(current_real_dir)
        tgt = os.path.normcase(target)
        if is_dir and (cur == tgt or cur.startswith(tgt.rstrip(os.sep) + os.sep)):
            return f"link loop (points to parent folder {target})"
        if self._inside_library(target):
            what = "folder" if is_dir else "file"
            return f"link to a {what} already in the library ({target})"
        return f"link points outside the library ({target}); add that folder with --path instead"

    def scan_root(self, root_index: int, root: Path, key: str) -> None:
        root_real = os.path.realpath(root)
        root_str = str(root)
        # (absolute path, real path, relative parts)
        stack: list[tuple[str, str, tuple[str, ...]]] = [(root_str, root_real, ())]
        while stack:
            dir_path, dir_real, rel = stack.pop()
            self.report_progress(dir_path)
            try:
                with os.scandir(dir_path) as it:
                    entries = list(it)
            except PermissionError:
                self.folders_scanned += 1
                self.error(dir_path, "no permission to read folder")
                continue
            except OSError as exc:
                self.folders_scanned += 1
                self.error(dir_path, f"cannot read folder ({exc.strerror or exc})")
                continue

            relevant = 0
            for n, entry in enumerate(entries, start=1):
                try:
                    relevant += self._handle_entry(root_index, root_str, key, entry, dir_real, rel, stack)
                except OSError as exc:
                    # e.g. file vanished between listing and stat
                    self.error(entry.path, f"cannot read ({exc.strerror or exc})")
                if n % PROGRESS_EVERY_ENTRIES == 0:
                    self.report_progress(dir_path)
            self.folders_scanned += 1
            if relevant == 0:
                self.empty_folders += 1
                logger.debug("Folder has no videos: %s", dir_path)

    def _handle_entry(self, root_index, root_str, key, entry, dir_real, rel, stack) -> int:
        """Process one directory entry; return 1 if it is a video or a sub-folder to scan."""
        if _is_link(entry):
            reason = self.link_reason(entry, dir_real)
            if reason:
                self.skip(entry.path, reason)
            return 0

        if entry.is_dir(follow_symlinks=False):
            reason = _hidden_reason(entry)
            if reason:
                self.skip(entry.path, reason)
                return 0
            stack.append((entry.path, os.path.join(dir_real, entry.name), (*rel, entry.name)))
            return 1

        if not entry.is_file(follow_symlinks=False) or not self.is_video_name(entry.name):
            return 0  # non-video files are ignored silently

        reason = _hidden_reason(entry)
        if reason:
            self.skip(entry.path, reason)
            return 0

        st = entry.stat(follow_symlinks=False)
        name = unicodedata.normalize("NFC", entry.name)
        folder = "/".join(unicodedata.normalize("NFC", p) for p in rel)
        relative_path = f"{folder}/{name}" if folder else name
        item_id = media_id(key, relative_path)

        known = self.known_files.get(item_id) if self.known_files else None
        if known != (st.st_size, st.st_mtime):
            # New or changed since the last scan: prove it is readable (permission, file
            # locked by another app). Unchanged files skip this to keep rescans fast.
            try:
                with open(entry.path, "rb"):
                    pass
            except PermissionError:
                self.error(entry.path, "no permission to read file, or file is in use")
                return 0
            except OSError as exc:
                self.error(entry.path, f"cannot open file ({exc.strerror or exc})")
                return 0

        stem, ext = os.path.splitext(name)
        item = MediaItem(
            id=item_id,
            root_key=key,
            name=name,
            title=stem,
            library_root=root_str,
            relative_path=relative_path,
            folder=folder,
            extension=ext.lower(),
            size=st.st_size,
            mtime=st.st_mtime,
        )
        self.items.append((root_index, item))
        return 1


def scan_library(
    roots: Sequence[Path],
    extensions: Iterable[str],
    on_progress: ProgressCallback | None = None,
    known_files: KnownFiles | None = None,
) -> ScanResult:
    """Scan every library root and return the videos found plus a report.

    Never raises for problems with individual files or folders: they end up in
    ``report.skipped`` or ``report.errors``. A root that is not reachable (drive
    unplugged) is listed in ``report.unavailable_roots`` and not scanned.

    ``on_progress`` (optional) is called when a folder starts, every
    ``PROGRESS_EVERY_ENTRIES`` entries inside a folder, and once at the end. It runs
    on the scanning thread, so it should be fast; throttling is the caller's job.

    ``known_files`` (optional, from the index) lets files whose size and mtime did not
    change skip the "can it be opened" check.
    """
    started = time.perf_counter()
    exts = frozenset(e.lower() if e.startswith(".") else f".{e.lower()}" for e in extensions)
    scan = _Scan(extensions=exts, real_roots=[os.path.realpath(r) for r in roots],
                 on_progress=on_progress, known_files=known_files)
    root_keys: dict[str, str] = {}
    unavailable: list[str] = []
    for index, root in enumerate(roots):
        try:
            if not os.path.isdir(root):
                raise FileNotFoundError(str(root))
            key = root_key(root)
        except OSError:
            logger.warning("Library folder not available (drive disconnected?): %s", root)
            unavailable.append(str(root))
            continue
        root_keys[str(root)] = key
        scan.scan_root(index, Path(root), key)
    scan.report_progress("")

    scan.items.sort(key=lambda pair: (pair[0], natural_key(pair[1].folder), natural_key(pair[1].title),
                                      pair[1].extension))
    items = [item for _, item in scan.items]
    report = ScanReport(
        roots=[str(r) for r in roots],
        root_keys=root_keys,
        unavailable_roots=unavailable,
        found=len(items),
        skipped=sorted(scan.skipped, key=lambda i: natural_key(i.path)),
        errors=sorted(scan.errors, key=lambda i: natural_key(i.path)),
        empty_folders=scan.empty_folders,
        duration_seconds=round(time.perf_counter() - started, 3),
    )
    logger.info("Library scan: %s in %.2f s", report.summary(), report.duration_seconds)
    return ScanResult(items=items, report=report)
