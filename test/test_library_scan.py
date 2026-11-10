"""Tests for plan step 2.1: library scanner."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from source import pipeline
from source.modules.library import media_id, natural_key, root_key, scan_library
from source.modules.library import scanner as scanner_module
from source.modules.library.report import format_scan

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LIBRARY = PROJECT_ROOT / "test" / "fixtures" / "library"
VIDEO_EXTS = (".mp4", ".m4v", ".webm", ".mkv", ".avi", ".mov")
IS_WINDOWS = sys.platform == "win32"


def touch(path: Path, data: bytes = b"video") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def link_dir(link: Path, target: Path) -> str:
    """Create a directory symlink, or a junction on Windows without symlink privilege."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return "symlink"
    except OSError:
        if not IS_WINDOWS:
            pytest.skip("cannot create directory symlink")
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
        return "junction"


def set_windows_attr(path: Path, attr: int) -> None:
    import ctypes

    assert ctypes.windll.kernel32.SetFileAttributesW(str(path), attr)


def rel_paths(result) -> list[str]:
    return [i.relative_path for i in result.items]


def reasons(issues) -> dict[str, str]:
    return {Path(i.path).name: i.reason for i in issues}


# ------------------------------------------------------------ fixture library


def test_fixture_library():
    result = scan_library([LIBRARY], VIDEO_EXTS)
    assert rel_paths(result) == [
        "Hỏng/File hỏng.mp4",
        "Phim bộ/Mùa 1/Tập 01.mkv",
        "Phim bộ/Mùa 1/Tập 02 HEVC.mkv",
        "Phim lẻ/Hành động/Cuộc Chiến Cuối Cùng.mp4",
        "Phim lẻ/Tài liệu/Đại dương xanh.webm",
        "UPPERCASE EXT/Clip ngắn.MP4",
    ]
    assert result.report.found == 6
    assert reasons(result.report.skipped) == {".hidden": "hidden (name starts with '.')"}
    assert result.report.errors == []
    # "Thư mục rỗng" holds only a hidden .gitkeep
    assert result.report.empty_folders >= 1


def test_item_fields():
    items = {i.relative_path: i for i in scan_library([LIBRARY], VIDEO_EXTS).items}
    clip = items["UPPERCASE EXT/Clip ngắn.MP4"]
    assert clip.name == "Clip ngắn.MP4"
    assert clip.title == "Clip ngắn"
    assert clip.extension == ".mp4"
    assert clip.folder == "UPPERCASE EXT"
    assert clip.library_root == str(LIBRARY)
    path = LIBRARY / "UPPERCASE EXT" / "Clip ngắn.MP4"
    assert clip.size == path.stat().st_size
    assert clip.mtime == path.stat().st_mtime

    ep = items["Phim bộ/Mùa 1/Tập 01.mkv"]
    assert ep.folder == "Phim bộ/Mùa 1"
    assert ep.title == "Tập 01"


def test_non_video_files_ignored_silently():
    result = scan_library([LIBRARY], VIDEO_EXTS)
    names = {i.name for i in result.items} | set(reasons(result.report.skipped))
    assert not any(n.endswith((".srt", ".txt", ".gitkeep")) for n in names)


def test_extension_filter_follows_settings():
    result = scan_library([LIBRARY], ["MKV"])
    assert [i.name for i in result.items] == ["Tập 01.mkv", "Tập 02 HEVC.mkv"]


# -------------------------------------------------------- structure and order


def test_deeply_nested_and_unicode(tmp_path):
    deep = tmp_path / "a" / "b c" / "Thư mục" / "d" / "e"
    touch(deep / "Phim hay nhất.mp4")
    touch(tmp_path / "root file.mkv")
    result = scan_library([tmp_path], VIDEO_EXTS)
    assert rel_paths(result) == ["root file.mkv", "a/b c/Thư mục/d/e/Phim hay nhất.mp4"]
    assert result.items[0].folder == ""


def test_natural_sort(tmp_path):
    for n in (10, 2, 1, 21):
        touch(tmp_path / f"Tập {n}.mp4")
    assert [i.title for i in scan_library([tmp_path], VIDEO_EXTS).items] == [
        "Tập 1", "Tập 2", "Tập 10", "Tập 21",
    ]
    assert natural_key("Tập 9") < natural_key("tập 10")


def test_multiple_roots_keep_order(tmp_path):
    a, b = tmp_path / "B-first", tmp_path / "A-second"
    touch(a / "x.mp4")
    touch(b / "x.mp4")
    result = scan_library([a, b], VIDEO_EXTS)
    assert [i.library_root for i in result.items] == [str(a), str(b)]
    assert result.items[0].id != result.items[1].id


def test_empty_root(tmp_path):
    result = scan_library([tmp_path], VIDEO_EXTS)
    assert result.items == []
    assert result.report.found == 0
    assert result.report.empty_folders == 1
    assert "(no videos)" in format_scan(result)


# ------------------------------------------------------------------ stable IDs


