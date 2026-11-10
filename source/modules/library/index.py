"""Persistent library index (SQLite), so a restart does not have to rescan everything.

- ``LibraryIndex.apply_scan()`` merges a ScanResult and returns what changed (IndexDiff).
- A damaged index file is moved aside (``library.db.corrupt-<time>``) and rebuilt empty;
  the next scan fills it again.
- A library folder that is not connected (USB unplugged) keeps its items, marked
  unavailable, instead of losing them. They become available again when it comes back,
  even under another drive letter (IDs are based on the volume, see scanner.root_key).
- Items under a folder/file that could not be read during the scan are kept as they were:
  a temporary error (flaky network share, file locked by another app) must not wipe them.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from source.modules.library.scanner import natural_key
from source.schemas.media import MediaInfo, MediaItem, ScanResult

logger = logging.getLogger("house_cinema.library.index")

SCHEMA_VERSION = 2
# media_info has no foreign key on purpose: on a rename the old row is carried over to the
# new ID before orphans are purged (see carry_over_renames / purge_orphan_info).
_MEDIA_INFO_SCHEMA = """
CREATE TABLE media_info (
    id               TEXT PRIMARY KEY,
    signature        TEXT NOT NULL,
    status           TEXT NOT NULL,
    error            TEXT,
    duration         REAL,
    width            INTEGER,
    height           INTEGER,
    container        TEXT,
    video_codec      TEXT,
    pix_fmt          TEXT,
    audio_codec      TEXT,
    browser_playable INTEGER,
    playable_note    TEXT,
    thumbnail        TEXT,
    updated_at       REAL NOT NULL
);
"""
_INFO_COLUMNS = ("signature", "status", "error", "duration", "width", "height", "container",
                 "video_codec", "pix_fmt", "audio_codec", "browser_playable", "playable_note",
                 "thumbnail")
# version -> SQL that upgrades it to version + 1
_MIGRATIONS = {1: _MEDIA_INFO_SCHEMA}
_SCHEMA = _MEDIA_INFO_SCHEMA + """
CREATE TABLE roots (
    root_key  TEXT PRIMARY KEY,
    path      TEXT NOT NULL,
    available INTEGER NOT NULL DEFAULT 1,
    last_scan REAL
);
CREATE TABLE media (
    id            TEXT PRIMARY KEY,
    root_key      TEXT NOT NULL REFERENCES roots(root_key) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    name          TEXT NOT NULL,
    title         TEXT NOT NULL,
    folder        TEXT NOT NULL,
    extension     TEXT NOT NULL,
    size          INTEGER NOT NULL,
    mtime         REAL NOT NULL,
    added_at      REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE INDEX media_root ON media(root_key);
"""
_MEDIA_COLUMNS = ("id", "root_key", "relative_path", "name", "title", "folder", "extension",
                  "size", "mtime", "added_at", "updated_at")


class LibraryIndexError(Exception):
    """The index cannot be used; the message is meant for the user."""


@dataclass(frozen=True)
class RootInfo:
    key: str
    path: str
    available: bool
    last_scan: float | None
    items: int


@dataclass
class IndexDiff:
    first_scan: bool = False
    added: list[MediaItem] = field(default_factory=list)
    removed: list[MediaItem] = field(default_factory=list)
    renamed: list[tuple[MediaItem, MediaItem]] = field(default_factory=list)
    """(old, new) pairs."""
    changed: list[MediaItem] = field(default_factory=list)
    """Same path, new size or mtime (file replaced or re-encoded)."""
    now_unavailable: list[tuple[str, int]] = field(default_factory=list)
    """(root path, item count) for folders that just went offline."""
    restored: list[tuple[str, int]] = field(default_factory=list)
    """(root path, item count) for folders that came back."""
    dropped_roots: list[tuple[str, int]] = field(default_factory=list)
    """(root path, item count) for folders no longer in the configuration."""
    kept_after_errors: int = 0
    """Items not removed because their folder/file could not be read this time."""

    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.renamed or self.changed
                    or self.now_unavailable or self.restored or self.dropped_roots)

    def summary(self) -> str:
        parts = [f"+{len(self.added)} −{len(self.removed)} ~{len(self.renamed)}"]
        if self.changed:
            parts.append(f"{len(self.changed)} updated")
        if self.now_unavailable:
            parts.append(f"!{sum(n for _, n in self.now_unavailable)} unavailable")
        if self.restored:
            parts.append(f"{sum(n for _, n in self.restored)} back online")
        if self.dropped_roots:
            parts.append(f"{sum(n for _, n in self.dropped_roots)} dropped (folder removed from config)")
        return " · ".join(parts)


def read_known_root_paths(index_path: Path) -> list[str]:
    """Library folder paths stored in the index; [] if the index is missing or unreadable.

    Read-only and never raises: used while loading the configuration, before the index
    is really opened (and repaired if needed).
    """
    path = Path(index_path)
    if not path.is_file():
        return []
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            return [row[0] for row in conn.execute("SELECT path FROM roots")]
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError):
        return []


def _norm(path: str) -> str:
    # NFC: error paths come from the disk (may be NFD), indexed paths are NFC.
    return unicodedata.normalize("NFC", os.path.normcase(os.path.normpath(path)))


class LibraryIndex:
    def __init__(self, path: Path, *, any_thread: bool = False):
        """``any_thread``: allow use from several threads (the server). The caller must then
        serialize access itself (one lock around every call)."""
        self.path = Path(path)
        self._any_thread = any_thread
        self.recovered_from: Path | None = None
        """Where a damaged index was moved to, if it had to be rebuilt on open."""
        self.recovery_reason: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise LibraryIndexError(
                f"cannot create the index folder {self.path.parent}: {exc.strerror or exc}"
            ) from None
        self._conn = self._open()

    # ------------------------------------------------------------------ opening

    def _connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(self.path, isolation_level=None,  # explicit transactions
                                   check_same_thread=not self._any_thread)
        except sqlite3.Error as exc:
            raise LibraryIndexError(f"cannot open the index {self.path}: {exc}") from None
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _prepare(self, conn: sqlite3.Connection) -> None:
        """Validate the file, or create the schema in an empty one. Raises DatabaseError."""
        check = conn.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise sqlite3.DatabaseError(f"integrity check failed: {check}")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if version == 0 and not tables:
            conn.executescript(_SCHEMA + f"PRAGMA user_version = {SCHEMA_VERSION};")
        elif version in _MIGRATIONS and {"roots", "media"} <= tables:
            while version < SCHEMA_VERSION:
                conn.executescript(f"BEGIN; {_MIGRATIONS[version]} PRAGMA user_version = {version + 1}; COMMIT;")
                logger.info("Index upgraded from format %d to %d", version, version + 1)
                version += 1
        elif version != SCHEMA_VERSION or not {"roots", "media", "media_info"} <= tables:
            raise sqlite3.DatabaseError(
                f"unexpected index format (version {version}, tables {sorted(tables)})"
            )
        conn.execute("PRAGMA journal_mode = WAL")

    def _open(self) -> sqlite3.Connection:
        conn = self._connect()
        try:
            self._prepare(conn)
            return conn
        except sqlite3.DatabaseError as exc:
            conn.close()
            self._move_aside(str(exc))
        conn = self._connect()
        self._prepare(conn)
        return conn

    def _move_aside(self, reason: str) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        n = 1
        while target.exists():
            n += 1
            target = self.path.with_name(f"{self.path.name}.corrupt-{stamp}-{n}")
        try:
            for suffix in ("", "-wal", "-shm"):
                src = Path(f"{self.path}{suffix}")
                if src.exists():
                    os.replace(src, Path(f"{target}{suffix}"))
        except OSError as exc:
            raise LibraryIndexError(
                f"the index {self.path} is damaged ({reason}) and could not be moved aside "
                f"({exc.strerror or exc}); is the hub running? Stop it and try again."
            ) from None
        self.recovered_from = target
        self.recovery_reason = reason
        logger.warning("Index %s was unreadable (%s); kept it as %s and started a new index. "
                       "The next scan fills it again.", self.path, reason, target.name)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> LibraryIndex:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ------------------------------------------------------------------ reading

    def known_files(self) -> dict[str, tuple[int, float]]:
        return {r["id"]: (r["size"], r["mtime"])
                for r in self._conn.execute("SELECT id, size, mtime FROM media")}

    def roots(self) -> list[RootInfo]:
        rows = self._conn.execute(
            "SELECT r.root_key, r.path, r.available, r.last_scan, COUNT(m.id) AS n "
            "FROM roots r LEFT JOIN media m ON m.root_key = r.root_key GROUP BY r.root_key"
        )
        return sorted((RootInfo(r["root_key"], r["path"], bool(r["available"]), r["last_scan"], r["n"])
                       for r in rows), key=lambda r: natural_key(r.path))

    _ITEM_QUERY = (
        "SELECT m.*, r.path AS root_path, r.available AS root_available, "
        + ", ".join(f"i.{c} AS info_{c}" for c in _INFO_COLUMNS)
        + " FROM media m JOIN roots r ON r.root_key = m.root_key "
        "LEFT JOIN media_info i ON i.id = m.id"
    )

    def items(self) -> list[MediaItem]:
        items = [self._to_item(r) for r in self._conn.execute(self._ITEM_QUERY)]
        items.sort(key=lambda i: (natural_key(i.library_root), natural_key(i.folder),
                                  natural_key(i.title), i.extension))
        return items

    def data_version(self) -> int:
        """Changes whenever *another* connection (a scan) commits: cheap "anything new?" check."""
        return self._conn.execute("PRAGMA data_version").fetchone()[0]

    def get(self, media_id: str) -> MediaItem | None:
        """The item with exactly this ID, or None."""
        row = self._conn.execute(self._ITEM_QUERY + " WHERE m.id = ?", (media_id,)).fetchone()
        return self._to_item(row) if row is not None else None

    def find(self, id_or_prefix: str) -> list[MediaItem]:
        """Items whose ID starts with ``id_or_prefix`` (case-insensitive)."""
        prefix = id_or_prefix.strip().lower()
        if not prefix:
            return []
        rows = self._conn.execute(self._ITEM_QUERY + " WHERE m.id LIKE ? ESCAPE '\\'",
                                  (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",))
        return [self._to_item(r) for r in rows]

    @staticmethod
    def _to_info(row: sqlite3.Row) -> MediaInfo | None:
        keys = row.keys()
        if "info_status" not in keys or row["info_status"] is None:
            return None
        values = {c: row[f"info_{c}"] for c in _INFO_COLUMNS}
        if values["browser_playable"] is not None:
            values["browser_playable"] = bool(values["browser_playable"])
        return MediaInfo(**values)

    @classmethod
    def _to_item(cls, row: sqlite3.Row, root_path: str | None = None, available: bool | None = None) -> MediaItem:
        return MediaItem(
            id=row["id"], root_key=row["root_key"], name=row["name"], title=row["title"],
            library_root=root_path if root_path is not None else row["root_path"],
            relative_path=row["relative_path"], folder=row["folder"], extension=row["extension"],
            size=row["size"], mtime=row["mtime"],
            available=available if available is not None else bool(row["root_available"]),
            info=cls._to_info(row),
        )

    # --------------------------------------------------------------- media info

    def save_info(self, media_id: str, info: MediaInfo) -> None:
        values = info.model_dump()
        if values["browser_playable"] is not None:
            values["browser_playable"] = int(values["browser_playable"])
        cols = ("id", *_INFO_COLUMNS, "updated_at")
        self._conn.execute(
            f"INSERT OR REPLACE INTO media_info ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            (media_id, *(values[c] for c in _INFO_COLUMNS), time.time()),
        )

    def carry_over_renames(self, renamed: list[tuple[MediaItem, MediaItem]]) -> list[tuple[str, str]]:
        """Give a renamed file the info of its old ID (same content). Returns moved (old, new) IDs."""
        moved = []
        for old, new in renamed:
            if old.signature != new.signature:
                continue
            cur = self._conn.execute("UPDATE media_info SET id = ? WHERE id = ? AND NOT EXISTS "
                                     "(SELECT 1 FROM media_info WHERE id = ?)", (new.id, old.id, new.id))
            if cur.rowcount:
                moved.append((old.id, new.id))
        return moved

    def purge_orphan_info(self) -> list[str]:
        """Delete info rows whose media is gone; return their IDs (to delete thumbnails)."""
        ids = [r[0] for r in self._conn.execute(
            "SELECT id FROM media_info WHERE id NOT IN (SELECT id FROM media)")]
        self._conn.executemany("DELETE FROM media_info WHERE id = ?", [(i,) for i in ids])
        return ids

    # ------------------------------------------------------------------ writing

    def apply_scan(self, result: ScanResult, *, dry_run: bool = False) -> IndexDiff:
        """Merge a scan into the index in one transaction and return what changed.

        With ``dry_run`` the changes are computed and then rolled back.
        """
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            diff = self._merge(result)
            conn.execute("ROLLBACK" if dry_run else "COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return diff

    def _merge(self, result: ScanResult) -> IndexDiff:
        conn, now = self._conn, time.time()
        report = result.report
        existing = {r["root_key"]: r for r in conn.execute("SELECT * FROM roots")}
        diff = IndexDiff(first_scan=not existing)

        scanned: dict[str, dict[str, MediaItem]] = defaultdict(dict)
        for item in result.items:
            scanned[item.root_key][item.id] = item

        # Paths that could not be read this time: keep whatever the index has under them.
        error_paths = [_norm(e.path) for e in report.errors]

        def under_error(abs_path: str) -> bool:
            p = _norm(abs_path)
            return any(p == e or p.startswith(e.rstrip(os.sep) + os.sep) for e in error_paths)

        seen_keys: set[str] = set()

        for root_path, key in report.root_keys.items():
            seen_keys.add(key)
            prev = existing.get(key)
            conn.execute(
                "INSERT INTO roots (root_key, path, available, last_scan) VALUES (?, ?, 1, ?) "
                "ON CONFLICT(root_key) DO UPDATE SET path = excluded.path, available = 1, "
                "last_scan = excluded.last_scan",
                (key, root_path, now),
            )
            old = {r["id"]: r for r in conn.execute("SELECT * FROM media WHERE root_key = ?", (key,))}
            new = scanned.get(key, {})
            if prev is not None and not prev["available"]:
                diff.restored.append((root_path, len(new)))

            removed_ids = [i for i in old.keys() - new.keys()
                           if not under_error(os.path.join(root_path, old[i]["relative_path"]))]
            diff.kept_after_errors += len(old.keys() - new.keys()) - len(removed_ids)
            added_ids = list(new.keys() - old.keys())

            # Rename = one file gone and one file new with the same size and mtime (unique match).
            gone_by_sig, new_by_sig = defaultdict(list), defaultdict(list)
            for i in removed_ids:
                gone_by_sig[(old[i]["size"], old[i]["mtime"])].append(i)
            for i in added_ids:
                new_by_sig[(new[i].size, new[i].mtime)].append(i)
            renames = {gone[0]: new_by_sig[sig][0] for sig, gone in gone_by_sig.items()
                       if len(gone) == 1 and len(new_by_sig.get(sig, ())) == 1}

            for old_id in removed_ids:
                conn.execute("DELETE FROM media WHERE id = ?", (old_id,))
                old_item = self._to_item(old[old_id], root_path, True)
                if old_id in renames:
                    diff.renamed.append((old_item, new[renames[old_id]]))
                else:
                    diff.removed.append(old_item)
            renamed_added_at = {new_id: old[old_id]["added_at"] for old_id, new_id in renames.items()}
            for new_id in added_ids:
                self._insert(new[new_id], added_at=renamed_added_at.get(new_id, now), now=now)
                if new_id not in renamed_added_at:
                    diff.added.append(new[new_id])

            for item_id in old.keys() & new.keys():
                item, row = new[item_id], old[item_id]
                if (item.size, item.mtime) != (row["size"], row["mtime"]):
                    conn.execute("UPDATE media SET size = ?, mtime = ?, updated_at = ? WHERE id = ?",
                                 (item.size, item.mtime, now, item_id))
                    diff.changed.append(item)

        # Configured folders that are not connected: keep their items, mark unavailable.
        by_path = {_norm(r["path"]): r for r in existing.values()}
        for root_path in report.unavailable_roots:
            row = by_path.get(_norm(root_path))
            if row is None or row["root_key"] in seen_keys:
                continue  # never indexed, or the same volume is already back under another letter
            seen_keys.add(row["root_key"])
            if row["available"]:
                count = conn.execute("SELECT COUNT(*) FROM media WHERE root_key = ?",
                                     (row["root_key"],)).fetchone()[0]
                conn.execute("UPDATE roots SET available = 0 WHERE root_key = ?", (row["root_key"],))
                diff.now_unavailable.append((root_path, count))

        # Folders no longer configured at all: drop them and their items.
        for key, row in existing.items():
            if key not in seen_keys:
                count = conn.execute("SELECT COUNT(*) FROM media WHERE root_key = ?", (key,)).fetchone()[0]
                conn.execute("DELETE FROM roots WHERE root_key = ?", (key,))
                diff.dropped_roots.append((row["path"], count))

        if diff.kept_after_errors:
            logger.warning("Kept %d indexed videos that could not be read this time.", diff.kept_after_errors)
        return diff

    def _insert(self, item: MediaItem, *, added_at: float, now: float) -> None:
        self._conn.execute(
            f"INSERT INTO media ({', '.join(_MEDIA_COLUMNS)}) VALUES ({', '.join('?' * len(_MEDIA_COLUMNS))})",
            (item.id, item.root_key, item.relative_path, item.name, item.title, item.folder,
             item.extension, item.size, item.mtime, added_at, now),
        )
