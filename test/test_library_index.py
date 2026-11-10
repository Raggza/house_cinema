"""Tests for plan step 2.2: persistent index, rescan diff, damaged index, unplugged drives."""

from __future__ import annotations

import logging
import os
import sqlite3
import time
import types
from pathlib import Path

import pytest

from source import pipeline
from source.modules.library import LibraryIndex, read_known_root_paths, scan_library
from source.modules.library import scanner as scanner_module
from source.schemas.media import MediaItem, ScanReport, ScanResult
from source.utils.config import ConfigError, load_settings

EXTS = (".mp4", ".mkv")


def touch(path: Path, data: bytes = b"video") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def lib(tmp_path) -> Path:
    root = tmp_path / "Phim"
    touch(root / "Hành động" / "A.mp4", b"a")
    touch(root / "Hành động" / "B.mp4", b"bb")
    touch(root / "Mùa 1" / "Tập 1.mkv", b"ccc")
    return root


@pytest.fixture
def index(tmp_path):
    idx = LibraryIndex(tmp_path / "db" / "library.db")
    yield idx
    idx.close()


def rescan(index: LibraryIndex, roots, *, dry_run=False):
    result = scan_library(roots, EXTS, known_files=index.known_files())
    return index.apply_scan(result, dry_run=dry_run)


def indexed_paths(index: LibraryIndex) -> set[str]:
    return {i.relative_path for i in index.items()}


def disk_paths(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.suffix.lower() in EXTS}


# --------------------------------------------------------------- persistence


def test_first_scan_and_persistence(tmp_path, lib):
    db = tmp_path / "db" / "library.db"
    with LibraryIndex(db) as idx:
        diff = rescan(idx, [lib])
        assert diff.first_scan
        assert len(diff.added) == 3
        ids = {i.id for i in idx.items()}

    # A restart reads the index without scanning.
    with LibraryIndex(db) as idx:
        items = idx.items()
        assert {i.id for i in items} == ids
        assert all(i.available and i.library_root == str(lib) for i in items)
        assert idx.recovered_from is None
        assert [r.items for r in idx.roots()] == [3]


def test_rescan_without_changes(index, lib):
    rescan(index, [lib])
    diff = rescan(index, [lib])
    assert diff.is_empty()
    assert diff.summary() == "+0 −0 ~0"


def test_add_delete_rename_then_rescan_matches_disk(index, lib):
    rescan(index, [lib])
    before = {i.relative_path: i for i in index.items()}

    touch(lib / "Mới.mp4", b"new file")
    (lib / "Hành động" / "B.mp4").unlink()
    os.rename(lib / "Mùa 1" / "Tập 1.mkv", lib / "Mùa 1" / "Tập 01.mkv")  # keeps size and mtime

    diff = rescan(index, [lib])
    assert [i.relative_path for i in diff.added] == ["Mới.mp4"]
    assert [i.relative_path for i in diff.removed] == ["Hành động/B.mp4"]
    assert [(o.relative_path, n.relative_path) for o, n in diff.renamed] == [("Mùa 1/Tập 1.mkv", "Mùa 1/Tập 01.mkv")]
    assert diff.summary() == "+1 −1 ~1"
    assert indexed_paths(index) == disk_paths(lib)

    # Untouched files keep their IDs.
    assert {i.relative_path: i.id for i in index.items()}["Hành động/A.mp4"] == before["Hành động/A.mp4"].id


def test_rename_keeps_added_at(index, lib, monkeypatch):
    clock = iter([1000.0, 2000.0, 3000.0])
    # Only the index's clock: patching time.time itself let threads left by other tests (their
    # log records call time.time) use up these values (StopIteration, seen in step 6.2).
    fake_time = types.SimpleNamespace(time=lambda: next(clock), strftime=time.strftime)
    monkeypatch.setattr("source.modules.library.index.time", fake_time)
    rescan(index, [lib])
    os.rename(lib / "Hành động" / "A.mp4", lib / "Hành động" / "A (2019).mp4")
    rescan(index, [lib])
    row = index._conn.execute("SELECT added_at FROM media WHERE name = 'A (2019).mp4'").fetchone()
    assert row["added_at"] == 1000.0


