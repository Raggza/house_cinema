"""Tests for plan step 2.3: video metadata (ffprobe) and thumbnails (ffmpeg)."""

from __future__ import annotations

import os
import shutil
import sqlite3
import unicodedata
from pathlib import Path

import pytest

from source import pipeline
from source.image.thumbnails import DEFAULT_THUMBNAIL, seek_time, thumb_path
from source.modules.library import LibraryIndex, scan_library
from source.modules.library.media_info import process_media
from source.modules.library.probe import container_for, parse_ffprobe, playability
from source.modules.library.report import format_duration
from source.modules.library.scanner import locate
from source.utils.config import load_settings
from source.utils.ffmpeg import FFTools, find_tools

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LIBRARY = PROJECT_ROOT / "test" / "fixtures" / "library"
EXTS = (".mp4", ".mkv", ".webm")
REAL_TOOLS = FFTools(ffmpeg=shutil.which("ffmpeg"), ffprobe=shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(REAL_TOOLS.missing != [], reason="ffmpeg/ffprobe not installed")


# ===================================================================== pure rules


@pytest.mark.parametrize(
    ("container", "video", "pix", "audio", "playable", "note_part"),
    [
        ("mp4", "h264", "yuv420p", "aac", True, None),
        ("mp4", "h264", "yuvj420p", "mp3", True, None),
        ("mp4", "h264", "yuv420p", None, True, None),  # silent video is fine
        ("webm", "vp9", "yuv420p", "opus", True, None),
        ("webm", "vp8", "yuv420p", "vorbis", True, None),
        ("mp4", "hevc", "yuv420p", "aac", False, "HEVC"),
        ("mp4", "h264", "yuv420p10le", "aac", False, "10-bit"),
        ("webm", "vp9", "yuv444p", "opus", False, "yuv444p"),
        ("mp4", "h264", "yuv420p", "ac3", False, "AC-3"),
        ("matroska", "h264", "yuv420p", "aac", False, "MKV container"),
        ("avi", "mpeg4", "yuv420p", "mp3", False, "AVI container"),
        ("mp4", None, None, "aac", False, "no video stream"),
    ],
)
def test_playability(container, video, pix, audio, playable, note_part):
    ok, note = playability(container, video, pix, audio)
    assert ok is playable
    if note_part:
        assert note_part in note
    else:
        assert note is None


def test_playability_lists_every_reason():
    ok, note = playability("matroska", "hevc", "yuv420p10le", "dts")
    assert not ok
    assert note.startswith("MKV container")
    assert "HEVC" in note and "DTS" in note


@pytest.mark.parametrize(
    ("ext", "fmt", "expected"),
    [
        (".mp4", "mov,mp4,m4a,3gp,3g2,mj2", "mp4"),
        (".M4V", "mov,mp4,m4a,3gp,3g2,mj2", "mp4"),
        (".webm", "matroska,webm", "webm"),
        (".mkv", "matroska,webm", "matroska"),
        (".mp4", "matroska,webm", "matroska"),  # extension lies: really an MKV
        (".xyz", "avi", "avi"),
    ],
)
def test_container_for(ext, fmt, expected):
    assert container_for(ext, fmt) == expected


def test_parse_ffprobe_skips_cover_art_and_falls_back_to_stream_duration():
    data = {
        "format": {"format_name": "matroska,webm"},
        "streams": [
            {"codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}},
            {"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p",
             "width": 1920, "height": "1080", "duration": "5400.5"},
            {"codec_type": "audio", "codec_name": "aac"},
        ],
    }
    r = parse_ffprobe(data, ".mkv")
    assert (r.video_codec, r.width, r.height, r.duration) == ("h264", 1920, 1080, 5400.5)
    assert r.container == "matroska"
    assert r.browser_playable is False


@pytest.mark.parametrize(("duration", "expected"), [(None, 1.0), (1.0, 0.5), (60, 6.0), (7200, 300.0)])
def test_seek_time(duration, expected):
    assert seek_time(duration) == expected


@pytest.mark.parametrize(("seconds", "text"), [(None, "unknown"), (2.02, "0:02"), (3725, "1:02:05")])
def test_format_duration(seconds, text):
    assert format_duration(seconds) == text


def test_find_tools_prefers_ffprobe_next_to_ffmpeg(tmp_path, monkeypatch):
    exe = ".exe" if os.name == "nt" else ""
    (tmp_path / f"ffmpeg{exe}").write_text("")
    (tmp_path / f"ffprobe{exe}").write_text("")
    monkeypatch.setattr(shutil, "which", lambda name: f"/elsewhere/{name}")
    s = load_settings(environ={"HUB_FFMPEG": str(tmp_path / f"ffmpeg{exe}")},
                      env_file=tmp_path / "none.env", cli_paths=[str(tmp_path)])
    tools = find_tools(s)
    assert tools.ffprobe == str(tmp_path / f"ffprobe{exe}")


def test_find_tools_reports_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    s = load_settings(environ={}, env_file=tmp_path / "none.env", cli_paths=[str(tmp_path)])
    assert find_tools(s).missing == ["ffmpeg", "ffprobe"]


def test_configured_tool_must_exist(tmp_path):
    from source.utils.config import ConfigError

    with pytest.raises(ConfigError, match="ffmpeg_path: file not found"):
        load_settings(environ={"HUB_FFMPEG": str(tmp_path / "nope.exe")},
                      env_file=tmp_path / "none.env", cli_paths=[str(tmp_path)])


# ============================================================ with real ffmpeg


@pytest.fixture
def lib(tmp_path) -> Path:
    root = tmp_path / "lib"
    shutil.copytree(LIBRARY, root)
    return root


@pytest.fixture
def index(tmp_path):
    idx = LibraryIndex(tmp_path / "db" / "library.db")
    yield idx
    idx.close()


def scan_and_process(index, lib, cache, tools=REAL_TOOLS, **kwargs):
    diff = index.apply_scan(scan_library([lib], EXTS, known_files=index.known_files()))
    report = process_media(index, tools, cache, workers=2, renamed=diff.renamed, **kwargs)
    return {i.relative_path: i for i in index.items()}, report


@needs_ffmpeg
def test_fixture_library_metadata_and_thumbnails(index, lib, tmp_path):
    cache = tmp_path / "cache"
    items, report = scan_and_process(index, lib, cache)

    assert (report.read_ok, report.read_failed) == (5, 1)
    assert (report.thumbs_created, report.thumbs_default) == (5, 1)
    for item in items.values():  # one image per video, broken file included
        assert thumb_path(cache, item.id).is_file()

    mp4 = items["Phim lẻ/Hành động/Cuộc Chiến Cuối Cùng.mp4"].info
    assert mp4.status == "ok" and mp4.thumbnail == "ok"
    assert (mp4.width, mp4.height, mp4.video_codec, mp4.audio_codec) == (160, 120, "h264", "aac")
    assert mp4.duration == pytest.approx(2.0, abs=0.1)
    assert mp4.browser_playable is True

    webm = items["Phim lẻ/Tài liệu/Đại dương xanh.webm"].info
    assert (webm.container, webm.video_codec, webm.audio_codec) == ("webm", "vp9", "opus")
    assert webm.browser_playable is True

    mkv = items["Phim bộ/Mùa 1/Tập 01.mkv"].info
    assert mkv.browser_playable is False and mkv.playable_note.startswith("MKV container")

    hevc = items["Phim bộ/Mùa 1/Tập 02 HEVC.mkv"].info
    assert "HEVC" in hevc.playable_note

    broken = items["Hỏng/File hỏng.mp4"]
    assert broken.info.status == "failed" and "moov atom not found" in broken.info.error
    assert broken.info.thumbnail == "default"
    assert thumb_path(cache, broken.id).read_bytes() == DEFAULT_THUMBNAIL.read_bytes()
    assert any(path == "Hỏng/File hỏng.mp4" for path, _ in report.failures)

    ok_thumb = thumb_path(cache, items["Phim lẻ/Hành động/Cuộc Chiến Cuối Cùng.mp4"].id).read_bytes()
    assert ok_thumb[:2] == b"\xff\xd8" and ok_thumb != DEFAULT_THUMBNAIL.read_bytes()


@needs_ffmpeg
def test_second_run_uses_cache(index, lib, tmp_path):
    cache = tmp_path / "cache"
    items, _ = scan_and_process(index, lib, cache)
    stamps = {i.id: thumb_path(cache, i.id).stat().st_mtime_ns for i in items.values()}

    _, report = scan_and_process(index, lib, cache)
    assert report.to_process == 0
    assert (report.read_ok, report.thumbs_created, report.thumbs_cached) == (0, 0, 6)
    assert {i: thumb_path(cache, i).stat().st_mtime_ns for i in stamps} == stamps


@needs_ffmpeg
def test_deleted_thumbnail_is_remade_without_reprobing(index, lib, tmp_path):
    cache = tmp_path / "cache"
    items, _ = scan_and_process(index, lib, cache)
    target = thumb_path(cache, items["Phim bộ/Mùa 1/Tập 01.mkv"].id)
    target.unlink()
    _, report = scan_and_process(index, lib, cache)
    assert target.is_file()
    assert (report.to_process, report.read_ok, report.thumbs_created) == (1, 0, 1)


@needs_ffmpeg
def test_changed_file_is_reprocessed(index, lib, tmp_path):
    cache = tmp_path / "cache"
    scan_and_process(index, lib, cache)
    src = lib / "Phim lẻ" / "Hành động" / "Cuộc Chiến Cuối Cùng.mp4"
    shutil.copyfile(lib / "Phim bộ" / "Mùa 1" / "Tập 02 HEVC.mkv", src)  # content replaced
    os.utime(src, (2_000_000_000, 2_000_000_000))
    items, report = scan_and_process(index, lib, cache)
    assert report.read_ok == 1
    assert items["Phim lẻ/Hành động/Cuộc Chiến Cuối Cùng.mp4"].info.video_codec == "hevc"


@needs_ffmpeg
def test_rename_keeps_info_and_thumbnail(index, lib, tmp_path):
    cache = tmp_path / "cache"
    items, _ = scan_and_process(index, lib, cache)
    old = items["Phim bộ/Mùa 1/Tập 01.mkv"]
    old_bytes = thumb_path(cache, old.id).read_bytes()
    os.rename(lib / "Phim bộ" / "Mùa 1" / "Tập 01.mkv", lib / "Phim bộ" / "Mùa 1" / "Tập 1.mkv")

    items, report = scan_and_process(index, lib, cache)
    new = items["Phim bộ/Mùa 1/Tập 1.mkv"]
    assert report.carried_over == 1 and report.to_process == 0
    assert new.info.video_codec == "h264"
    assert thumb_path(cache, new.id).read_bytes() == old_bytes
    assert not thumb_path(cache, old.id).exists()


@needs_ffmpeg
def test_removed_file_loses_info_and_thumbnail(index, lib, tmp_path):
    cache = tmp_path / "cache"
    items, _ = scan_and_process(index, lib, cache)
    gone = items["UPPERCASE EXT/Clip ngắn.MP4"]
    (lib / "UPPERCASE EXT" / "Clip ngắn.MP4").unlink()
    _, report = scan_and_process(index, lib, cache)
    assert report.purged == 1
    assert not thumb_path(cache, gone.id).exists()
    assert index._conn.execute("SELECT COUNT(*) FROM media_info WHERE id = ?", (gone.id,)).fetchone()[0] == 0


@needs_ffmpeg
def test_without_tools_then_with_tools(index, lib, tmp_path):
    cache = tmp_path / "cache"
    items, report = scan_and_process(index, lib, cache, tools=FFTools(None, None))
    assert report.missing_tools == ["ffmpeg", "ffprobe"]
    assert all(i.info.status == "no-tools" and i.info.thumbnail == "no-tools" for i in items.values())
    assert all(thumb_path(cache, i.id).is_file() for i in items.values())  # default images

    items, report = scan_and_process(index, lib, cache)  # ffmpeg installed later
    assert (report.read_ok, report.read_failed, report.thumbs_created) == (5, 1, 5)


@needs_ffmpeg
def test_ffprobe_missing_only(index, lib, tmp_path):
    cache = tmp_path / "cache"
    items, report = scan_and_process(index, lib, cache, tools=FFTools(REAL_TOOLS.ffmpeg, None))
    assert report.missing_tools == ["ffprobe"]
    assert {i.info.status for i in items.values()} == {"no-tools"}
    assert {i.info.thumbnail for i in items.values()} == {"no-tools"}


@needs_ffmpeg
def test_force_reprocesses_everything(index, lib, tmp_path):
    cache = tmp_path / "cache"
    scan_and_process(index, lib, cache)
    _, report = scan_and_process(index, lib, cache, force=True)
    assert report.to_process == 6 and report.thumbs_created == 5


def test_unavailable_items_are_skipped(index, lib, tmp_path):
    cache = tmp_path / "cache"
    index.apply_scan(scan_library([lib], EXTS))
    lib.rename(tmp_path / "away")
    index.apply_scan(scan_library([lib], EXTS))
    report = process_media(index, FFTools(None, None), cache, workers=1)
    assert report.skipped_unavailable == 6 and report.to_process == 0


def test_cache_not_writable_still_saves_info(index, lib, tmp_path):
    cache = tmp_path / "cache-is-a-file"
    cache.write_text("x")
    index.apply_scan(scan_library([lib], EXTS))
    report = process_media(index, FFTools(None, None), cache, workers=1)
    assert report.thumbs_disabled_reason
    assert {i.info.status for i in index.items()} == {"no-tools"}
    assert {i.info.thumbnail for i in index.items()} == {None}


# ==================================================================== index upgrade


def test_v1_index_is_upgraded_in_place(tmp_path, lib):
    db = tmp_path / "db" / "library.db"
    with LibraryIndex(db) as idx:
        idx.apply_scan(scan_library([lib], EXTS))
    conn = sqlite3.connect(db)  # turn it back into a format-1 index
    conn.executescript("DROP TABLE media_info; PRAGMA user_version = 1;")
    conn.close()

    with LibraryIndex(db) as idx:
        assert idx.recovered_from is None
        assert len(idx.items()) == 6
        assert idx._conn.execute("PRAGMA user_version").fetchone()[0] == 2


# ============================================================================ CLI


def run_cli(capsys, tmp_path, *args):
    code = pipeline.main([*args, "--env-file", str(tmp_path / "none.env")])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@needs_ffmpeg
def test_cli_scan_and_info(capsys, tmp_path, lib, _isolated_cache):
    code, out, _ = run_cli(capsys, tmp_path, "scan", "--path", str(lib))
    assert code == 0
    assert "Video info: 5 read · 1 failed · 0 up to date; thumbnails: 5 created · 1 default · 0 from cache" in out
    assert "! Hỏng/File hỏng.mp4 — cannot read video: moov atom not found" in out
    assert f"thumbnails saved in: {(_isolated_cache / 'thumbs').resolve()}" in out
    assert len(list((_isolated_cache / "thumbs").glob("*.jpg"))) == 6

    code, out, _ = run_cli(capsys, tmp_path, "scan", "--path", str(lib))
    assert "0 read · 0 failed · 6 up to date; thumbnails: 0 created · 0 default · 6 from cache" in out

    with LibraryIndex(Path(os.environ["HUB_INDEX_PATH"])) as idx:
        ids = {i.relative_path: i.id for i in idx.items()}

    code, out, _ = run_cli(capsys, tmp_path, "info", ids["Phim lẻ/Hành động/Cuộc Chiến Cuối Cùng.mp4"][:8],
                           "--path", str(lib))
    assert code == 0
    assert "duration   | 0:02" in out
    assert "resolution | 160×120" in out
    assert "video      | h264 (yuv420p)" in out
    assert "browser    | plays directly" in out
    assert "thumbnail  | from the video" in out

    code, out, _ = run_cli(capsys, tmp_path, "info", ids["Phim bộ/Mùa 1/Tập 02 HEVC.mkv"], "--path", str(lib))
    assert "needs conversion: MKV container (needs remux to MP4); HEVC/H.265 video" in out

    code, out, _ = run_cli(capsys, tmp_path, "info", ids["Hỏng/File hỏng.mp4"], "--path", str(lib))
    assert "cannot read this file: moov atom not found" in out
    assert "thumbnail  | default image" in out


def test_cli_info_unknown_id(capsys, tmp_path, lib):
    code, _, err = run_cli(capsys, tmp_path, "info", "ffffffffffff", "--path", str(lib))
    assert code == pipeline.EXIT_NOT_FOUND
    assert "No video with ID 'ffffffffffff'" in err


def test_cli_info_ambiguous_prefix(capsys, tmp_path, lib):
    run_cli(capsys, tmp_path, "scan", "--no-media", "--path", str(lib))
    with LibraryIndex(Path(os.environ["HUB_INDEX_PATH"])) as idx:
        all_ids = [i.id for i in idx.items()]
    # Find a 1-character prefix shared by 2+ IDs (6 IDs over 16 hex digits: very likely).
    prefix = next((c for c in "0123456789abcdef" if sum(i.startswith(c) for i in all_ids) > 1), None)
    if prefix is None:
        pytest.skip("no shared 1-char prefix in this run")
    code, _, err = run_cli(capsys, tmp_path, "info", prefix, "--path", str(lib))
    assert code == pipeline.EXIT_NOT_FOUND
    assert "use more characters" in err


def test_cli_no_media_and_dry_run_make_no_thumbnails(capsys, tmp_path, lib, _isolated_cache):
    run_cli(capsys, tmp_path, "scan", "--dry-run", "--path", str(lib))
    code, out, _ = run_cli(capsys, tmp_path, "scan", "--no-media", "--path", str(lib))
    assert code == 0 and "Video info" not in out
    assert not (_isolated_cache / "thumbs").exists()


# ===================================================== NFD file names (macOS copies, downloads)

NFD_NAME = unicodedata.normalize("NFD", "[23]【神ボディ】② Tiếng Việt.mp4")


def test_locate_finds_nfd_name(tmp_path):
    folder = tmp_path / unicodedata.normalize("NFD", "Phim bộ")
    folder.mkdir()
    (folder / NFD_NAME).write_bytes(b"x")
    nfc_rel = unicodedata.normalize("NFC", f"Phim bộ/{NFD_NAME}")
    found = locate(tmp_path, nfc_rel)
    assert found.is_file() and found.name == NFD_NAME
    assert locate(tmp_path, "Phim bộ/missing.mp4") == tmp_path / "Phim bộ" / "missing.mp4"


@needs_ffmpeg
def test_nfd_file_name_is_read_and_gets_thumbnail(index, tmp_path):
    lib = tmp_path / "nfd"
    lib.mkdir()
    shutil.copy(LIBRARY / "Phim lẻ" / "Hành động" / "Cuộc Chiến Cuối Cùng.mp4", lib / NFD_NAME)
    if not (lib / NFD_NAME).exists() or (lib / unicodedata.normalize("NFC", NFD_NAME)).exists():
        pytest.skip("this file system normalizes names, so NFD and NFC are the same file")
    items, report = scan_and_process(index, lib, tmp_path / "cache")
    item = items[unicodedata.normalize("NFC", NFD_NAME)]
    assert item.info.status == "ok", item.info.error
    assert item.info.thumbnail == "ok"
    assert report.failures == []


def test_failed_because_missing_is_retried(index, tmp_path):
    from source.modules.library.media_info import plan_work
    from source.schemas.media import MediaInfo

    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "a.mp4").write_bytes(b"x")
    index.apply_scan(scan_library([lib], EXTS))
    item = index.items()[0]
    missing = MediaInfo(status="failed", error=f"{lib / 'a.mp4'}: No such file or directory",
                        thumbnail="default", signature=item.signature)
    broken = MediaInfo(status="failed", error="moov atom not found", thumbnail="default",
                       signature=item.signature)
    cache = tmp_path / "cache"
    thumb_path(cache, item.id).parent.mkdir(parents=True)
    thumb_path(cache, item.id).write_bytes(b"x")
    assert plan_work(item.model_copy(update={"info": missing}), REAL_TOOLS, cache,
                     force=False, thumbs_enabled=True) == "full"
    assert plan_work(item.model_copy(update={"info": broken}), REAL_TOOLS, cache,
                     force=False, thumbs_enabled=True) is None