def test_ids_are_stable(tmp_path):
    f = touch(tmp_path / "Phim" / "a.mp4")
    touch(tmp_path / "Phim" / "b.mp4")
    first = {i.relative_path: i.id for i in scan_library([tmp_path], VIDEO_EXTS).items}
    os.utime(f, (1_000_000, 1_000_000))
    f.write_bytes(b"changed content, same path")
    second = {i.relative_path: i.id for i in scan_library([tmp_path], VIDEO_EXTS).items}
    assert first == second
    assert len(set(first.values())) == 2


def test_id_independent_of_root_order(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    touch(a / "x.mp4")
    touch(b / "y.mp4")
    ids_ab = {i.name: i.id for i in scan_library([a, b], VIDEO_EXTS).items}
    ids_ba = {i.name: i.id for i in scan_library([b, a], VIDEO_EXTS).items}
    assert ids_ab == ids_ba


def test_id_matches_media_id_helper(tmp_path):
    touch(tmp_path / "Mùa 1" / "Tập 1.mp4")
    item = scan_library([tmp_path], VIDEO_EXTS).items[0]
    assert item.root_key == root_key(tmp_path)
    assert item.id == media_id(root_key(tmp_path), "Mùa 1/Tập 1.mp4")
    assert len(item.id) == 16


@pytest.mark.skipif(not IS_WINDOWS, reason="case-insensitive paths are a Windows behaviour")
def test_root_key_ignores_case_on_windows(tmp_path):
    assert root_key(str(tmp_path).upper()) == root_key(str(tmp_path).lower())


@pytest.mark.skipif(not IS_WINDOWS, reason="volume serial is used on Windows only")
def test_root_key_uses_volume_not_drive_letter(tmp_path):
    key = root_key(tmp_path)
    serial = os.stat(tmp_path).st_dev
    inside = os.path.normcase(os.path.splitdrive(os.path.realpath(tmp_path))[1])
    assert key == f"vol:{serial:x}:{inside}"
    assert ":" not in key.split(":", 2)[2]  # no drive letter in the part inside the volume


def test_root_key_differs_per_folder(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    assert root_key(tmp_path / "a") != root_key(tmp_path / "b")


def test_root_key_unreachable_raises(tmp_path):
    with pytest.raises(OSError):
        root_key(tmp_path / "missing")


def test_id_nfc_normalized(tmp_path):
    import unicodedata

    nfd = unicodedata.normalize("NFD", "Tập 1.mp4")
    assert media_id("k", nfd) == media_id("k", "Tập 1.mp4")


# --------------------------------------------------------------- hidden/system


def test_dot_hidden_file_and_folder_skipped(tmp_path):
    touch(tmp_path / ".secret.mp4")
    touch(tmp_path / ".cache" / "a.mp4")
    touch(tmp_path / "ok.mp4")
    result = scan_library([tmp_path], VIDEO_EXTS)
    assert rel_paths(result) == ["ok.mp4"]
    assert set(reasons(result.report.skipped)) == {".secret.mp4", ".cache"}


def test_system_folder_names_skipped(tmp_path):
    touch(tmp_path / "$RECYCLE.BIN" / "deleted.mp4")
    touch(tmp_path / "System Volume Information" / "x.mp4")
    result = scan_library([tmp_path], VIDEO_EXTS)
    assert result.items == []
    assert set(reasons(result.report.skipped).values()) == {"system folder"}


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows file attributes")
def test_windows_hidden_and_system_attributes(tmp_path):
    hidden_file = touch(tmp_path / "hidden.mp4")
    system_dir = tmp_path / "sysdir"
    touch(system_dir / "inside.mp4")
    touch(tmp_path / "visible.mp4")
    set_windows_attr(hidden_file, 0x2)  # FILE_ATTRIBUTE_HIDDEN
    set_windows_attr(system_dir, 0x4 | 0x10)  # SYSTEM | DIRECTORY
    try:
        result = scan_library([tmp_path], VIDEO_EXTS)
        assert rel_paths(result) == ["visible.mp4"]
        r = reasons(result.report.skipped)
        assert r["hidden.mp4"] == "hidden (Windows attribute)"
        assert r["sysdir"] == "system file/folder (Windows attribute)"
    finally:
        set_windows_attr(hidden_file, 0x80)  # NORMAL, so tmp_path can be cleaned
        set_windows_attr(system_dir, 0x10)


# ----------------------------------------------------------------------- links


def test_link_loop_skipped(tmp_path):
    lib = tmp_path / "lib"
    touch(lib / "a" / "x.mp4")
    link_dir(lib / "a" / "loop", lib)
    result = scan_library([lib], VIDEO_EXTS)
    assert rel_paths(result) == ["a/x.mp4"]
    assert reasons(result.report.skipped)["loop"].startswith("link loop")


def test_link_outside_library_skipped(tmp_path):
    lib, outside = tmp_path / "lib", tmp_path / "outside"
    touch(lib / "in.mp4")
    touch(outside / "secret.mp4")
    link_dir(lib / "out", outside)
    result = scan_library([lib], VIDEO_EXTS)
    assert rel_paths(result) == ["in.mp4"]
    assert "outside the library" in reasons(result.report.skipped)["out"]


def test_link_inside_library_not_duplicated(tmp_path):
    lib = tmp_path / "lib"
    touch(lib / "real" / "x.mp4")
    link_dir(lib / "alias", lib / "real")
    result = scan_library([lib], VIDEO_EXTS)
    assert rel_paths(result) == ["real/x.mp4"]
    assert "already in the library" in reasons(result.report.skipped)["alias"]


def test_link_to_other_library_root_not_duplicated(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    touch(a / "x.mp4")
    touch(b / "y.mp4")
    link_dir(a / "to-b", b)
    result = scan_library([a, b], VIDEO_EXTS)
    assert sorted(i.name for i in result.items) == ["x.mp4", "y.mp4"]
    assert "already in the library" in reasons(result.report.skipped)["to-b"]


def test_broken_link_skipped(tmp_path):
    lib, gone = tmp_path / "lib", tmp_path / "gone"
    touch(lib / "ok.mp4")
    gone.mkdir()
    link_dir(lib / "dangling", gone)
    gone.rmdir()
    result = scan_library([lib], VIDEO_EXTS)
    assert rel_paths(result) == ["ok.mp4"]
    assert reasons(result.report.skipped)["dangling"].startswith("broken link")


# ---------------------------------------------------------------------- errors


def test_unreadable_folder_does_not_stop_scan(tmp_path, monkeypatch):
    touch(tmp_path / "locked" / "a.mp4")
    touch(tmp_path / "open" / "b.mp4")
    real_scandir = os.scandir

    def fake_scandir(path):
        if Path(path).name == "locked":
            raise PermissionError(13, "Access is denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr(scanner_module.os, "scandir", fake_scandir)
    result = scan_library([tmp_path], VIDEO_EXTS)
    assert rel_paths(result) == ["open/b.mp4"]
    assert reasons(result.report.errors) == {"locked": "no permission to read folder"}


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (PermissionError(13, "Access is denied"), "no permission to read file, or file is in use"),
        (FileNotFoundError(2, "No such file or directory"), "cannot open file (No such file or directory)"),
    ],
)
def test_unreadable_file_does_not_stop_scan(tmp_path, monkeypatch, exc, expected):
    touch(tmp_path / "bad.mp4")
    touch(tmp_path / "good.mp4")
    real_open = open

    def fake_open(path, *args, **kwargs):
        if Path(path).name == "bad.mp4":
            raise exc
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(scanner_module, "open", fake_open, raising=False)
    result = scan_library([tmp_path], VIDEO_EXTS)
    assert rel_paths(result) == ["good.mp4"]
    assert reasons(result.report.errors) == {"bad.mp4": expected}
    assert "1 error" in result.report.summary()


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows share-mode locking")
def test_file_locked_by_other_process(tmp_path):
    """A file opened with no sharing (like a download in progress) is reported, not fatal."""
    import ctypes
    from ctypes import wintypes

    locked = touch(tmp_path / "downloading.mp4")
    touch(tmp_path / "done.mp4")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(str(locked), 0x80000000, 0, None, 3, 0x80, None)  # no share
    assert handle != wintypes.HANDLE(-1).value
    try:
        result = scan_library([tmp_path], VIDEO_EXTS)
    finally:
        kernel32.CloseHandle(handle)
    assert rel_paths(result) == ["done.mp4"]
    assert "downloading.mp4" in reasons(result.report.errors)


# ------------------------------------------------------------------------- CLI


def test_cli_scan_dry_run(capsys, tmp_path):
    code = pipeline.main(
        ["scan", "--dry-run", "--path", str(LIBRARY), "--env-file", str(tmp_path / "none.env")]
    )
    out = capsys.readouterr().out
    assert code == pipeline.EXIT_OK
    assert "Phim bộ/Mùa 1/" in out
    assert "Clip ngắn.MP4" in out
    assert ".hidden  — hidden (name starts with '.')" in out
    assert "6 found · 1 skipped · 0 errors" in out
    assert "First scan: 6 videos would be indexed." in out
    assert out.rstrip().splitlines()[-1] == "(dry run: index not changed)"


def test_cli_options_before_subcommand(capsys, tmp_path):
    code = pipeline.main(
        ["--path", str(LIBRARY / "Phim bộ"), "--env-file", str(tmp_path / "none.env"), "scan", "--dry-run"]
    )
    out = capsys.readouterr().out
    assert code == pipeline.EXIT_OK
    assert "2 found" in out


def test_cli_scan_bad_path_is_config_error(capsys, tmp_path):
    code = pipeline.main(
        ["scan", "--dry-run", "--path", str(tmp_path / "nope"), "--env-file", str(tmp_path / "none.env")]
    )
    assert code == pipeline.EXIT_CONFIG_ERROR
    assert "path does not exist" in capsys.readouterr().err