def test_ambiguous_rename_is_reported_as_add_and_remove(index, tmp_path):
    root = tmp_path / "lib"
    a, b = touch(root / "a.mp4", b"same"), touch(root / "b.mp4", b"same")
    for f in (a, b):
        os.utime(f, (1_700_000_000, 1_700_000_000))
    rescan(index, [root])
    os.rename(a, root / "c.mp4")
    os.rename(b, root / "d.mp4")
    diff = rescan(index, [root])
    assert diff.renamed == []
    assert sorted(i.name for i in diff.added) == ["c.mp4", "d.mp4"]
    assert sorted(i.name for i in diff.removed) == ["a.mp4", "b.mp4"]


def test_changed_file_is_updated(index, lib):
    rescan(index, [lib])
    f = lib / "Hành động" / "A.mp4"
    f.write_bytes(b"re-encoded, longer")
    os.utime(f, (2_000_000_000, 2_000_000_000))
    diff = rescan(index, [lib])
    assert [i.name for i in diff.changed] == ["A.mp4"]
    assert index.known_files()[diff.changed[0].id] == (f.stat().st_size, f.stat().st_mtime)


def test_dry_run_does_not_write(index, lib):
    diff = rescan(index, [lib], dry_run=True)
    assert len(diff.added) == 3
    assert index.items() == []
    assert index.roots() == []


def test_root_removed_from_config_is_dropped(index, tmp_path, lib):
    other = tmp_path / "Khác"
    touch(other / "x.mp4")
    rescan(index, [lib, other])
    diff = rescan(index, [lib])
    assert diff.dropped_roots == [(str(other), 1)]
    assert {i.library_root for i in index.items()} == {str(lib)}


# ------------------------------------------------------- incremental read check


def test_unchanged_files_are_not_reopened(index, lib, monkeypatch):
    rescan(index, [lib])
    opened: list[str] = []
    real_open = open

    def counting_open(path, *args, **kwargs):
        opened.append(Path(path).name)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(scanner_module, "open", counting_open, raising=False)
    touch(lib / "new.mp4")
    rescan(index, [lib])
    assert opened == ["new.mp4"]


# ------------------------------------------------- temporary read errors keep data


def test_unreadable_folder_keeps_its_items(index, lib, monkeypatch):
    rescan(index, [lib])
    real_scandir = os.scandir

    def flaky(path):
        if Path(path).name == "Mùa 1":
            raise PermissionError(13, "denied")
        return real_scandir(path)

    monkeypatch.setattr(scanner_module.os, "scandir", flaky)
    diff = rescan(index, [lib])
    assert diff.removed == []
    assert diff.kept_after_errors == 1
    assert "Mùa 1/Tập 1.mkv" in indexed_paths(index)


def test_locked_file_keeps_its_item(index, lib, monkeypatch):
    rescan(index, [lib])
    f = lib / "Hành động" / "A.mp4"
    os.utime(f, (2_000_000_000, 2_000_000_000))  # changed, so the read check runs
    real_open = open

    def locked(path, *args, **kwargs):
        if Path(path).name == "A.mp4":
            raise PermissionError(13, "in use")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(scanner_module, "open", locked, raising=False)
    diff = rescan(index, [lib])
    assert diff.removed == [] and diff.kept_after_errors == 1
    assert "Hành động/A.mp4" in indexed_paths(index)


# ------------------------------------------------------------ unplugged drives


