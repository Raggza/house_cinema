"""The list of library folders, managed from the admin page (plan step 4.2).

Where the list comes from, first match wins:
1. ``--path`` on the command line: used as is for this run, not editable from the web page.
2. ``library-paths.json`` next to the index (``data/`` by default), written by the admin page.
3. ``library.paths`` in config.yml / ``HUB_LIBRARY_PATHS``: only until the first save.

The run command is therefore always the same; the folders live in the saved file. Changes
apply at once (no restart): the server reads ``LibraryFolders.paths()`` on every request and
every rescan.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

logger = logging.getLogger("house_cinema.library.folders")

STORE_NAME = "library-paths.json"
Source = Literal["cli", "saved", "config"]


def store_path_for(index_path: Path) -> Path:
    return Path(index_path).with_name(STORE_NAME)


def read_saved_paths(store: Path) -> list[str] | None:
    """Paths in the saved file; None if there is no file yet. A damaged file is kept aside
    and treated as missing (the admin page then starts from the config again)."""
    try:
        data = json.loads(store.read_text(encoding="utf-8"))
        paths = data["paths"]
        if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
            raise ValueError("'paths' must be a list of strings")
        return paths
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.warning("Ignoring unreadable %s (%s); fix or delete it, or save the folders again "
                       "on the admin page.", store, exc)
        return None


class FolderError(ValueError):
    """The new list is refused. ``problems`` holds (path as typed, message) pairs."""

    def __init__(self, problems: list[tuple[str, str]]):
        super().__init__("; ".join(f"{p}: {m}" for p, m in problems))
        self.problems = problems


class FoldersLocked(Exception):
    """The folders were given with --path for this run: not editable."""


@dataclass(frozen=True)
class FolderCheck:
    path: Path
    exists: bool
    problem: str | None
    """Why it cannot be scanned now (missing, not a folder, no permission), or None."""


def _key(path: Path | str) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def clean_input(text: str) -> str:
    """Path as typed or pasted: trims spaces and the quotes of Explorer's "Copy as path"."""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


def check_folder(path: Path) -> str | None:
    """None if ``path`` can be used as a library folder now, else a message (Vietnamese:
    shown on the admin page). Same rules as the config (plan step 1.1)."""
    if not path.exists():
        return f"Không tìm thấy thư mục: {path}"
    if not path.is_dir():
        return f"Đây là một file, không phải thư mục: {path}"
    try:
        with os.scandir(path) as entries:  # os.access() is unreliable on Windows
            next(entries, None)
    except PermissionError:
        return f"Không có quyền đọc thư mục: {path}"
    except OSError as exc:
        return f"Không đọc được thư mục: {path} ({exc.strerror or exc})"
    return None


class LibraryFolders:
    """Current library folders, shared by the HTTP handlers and the rescan (thread-safe)."""

    def __init__(self, paths: Sequence[Path], *, store: Path, source: Source):
        self._lock = threading.Lock()
        self._paths = [Path(p) for p in paths]
        self._version = 0
        self.store = store
        self.source: Source = source

    @property
    def editable(self) -> bool:
        return self.source != "cli"

    @property
    def version(self) -> int:
        """Increases on every change (lets readers drop cached results)."""
        with self._lock:
            return self._version

    def paths(self) -> list[Path]:
        with self._lock:
            return list(self._paths)

    def check(self) -> list[FolderCheck]:
        return [FolderCheck(p, p.exists(), check_folder(p)) for p in self.paths()]

    def validate(self, typed: Sequence[str]) -> list[Path]:
        """The new list, cleaned and resolved, or FolderError listing every problem.

        A folder already in the list may be missing now (drive unplugged): it is kept. A new
        one must exist and be readable. No duplicates, no folder inside another.
        """
        current = {_key(p) for p in self.paths()}
        problems: list[tuple[str, str]] = []
        result: list[Path] = []
        for raw in typed:
            text = clean_input(raw)
            if not text:
                continue
            no_drive = os.name == "nt" and not os.path.splitdrive(text)[0]  # "\Phim": which drive?
            if not os.path.isabs(text) or no_drive or text.startswith(("\\\\?\\", "\\\\.\\")):
                problems.append((raw, "Cần đường dẫn đầy đủ, ví dụ D:\\Phim hoặc \\\\may-chu\\Phim"))
                continue
            path = Path(os.path.normpath(text))
            if path.exists():
                path = path.resolve()
            if any(_key(path) == _key(p) for p in result):
                problems.append((raw, "Thư mục này đã có trong danh sách"))
                continue
            if _key(path) not in current:
                message = check_folder(path)
                if message:
                    problems.append((raw, message))
                    continue
            result.append(path)
        for i, outer in enumerate(result):
            for inner in result[i + 1:]:
                if inner.is_relative_to(outer) or outer.is_relative_to(inner):
                    parent, child = (outer, inner) if inner.is_relative_to(outer) else (inner, outer)
                    problems.append((str(child), f"Nằm bên trong thư mục {parent}: chỉ giữ thư mục cha"))
        if problems:
            raise FolderError(problems)
        return result

    def save(self, typed: Sequence[str], *, by: str = "?") -> list[Path]:
        """Validate, write the store file (atomically), and apply at once."""
        if not self.editable:
            raise FoldersLocked()
        new = self.validate(typed)
        data = {"paths": [str(p) for p in new],
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        self.store.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.store.with_name(f"{self.store.name}.tmp{os.getpid()}")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, self.store)
        finally:
            tmp.unlink(missing_ok=True)
        with self._lock:
            old = {_key(p): p for p in self._paths}
            self._paths = new
            self._version += 1
            self.source = "saved"
        added = [str(p) for p in new if _key(p) not in old]
        removed = [str(p) for k, p in old.items() if k not in {_key(n) for n in new}]
        logger.info("Library folders changed from %s: added %s, removed %s (saved to %s)",
                    by, added or "none", removed or "none", self.store)
        return new
