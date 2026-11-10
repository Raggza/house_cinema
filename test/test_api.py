"""Tests for plan step 3.4: the JSON API (library, media, folders, rescan, thumbnails).

Contract tests: every response is validated against the published schema (extra fields
forbidden) and the field sets are pinned, so a change that would break the web client of
Phase 4 fails here first.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from source.image.thumbnails import DEFAULT_THUMBNAIL
from source.modules.library import LibraryIndex, scan_library
from source.modules.library.media_info import process_media
from source.modules.server import bind_socket, create_app
from source.modules.server import rescan as rescan_module
from source.schemas.api import FolderList, LibraryPage, MediaDetail, MediaSummary, RescanStatus
from source.utils.config import load_settings
from source.utils.ffmpeg import FFTools

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LIBRARY = PROJECT_ROOT / "test" / "fixtures" / "library"
EXTS = (".mp4", ".mkv", ".webm")
REAL_TOOLS = FFTools(ffmpeg=shutil.which("ffmpeg"), ffprobe=shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(REAL_TOOLS.missing != [], reason="ffmpeg/ffprobe not installed")

MOVIE = "Phim lẻ/Hành động/Cuộc Chiến Cuối Cùng.mp4"
EPISODE = "Phim bộ/Mùa 1/Tập 01.mkv"
BROKEN = "Hỏng/File hỏng.mp4"


# ======================================================================= setup


@pytest.fixture
def lib(tmp_path) -> Path:
    root = tmp_path / "Thư viện"
    shutil.copytree(LIBRARY, root)
    return root


def settings_for(lib: Path, index_path: Path, cache: Path, tmp_path: Path, extra_roots=()):
    # Both the index and the cache are test folders (a rescan writes thumbnails).
    return load_settings(environ={"HUB_INDEX_PATH": str(index_path), "HUB_CACHE_DIR": str(cache)},
                         env_file=tmp_path / "none.env", cli_paths=[str(lib), *map(str, extra_roots)])


@pytest.fixture
def ids(lib, _isolated_index, _isolated_cache) -> dict[str, str]:
    """Scanned, with details and thumbnails when ffmpeg is installed."""
    with LibraryIndex(_isolated_index) as index:
        index.apply_scan(scan_library([lib], EXTS))
        if not REAL_TOOLS.missing:
            process_media(index, REAL_TOOLS, _isolated_cache, workers=2)
        return {i.relative_path: i.id for i in index.items()}


@pytest.fixture
def client(lib, ids, _isolated_index, _isolated_cache, tmp_path):
    settings = settings_for(lib, _isolated_index, _isolated_cache, tmp_path)
    with TestClient(create_app(settings)) as c:
        yield c
        c.app.state.rescanner.wait(30)


def page(client, **params) -> LibraryPage:
    r = client.get("/api/library", params=params)
    assert r.status_code == 200, r.text
    return LibraryPage.model_validate(r.json())  # extra="forbid": unknown fields fail


def assert_no_server_path(text: str, lib: Path) -> None:
    assert str(lib) not in text and str(lib).replace("\\", "\\\\") not in text
    assert ":\\\\" not in text and "Thư viện\\" not in text


# ============================================================ contract: schema


EXPECTED_FIELDS = {
    "MediaSummary": {"id", "title", "name", "library", "folder", "extension", "size", "modified",
                     "duration", "width", "height", "available", "browser_playable", "thumbnail_url",
                     "stream_url", "detail_url"},
    "MediaDetail": {"info_status", "info_error", "container", "video_codec", "pix_fmt", "audio_codec",
                    "playable_note", "subtitles"},  # plus everything of MediaSummary
    "LibraryPage": {"items", "total", "offset", "limit", "next_offset"},
    "LibraryRef": {"id", "name", "available"},
    "FolderInfo": {"library", "folder", "videos", "total_videos"},
    "FolderList": {"libraries", "folders"},
    "RescanStatus": {"state", "started", "phase", "progress_done", "progress_total", "started_at",
                     "finished_at", "counts", "error"},
    "SubtitleTrackOut": {"key", "lang", "label", "format", "url"},
}


def test_openapi_contract(client):
    spec = client.get("/openapi.json").json()
    schemas = spec["components"]["schemas"]
    for name, fields in EXPECTED_FIELDS.items():
        props = set(schemas[name]["properties"])
        if name == "MediaDetail":
            props -= EXPECTED_FIELDS["MediaSummary"]
        assert props == fields, f"{name} fields changed: {props ^ fields}"
    paths = spec["paths"]
    expected = {"/api/library": {"get"}, "/api/folders": {"get"}, "/api/media/{media_id}": {"get"},
                "/api/rescan": {"get", "post"}, "/thumbnails/{media_id}.jpg": {"get"},
                "/stream/{media_id}": {"get", "head"}, "/subtitles/{media_id}": {"get"},
                "/subtitles/{media_id}/{key}.vtt": {"get"}, "/health": {"get"}}
    for path, methods in expected.items():
        assert set(paths[path]) == methods, path
    ref = lambda p, m, code="200": paths[p][m]["responses"][code]["content"]["application/json"]["schema"]  # noqa: E731
    assert ref("/api/library", "get")["$ref"].endswith("/LibraryPage")
    assert ref("/api/media/{media_id}", "get")["$ref"].endswith("/MediaDetail")
    assert ref("/api/rescan", "post", "202")["$ref"].endswith("/RescanStatus")
    assert ref("/api/folders", "get")["$ref"].endswith("/FolderList")


def test_docs_page(client):
    r = client.get("/docs")
    assert r.status_code == 200 and "swagger" in r.text.lower()


# ============================================================ /api/library


def test_library_lists_everything_in_order(client, ids, lib):
    r = client.get("/api/library")
    assert_no_server_path(r.text, lib)
    p = LibraryPage.model_validate(r.json())
    assert p.total == len(ids) == 6
    assert [i.folder + "/" + i.name for i in p.items] == [
        "Hỏng/File hỏng.mp4", "Phim bộ/Mùa 1/Tập 01.mkv", "Phim bộ/Mùa 1/Tập 02 HEVC.mkv",
        "Phim lẻ/Hành động/Cuộc Chiến Cuối Cùng.mp4", "Phim lẻ/Tài liệu/Đại dương xanh.webm",
        "UPPERCASE EXT/Clip ngắn.MP4"]
    item = next(i for i in p.items if i.id == ids[MOVIE])
    assert item.library.name == "Thư viện" and item.library.available
    assert (item.stream_url, item.thumbnail_url, item.detail_url) == (
        f"/stream/{item.id}", f"/thumbnails/{item.id}.jpg", f"/api/media/{item.id}")
    if not REAL_TOOLS.missing:
        assert (item.width, item.height, item.browser_playable) == (160, 120, True)
        assert item.duration == pytest.approx(2.0, abs=0.1)


def test_pagination(client):
    first = page(client, limit=4)
    assert (len(first.items), first.total, first.next_offset) == (4, 6, 4)
    rest = page(client, limit=4, offset=4)
    assert (len(rest.items), rest.next_offset) == (2, None)
    assert {i.id for i in first.items}.isdisjoint({i.id for i in rest.items})
    assert page(client, offset=100).items == []
    for bad in ({"limit": 0}, {"limit": 201}, {"offset": -1}, {"sort": "random"}):
        assert client.get("/api/library", params=bad).status_code == 422


@pytest.mark.parametrize(("q", "expected"), [
    ("cuoc chien", ["Cuộc Chiến Cuối Cùng"]),  # no marks, lower case
    ("CUỘC CHIẾN", ["Cuộc Chiến Cuối Cùng"]),
    ("tap", ["Tập 01", "Tập 02 HEVC"]),
    ("phim bo 02", ["Tập 02 HEVC"]),  # folder + title words
    ("dai duong", ["Đại dương xanh"]),  # đ → d
    ("không có", []),
])
def test_search(client, q, expected):
    assert [i.title for i in page(client, q=q).items] == expected


def test_folder_filter(client):
    assert [i.title for i in page(client, folder="Phim bộ/Mùa 1").items] == ["Tập 01", "Tập 02 HEVC"]
    assert page(client, folder="Phim lẻ").items == []  # only videos directly in it
    assert len(page(client, folder="Phim lẻ", recursive=True).items) == 2
    assert len(page(client, folder="", recursive=True).items) == 6


def test_sorting(client, lib, ids):
    os.utime(lib / MOVIE, (time.time() + 100, time.time() + 100))
    with LibraryIndex(Path(os.environ["HUB_INDEX_PATH"])) as index:
        index.apply_scan(scan_library([lib], EXTS, known_files=index.known_files()))
    assert page(client, sort="newest").items[0].id == ids[MOVIE]
    sizes = [i.size for i in page(client, sort="largest").items]
    assert sizes == sorted(sizes, reverse=True)
    titles = [i.title.casefold() for i in page(client, sort="title").items]
    assert titles == sorted(titles)


def test_two_libraries_and_library_filter(lib, ids, _isolated_index, _isolated_cache, tmp_path):
    other = tmp_path / "Khác"
    (other / "x").mkdir(parents=True)
    (other / "x" / "Phim khác.mp4").write_bytes(b"video")
    with LibraryIndex(_isolated_index) as index:
        index.apply_scan(scan_library([lib, other], EXTS, known_files=index.known_files()))
    with TestClient(create_app(settings_for(lib, _isolated_index, _isolated_cache, tmp_path, [other]))) as c:
        libs = FolderList.model_validate(c.get("/api/folders").json()).libraries
        assert sorted(x.name for x in libs) == ["Khác", "Thư viện"]
        khac = next(x for x in libs if x.name == "Khác")
        other_items = page(c, library=khac.id).items
        assert [i.title for i in other_items] == ["Phim khác"]
        assert page(c).total == 7
    # The same index, but only one folder configured now: the other one disappears at once,
    # from the list and from every URL.
    with TestClient(create_app(settings_for(lib, _isolated_index, _isolated_cache, tmp_path))) as c:
        assert page(c).total == 6 and page(c, library=khac.id).total == 0
        gone = other_items[0]
        for url in (gone.detail_url, gone.stream_url, gone.thumbnail_url):
            assert c.get(url).status_code == 404, url


def test_disconnected_library_is_listed_as_unavailable(client, lib, ids, _isolated_index):
    moved = lib.rename(lib.with_name("unplugged"))
    with LibraryIndex(_isolated_index) as index:
        index.apply_scan(scan_library([lib], EXTS, known_files=index.known_files()))
    p = page(client)
    assert p.total == 6 and not any(i.available for i in p.items)
    assert all(not i.library.available for i in p.items)
    d = MediaDetail.model_validate(client.get(f"/api/media/{ids[MOVIE]}").json())
    assert (d.available, d.subtitles) == (False, [])
    assert client.get(f"/stream/{ids[MOVIE]}").status_code == 404
    assert client.get(f"/thumbnails/{ids[MOVIE]}.jpg").status_code == 200  # still shown in the grid
    moved.rename(lib)


# ============================================================ /api/folders


def test_folders(client, lib):
    r = client.get("/api/folders")
    assert_no_server_path(r.text, lib)
    folders = {f.folder: (f.videos, f.total_videos) for f in FolderList.model_validate(r.json()).folders}
    assert folders == {"": (0, 6), "Hỏng": (1, 1), "Phim bộ": (0, 2), "Phim bộ/Mùa 1": (2, 2),
                       "Phim lẻ": (0, 2), "Phim lẻ/Hành động": (1, 1), "Phim lẻ/Tài liệu": (1, 1),
                       "UPPERCASE EXT": (1, 1)}


# ============================================================ /api/media/{id}


def test_media_detail(client, ids, lib):
    r = client.get(f"/api/media/{ids[MOVIE]}")
    assert r.status_code == 200
    assert_no_server_path(r.text, lib)
    d = MediaDetail.model_validate(r.json())
    assert d.title == "Cuộc Chiến Cuối Cùng" and d.folder == "Phim lẻ/Hành động"
    assert [(s.key, s.label, s.url) for s in d.subtitles] == [
        ("en", "English", f"/subtitles/{d.id}/en.vtt"), ("vi", "Tiếng Việt", f"/subtitles/{d.id}/vi.vtt")]
    if not REAL_TOOLS.missing:
        assert (d.info_status, d.container, d.video_codec, d.audio_codec, d.pix_fmt) == (
            "ok", "mp4", "h264", "aac", "yuv420p")
    # Every URL it gives works.
    assert client.get(d.stream_url, headers={"Range": "bytes=0-9"}).status_code == 206
    assert client.get(d.thumbnail_url).headers["content-type"] == "image/jpeg"
    assert client.get(d.subtitles[1].url).text.startswith("WEBVTT")


@needs_ffmpeg
def test_media_detail_broken_and_not_playable(client, ids):
    broken = MediaDetail.model_validate(client.get(f"/api/media/{ids[BROKEN]}").json())
    assert broken.info_status == "failed" and "moov atom not found" in broken.info_error
    assert broken.duration is None and broken.browser_playable is None
    mkv = MediaDetail.model_validate(client.get(f"/api/media/{ids[EPISODE]}").json())
    assert mkv.browser_playable is False and mkv.playable_note.startswith("MKV container")
    assert [s.lang for s in mkv.subtitles] == ["vi"]


def test_media_detail_pending_before_details_are_read(lib, _isolated_index, _isolated_cache, tmp_path):
    with LibraryIndex(_isolated_index) as index:
        index.apply_scan(scan_library([lib], EXTS))  # like `scan --no-media`
        media_id = index.items()[0].id
    with TestClient(create_app(settings_for(lib, _isolated_index, _isolated_cache, tmp_path))) as c:
        d = MediaDetail.model_validate(c.get(f"/api/media/{media_id}").json())
        assert (d.info_status, d.duration, d.browser_playable) == ("pending", None, None)
        assert c.get(d.thumbnail_url).content == DEFAULT_THUMBNAIL.read_bytes()


@pytest.mark.parametrize("media_id", ["0123456789abcdef", "nope", "..%2F..%2Fwin.ini"])
def test_media_detail_unknown_is_404(client, media_id):
    r = client.get(f"/api/media/{media_id}")
    assert (r.status_code, r.json()) == (404, {"detail": "Not Found"})


def test_all_404s_look_the_same(client, ids):
    """Unknown URL, unknown video, blocked probe: one identical answer (open item of 3.2)."""
    bodies = {client.get(url).text for url in [
        "/no/such/page", "/stream/0123456789abcdef", "/stream/..%2F..%2Fwin.ini", "/stream/zzz",
        "/api/media/0123456789abcdef", "/thumbnails/0123456789abcdef.jpg",
        "/subtitles/0123456789abcdef", f"/subtitles/{ids[MOVIE]}/fr.vtt"]}
    assert bodies == {'{"detail":"Not Found"}'}


# ============================================================ /thumbnails


@needs_ffmpeg
def test_thumbnails(client, ids, _isolated_cache):
    r = client.get(f"/thumbnails/{ids[MOVIE]}.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.content == (_isolated_cache / "thumbs" / f"{ids[MOVIE]}.jpg").read_bytes()
    assert r.content != DEFAULT_THUMBNAIL.read_bytes()
    assert "etag" in r.headers and r.headers["cache-control"] == "no-cache"
    again = client.get(f"/thumbnails/{ids[MOVIE]}.jpg", headers={"If-None-Match": r.headers["etag"]})
    assert again.status_code == 304
    # Broken file: the default image.
    assert client.get(f"/thumbnails/{ids[BROKEN]}.jpg").content == DEFAULT_THUMBNAIL.read_bytes()


# ============================================================ /api/rescan


def wait_done(client, timeout=60) -> RescanStatus:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = RescanStatus.model_validate(client.get("/api/rescan").json())
        if s.state in ("done", "failed"):
            return s
        time.sleep(0.1)
    pytest.fail("rescan did not finish")


def test_rescan_picks_up_changes(client, lib, ids):
    assert RescanStatus.model_validate(client.get("/api/rescan").json()).state == "idle"
    shutil.copy(lib / MOVIE, lib / "Phim lẻ" / "Mới thêm.mp4")
    (lib / BROKEN).unlink()
    (lib / EPISODE).rename(lib / "Phim bộ" / "Mùa 1" / "Tập 01 (đổi tên).mkv")
    before = page(client).total

    r = client.post("/api/rescan")
    assert r.status_code == 202
    s = RescanStatus.model_validate(r.json())
    assert s.started and s.state in ("running", "done")
    s = wait_done(client)
    assert s.state == "done" and s.error is None and s.finished_at >= s.started_at
    c = s.counts
    assert (c.found, c.added, c.removed, c.renamed) == (6, 1, 1, 1)
    if not REAL_TOOLS.missing:
        assert (c.info_read, c.thumbnails_created) == (1, 1)

    # The API sees the new index at once (no restart).
    titles = {i.title for i in page(client).items}
    assert before == 6 and "Mới thêm" in titles and "File hỏng" not in titles
    assert "Tập 01 (đổi tên)" in titles
    renamed = next(i for i in page(client, q="doi ten").items)
    assert client.get(f"/stream/{renamed.id}", headers={"Range": "bytes=0-9"}).status_code == 206
    assert client.get(f"/stream/{ids[EPISODE]}").status_code == 404  # old ID gone


def test_second_rescan_while_running_does_not_start_another(client, monkeypatch):
    gate = threading.Event()
    real_scan = rescan_module.scan_library

    def slow_scan(*args, **kwargs):
        gate.wait(10)
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(rescan_module, "scan_library", slow_scan)
    first = RescanStatus.model_validate(client.post("/api/rescan").json())
    second = RescanStatus.model_validate(client.post("/api/rescan").json())
    assert (first.started, second.started, second.state) == (True, False, "running")
    assert second.started_at == first.started_at
    gate.set()
    assert wait_done(client).state == "done"
    assert RescanStatus.model_validate(client.post("/api/rescan").json()).started  # can run again


def test_failed_rescan_is_reported_without_paths(client, monkeypatch, lib):
    def broken(*args, **kwargs):
        raise OSError(f"disk exploded at {lib}")

    monkeypatch.setattr(rescan_module, "scan_library", broken)
    client.post("/api/rescan")
    s = wait_done(client)
    assert s.state == "failed" and s.error.startswith("OSError")
    assert str(lib) not in s.error
    assert client.get("/health").status_code == 200  # the server keeps running


# ======================================================== rescan while streaming


@pytest.fixture
def live(lib, ids, _isolated_index, _isolated_cache, tmp_path):
    big = lib / "Lớn.mp4"
    with open(big, "wb") as f:
        f.write(os.urandom(1024 * 1024) * 48)  # 48 MB
    settings = settings_for(lib, _isolated_index, _isolated_cache, tmp_path)
    with LibraryIndex(_isolated_index) as index:
        index.apply_scan(scan_library([lib], EXTS, known_files=index.known_files()))
        big_id = next(i.id for i in index.items() if i.name == "Lớn.mp4")
    sock = bind_socket("127.0.0.1", 0)
    app = create_app(settings)
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}", big_id, big, app
    finally:
        app.state.rescanner.wait(60)
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


def test_rescan_during_streaming_does_not_interrupt(live, lib):
    base, big_id, big, app = live
    expected = hashlib.sha256(big.read_bytes()).hexdigest()
    digest, received = hashlib.sha256(), 0
    with httpx.Client(timeout=30) as http, http.stream("GET", f"{base}/stream/{big_id}") as r:
        assert r.status_code == 200
        chunks = r.iter_bytes(64 * 1024)
        for _ in range(16):  # ~1 MB, then trigger the rescan mid-stream
            chunk = next(chunks)
            digest.update(chunk)
            received += len(chunk)
        # Changes so that the rescan really writes to the index while we stream.
        shutil.copy(lib / MOVIE, lib / "Phim lẻ" / "Thêm trong lúc phát.mp4")
        (lib / BROKEN).unlink()
        assert httpx.post(f"{base}/api/rescan").json()["started"] is True
        status = app.state.rescanner.wait(60)
        assert status.state == "done", status
        assert (status.counts.added, status.counts.removed) == (1, 1)
        for chunk in chunks:  # the rest of the file, after the rescan finished
            digest.update(chunk)
            received += len(chunk)
    assert received == big.stat().st_size
    assert digest.hexdigest() == expected


# ======================================================== end to end (subprocess)


def test_e2e_scan_list_play(lib, tmp_path):
    """Fresh hub, empty index: rescan through the API → list → details → play a file."""
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HUB_", "APP_ENV", "LOG_LEVEL"))}
    env.update(PYTHONIOENCODING="utf-8", HUB_INDEX_PATH=str(tmp_path / "e2e" / "library.db"),
               HUB_CACHE_DIR=str(tmp_path / "e2e" / "cache"))
    proc = subprocess.Popen(
        [sys.executable, "-m", "source.pipeline", "--path", str(lib), "--port", str(port),
         "--env-file", str(tmp_path / "none.env")],
        cwd=PROJECT_ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8")
    base = f"http://127.0.0.1:{port}"
    try:
        _wait_up(base, proc)
        assert httpx.get(f"{base}/api/library").json()["total"] == 0  # empty index at first
        assert httpx.post(f"{base}/api/rescan").status_code == 202
        deadline = time.monotonic() + 60
        while (s := httpx.get(f"{base}/api/rescan").json())["state"] == "running":
            assert time.monotonic() < deadline
            time.sleep(0.2)
        assert s["state"] == "done" and s["counts"]["found"] == 6

        listing = LibraryPage.model_validate(httpx.get(f"{base}/api/library", params={"q": "cuoc chien"}).json())
        assert listing.total == 1
        summary: MediaSummary = listing.items[0]
        detail = MediaDetail.model_validate(httpx.get(base + summary.detail_url).json())
        assert len(detail.subtitles) == 2
        r = httpx.get(base + detail.stream_url, headers={"Range": "bytes=100-1099"})
        assert r.status_code == 206
        assert r.content == (lib / MOVIE).read_bytes()[100:1100]
        thumb = httpx.get(base + detail.thumbnail_url)
        assert thumb.status_code == 200 and thumb.content[:2] == b"\xff\xd8"
        if not REAL_TOOLS.missing:
            assert detail.info_status == "ok" and thumb.content != DEFAULT_THUMBNAIL.read_bytes()
    finally:
        proc.kill()
        _, log = proc.communicate(timeout=10)
    assert "Rescan done" in log, log[-2000:]
    assert "Traceback" not in log, log[-3000:]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_up(base: str, proc: subprocess.Popen, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"server exited early ({proc.returncode}): {proc.stderr.read()}")
        try:
            httpx.get(f"{base}/health", timeout=1)
            return
        except httpx.TransportError:
            time.sleep(0.2)
    pytest.fail("server did not start")