def test_unplugged_folder_marked_unavailable_then_restored(index, tmp_path, lib):
    rescan(index, [lib])
    ids = {i.id for i in index.items()}
    unplugged = tmp_path / "unplugged"
    lib.rename(unplugged)  # the folder disappears, like an unplugged USB drive

    result = scan_library([lib], EXTS)
    assert result.report.unavailable_roots == [str(lib)]
    assert result.report.errors == []
    diff = index.apply_scan(result)
    assert diff.now_unavailable == [(str(lib), 3)]
    assert diff.removed == []
    assert diff.summary() == "+0 −0 ~0 · !3 unavailable"
    items = index.items()
    assert {i.id for i in items} == ids
    assert not any(i.available for i in items)

    # Scanning again while still unplugged changes nothing.
    assert rescan(index, [lib]).is_empty()

    unplugged.rename(lib)  # plugged back in
    diff = rescan(index, [lib])
    assert diff.restored == [(str(lib), 3)]
    assert diff.added == [] and diff.removed == []
    assert all(i.available for i in index.items())
    assert {i.id for i in index.items()} == ids


def test_same_volume_under_new_drive_letter_keeps_ids(index, tmp_path, lib):
    """Simulate F: -> G:: the scan sees the same root_key under a new path."""
    rescan(index, [lib])
    old_items = index.items()
    key = old_items[0].root_key
    new_root = "G:\\Phim" if os.name == "nt" else "/media/new/Phim"
    moved = [MediaItem(**{**i.model_dump(), "library_root": new_root}) for i in old_items]
    result = ScanResult(items=moved, report=ScanReport(
        roots=[new_root, str(lib)], root_keys={new_root: key}, unavailable_roots=[str(lib)],
        found=len(moved), skipped=[], errors=[], empty_folders=0, duration_seconds=0.0,
    ))
    diff = index.apply_scan(result)
    assert diff.is_empty(), diff.summary()
    items = index.items()
    assert {i.id for i in items} == {i.id for i in old_items}
    assert {i.library_root for i in items} == {new_root}
    assert all(i.available for i in items)


# --------------------------------------------------------------- damaged index


@pytest.mark.parametrize("damage", ["garbage", "truncated", "foreign-db"])
def test_damaged_index_is_moved_aside_and_rebuilt(tmp_path, lib, caplog, damage):
    db = tmp_path / "db" / "library.db"
    with LibraryIndex(db) as idx:
        rescan(idx, [lib])
    if damage == "garbage":
        db.write_bytes(b"this is not a database" * 100)
    elif damage == "truncated":
        data = db.read_bytes()
        db.write_bytes(data[:100] + b"\x00" * 50 + data[150 : len(data) // 2])
    else:  # a valid SQLite file that is not our index
        db.unlink()
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE other (x)")
        conn.commit()
        conn.close()

    with caplog.at_level(logging.WARNING, logger="house_cinema.library.index"):
        with LibraryIndex(db) as idx:
            assert idx.recovered_from is not None
            assert idx.recovered_from.exists()
            assert idx.recovered_from.name.startswith("library.db.corrupt-")
            assert idx.items() == []
            diff = rescan(idx, [lib])  # the next scan fills it again
            assert diff.first_scan and len(diff.added) == 3
    assert "started a new index" in caplog.text


def test_missing_index_folder_is_created(tmp_path, lib):
    db = tmp_path / "a" / "b" / "library.db"
    with LibraryIndex(db) as idx:
        rescan(idx, [lib])
    assert db.is_file()


def test_read_known_root_paths(tmp_path, lib):
    db = tmp_path / "db" / "library.db"
    assert read_known_root_paths(db) == []
    with LibraryIndex(db) as idx:
        rescan(idx, [lib])
    assert read_known_root_paths(db) == [str(lib)]
    db.write_bytes(b"garbage")
    assert read_known_root_paths(db) == []


# ------------------------------------------------- settings with unplugged drive


def test_settings_accept_known_missing_folder(tmp_path):
    missing = tmp_path / "unplugged"
    kwargs = dict(environ={}, env_file=tmp_path / "none.env", cli_paths=[str(missing)])
    with pytest.raises(ConfigError, match="path does not exist"):
        load_settings(**kwargs)  # strict: never indexed, so most likely a typo
    with pytest.raises(ConfigError, match="path does not exist"):
        load_settings(**kwargs, known_roots_reader=lambda _: [str(tmp_path / "other")])
    s = load_settings(**kwargs, known_roots_reader=lambda _: [str(missing)])
    assert s.library_paths == (missing.resolve(),)


def test_settings_index_path_relative_to_project(tmp_path):
    from source.schemas.settings import PROJECT_ROOT

    s = load_settings(environ={"HUB_INDEX_PATH": "data/x.db"}, env_file=tmp_path / "none.env",
                      cli_paths=[str(tmp_path)])
    assert s.index_path == (PROJECT_ROOT / "data" / "x.db").resolve()


# ------------------------------------------------------------------------ CLI


def run_cli(capsys, tmp_path, *args):
    code = pipeline.main([*args, "--env-file", str(tmp_path / "none.env")])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_cli_scan_list_and_unplug(capsys, tmp_path, lib, _isolated_index):
    code, out, _ = run_cli(capsys, tmp_path, "scan", "--path", str(lib))
    assert code == 0
    assert "First scan: 3 videos indexed." in out
    assert _isolated_index.is_file()

    code, out, _ = run_cli(capsys, tmp_path, "list", "--path", str(lib))
    assert code == 0
    assert "Tập 1.mkv" in out
    assert "3 videos in index · 3 available · 0 unavailable" in out

    touch(lib / "Mới.mp4", b"new")
    code, out, _ = run_cli(capsys, tmp_path, "scan", "--path", str(lib))
    assert "Changes since last scan: +1 −0 ~0" in out
    assert "  + Mới.mp4" in out

    lib.rename(tmp_path / "away")  # unplug
    code, out, err = run_cli(capsys, tmp_path, "scan", "--path", str(lib))
    assert code == 0, err
    assert "[not connected]" in out
    assert "!4 unavailable" in out
    code, out, _ = run_cli(capsys, tmp_path, "list", "--path", str(lib))
    assert "not connected, 4 videos unavailable" in out
    code, out, _ = run_cli(capsys, tmp_path, "--check-config", "--path", str(lib))
    assert code == 0 and "(not connected)" in out

    # A path that was never indexed is still a config error.
    code, _, err = run_cli(capsys, tmp_path, "scan", "--path", str(tmp_path / "typo"))
    assert code == pipeline.EXIT_CONFIG_ERROR and "path does not exist" in err


def test_cli_dry_run_shows_changes_without_saving(capsys, tmp_path, lib, _isolated_index):
    run_cli(capsys, tmp_path, "scan", "--path", str(lib))
    (lib / "Hành động" / "B.mp4").unlink()
    code, out, _ = run_cli(capsys, tmp_path, "scan", "--dry-run", "--path", str(lib))
    assert "Would change: +0 −1 ~0" in out
    assert out.rstrip().endswith("(dry run: index not changed)")
    with LibraryIndex(_isolated_index) as idx:
        assert "Hành động/B.mp4" in indexed_paths(idx)


def test_cli_damaged_index_warns_and_recovers(capsys, tmp_path, lib, _isolated_index):
    _isolated_index.parent.mkdir(parents=True)
    _isolated_index.write_bytes(b"garbage" * 100)
    code, out, err = run_cli(capsys, tmp_path, "scan", "--path", str(lib))
    assert code == 0
    assert "Warning: the index was damaged" in err
    assert "First scan: 3 videos indexed." in out


def test_cli_index_folder_not_creatable(capsys, tmp_path, lib, monkeypatch):
    blocker = tmp_path / "file-not-folder"
    blocker.write_text("x")
    monkeypatch.setenv("HUB_INDEX_PATH", str(blocker / "library.db"))
    code, _, err = run_cli(capsys, tmp_path, "scan", "--path", str(lib))
    assert code == pipeline.EXIT_INDEX_ERROR
    assert err.startswith("Index error: cannot create the index folder")


def test_cli_list_empty_index(capsys, tmp_path, lib):
    code, out, _ = run_cli(capsys, tmp_path, "list", "--path", str(lib))
    assert code == 0
    assert "is empty. Run: python -m source.pipeline scan" in out
