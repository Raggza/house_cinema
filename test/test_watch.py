"""Tests for plan step 4.3: the player page /watch/<id>, in a real browser (Edge/Chromium).

A 60-second H.264/AAC video with Vietnamese and English subtitles is generated for these
tests (the sample library's videos are 2 s long: too short to seek or resume).

Recording, to watch the tests afterwards (files in test-artifacts/, git-ignored):
- HUB_E2E_RECORD=1: a Playwright trace per test (screenshots of every step, network, console);
  open it with `uv run playwright show-trace test-artifacts/trace-<test>.zip`.
- HUB_E2E_RECORD=video: the traces plus a .webm video per test. Needs Playwright's own
  ffmpeg once: `uv run playwright install ffmpeg`.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from source.modules.library import LibraryIndex, scan_library
from source.modules.library.media_info import process_media
from source.modules.server import bind_socket, create_app
from source.utils.config import load_settings
from source.utils.ffmpeg import FFTools

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Page, expect, sync_playwright  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LIBRARY = PROJECT_ROOT / "test" / "fixtures" / "library"
ARTIFACTS = PROJECT_ROOT / "test-artifacts"
EXTS = (".mp4", ".mkv", ".webm")
TOOLS = FFTools(ffmpeg=shutil.which("ffmpeg"), ffprobe=shutil.which("ffprobe"))
RECORD = os.environ.get("HUB_E2E_RECORD", "") in ("1", "video")
RECORD_VIDEO = os.environ.get("HUB_E2E_RECORD", "") == "video"
pytestmark = pytest.mark.skipif(TOOLS.missing != [], reason="ffmpeg/ffprobe needed to make the test video")

SRT_VI = ("1\n00:00:00,000 --> 00:00:04,000\nXin chào, phụ đề tiếng Việt\n\n"
          "2\n00:00:30,000 --> 00:00:34,000\nĐã tới giây thứ ba mươi\n")
SRT_EN = ("1\n00:00:00,000 --> 00:00:04,000\nHello, English subtitles\n\n"
          "2\n00:00:30,000 --> 00:00:34,000\nThirty seconds in\n")


# ======================================================================= library + hub


@pytest.fixture(scope="module")
def library(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("watch") / "Phim"
    (root / "Dài").mkdir(parents=True)
    long_mp4 = root / "Dài" / "Phim dài 60 giây.mp4"
    subprocess.run(
        [TOOLS.ffmpeg, "-v", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=duration=60:size=320x180:rate=15",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=60",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "15",
         "-c:a", "aac", "-shortest", "-movflags", "+faststart", str(long_mp4)],
        check=True, timeout=120)
    (root / "Dài" / "Phim dài 60 giây.vi.srt").write_text(SRT_VI, encoding="utf-8")
    (root / "Dài" / "Phim dài 60 giây.en.srt").write_text(SRT_EN, encoding="utf-8")
    for rel in ("Phim bộ/Mùa 1/Tập 02 HEVC.mkv", "Hỏng/File hỏng.mp4", "Phim lẻ/Tài liệu/Đại dương xanh.webm"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(LIBRARY / rel, root / rel)
    return root


@contextlib.contextmanager
def live_hub(lib: Path, index: Path, cache: Path, tmp: Path, **env):
    with LibraryIndex(index) as idx:
        idx.apply_scan(scan_library([lib], EXTS, known_files=idx.known_files()))
        process_media(idx, TOOLS, cache, workers=2)
        ids = {i.relative_path: i.id for i in idx.items()}
    settings = load_settings(environ={"HUB_INDEX_PATH": str(index), "HUB_CACHE_DIR": str(cache), **env},
                             env_file=tmp / "none.env", cli_paths=[str(lib)])
    app = create_app(settings)
    sock = bind_socket("127.0.0.1", 0)
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}", ids
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


@pytest.fixture(scope="module")
def hub(library, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("hub")
    with live_hub(library, tmp / "library.db", tmp / "cache", tmp) as h:
        yield h


@pytest.fixture(scope="module")
def hub_no_convert(library, tmp_path_factory):
    """The same hub with conversion (step 5.1) turned off: what 4.3 showed for every
    unsupported format, and what a hub without ffmpeg still shows."""
    tmp = tmp_path_factory.mktemp("hub-off")
    with live_hub(library, tmp / "library.db", tmp / "cache", tmp, HUB_TRANSCODE="false") as h:
        yield h


LONG = "Dài/Phim dài 60 giây.mp4"
HEVC = "Phim bộ/Mùa 1/Tập 02 HEVC.mkv"
BROKEN = "Hỏng/File hỏng.mp4"


# ======================================================================= browser


@pytest.fixture(scope="module")
def browser():
    args = ["--autoplay-policy=no-user-gesture-required"]  # as if the user pressed play
    with sync_playwright() as p:
        try:
            b = p.chromium.launch(channel="msedge", headless=True, args=args)
        except Exception:
            # Playwright's own Chromium has no H.264 decoder: these tests need Edge (or Chrome).
            pytest.skip("Microsoft Edge is needed to play H.264 in these tests")
        yield b
        b.close()


@pytest.fixture
def page(browser, request):
    options = {"viewport": {"width": 1280, "height": 800}}
    if RECORD_VIDEO:
        options["record_video_dir"] = str(ARTIFACTS / "videos")
    ctx = browser.new_context(**options)
    if RECORD:
        ctx.tracing.start(screenshots=True, snapshots=True)
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: m.type == "error" and "Failed to load resource" not in m.text
          and errors.append(m.text))
    try:
        yield pg
        assert errors == [], errors
    finally:
        if RECORD:
            ARTIFACTS.mkdir(exist_ok=True)
            ctx.tracing.stop(path=str(ARTIFACTS / f"trace-{request.node.name}.zip"))
        ctx.close()


def wait_ready(page: Page) -> None:
    page.wait_for_function("document.querySelector('#video').readyState >= 2", timeout=15000)


def current_time(page: Page) -> float:
    return page.evaluate("document.querySelector('#video').currentTime")


def wait_time(page: Page, predicate: str) -> None:
    page.wait_for_function(f"(t => {predicate})(document.querySelector('#video').currentTime)", timeout=15000)


# ======================================================================= playing


def test_plays_and_seeks(page, hub):
    base, ids = hub
    ranges: list[str] = []
    page.on("request", lambda r: "/stream/" in r.url and ranges.append(r.headers.get("range", "")))
    page.goto(f"{base}/watch/{ids[LONG]}")
    expect(page.locator("#title")).to_have_text("Phim dài 60 giây")
    expect(page.locator("#where")).to_have_text("Phim › Dài")
    expect(page.locator("#meta")).to_contain_text("1:00 · 320×180")
    wait_ready(page)
    wait_time(page, "t > 0.5")  # it plays
    assert page.evaluate("document.querySelector('#video').videoWidth") == 320  # picture decoded
    assert page.title() == "Phim dài 60 giây – House Cinema"

    page.evaluate("document.querySelector('#video').currentTime = 40")  # seek forward
    wait_time(page, "t >= 40 && t < 45")
    wait_time(page, "t > 40.5")  # still playing after the seek
    assert page.evaluate("document.querySelector('#video').paused") is False
    assert any(r.startswith("bytes=") for r in ranges), ranges  # seeking uses Range requests
    expect(page.locator("#notice")).to_be_hidden()


def test_keyboard_shortcuts(page, hub):
    base, ids = hub
    page.goto(f"{base}/watch/{ids[LONG]}")
    wait_ready(page)
    page.locator("#title").click()  # focus on the page, not on the video's own controls
    page.evaluate("v => { const x = document.querySelector('#video'); x.pause(); x.currentTime = 20; }", None)
    wait_time(page, "Math.abs(t - 20) < 0.5")
    page.keyboard.press("ArrowRight")
    wait_time(page, "Math.abs(t - 30) < 0.5")
    page.keyboard.press("ArrowLeft")
    page.keyboard.press("ArrowLeft")
    wait_time(page, "Math.abs(t - 10) < 0.5")
    page.keyboard.press(" ")
    page.wait_for_function("!document.querySelector('#video').paused")
    page.keyboard.press("k")
    page.wait_for_function("document.querySelector('#video').paused")
    page.keyboard.press("m")
    assert page.evaluate("document.querySelector('#video').muted") is True
    page.keyboard.press("f")
    page.wait_for_function("document.fullscreenElement === document.querySelector('#video')")
    page.keyboard.press("f")
    page.wait_for_function("document.fullscreenElement === null")


# ======================================================================= subtitles


def active_cue(page: Page) -> str:
    return page.evaluate("""() => {
        const t = [...document.querySelector('#video').textTracks].find(t => t.mode === 'showing');
        return t && t.activeCues && t.activeCues.length ? t.activeCues[0].text : '';
    }""")


def test_subtitles_default_vietnamese_and_choice_is_remembered(page, hub):
    base, ids = hub
    page.goto(f"{base}/watch/{ids[LONG]}")
    wait_ready(page)
    expect(page.locator("#subtitles option")).to_have_text(["Tắt", "English", "Tiếng Việt"])
    expect(page.locator("#subtitles")).to_have_value("vi")  # Vietnamese first when present
    page.evaluate("document.querySelector('#video').currentTime = 31")
    page.wait_for_function("document.querySelector('#video').textTracks.length === 2")
    page.wait_for_function("""() => [...document.querySelector('#video').textTracks]
        .some(t => t.mode === 'showing' && t.activeCues && t.activeCues.length)""")
    assert active_cue(page) == "Đã tới giây thứ ba mươi"  # right text at the right time

    page.select_option("#subtitles", "en")
    page.wait_for_function("""() => [...document.querySelector('#video').textTracks]
        .some(t => t.language === 'en' && t.mode === 'showing' && t.activeCues && t.activeCues.length)""")
    assert active_cue(page) == "Thirty seconds in"
    modes = page.evaluate("[...document.querySelector('#video').textTracks].map(t => t.language + ':' + t.mode)")
    assert sorted(modes) == ["en:showing", "vi:disabled"]

    page.reload()
    wait_ready(page)
    expect(page.locator("#subtitles")).to_have_value("en")  # remembered in this browser
    page.locator("#title").click()
    page.keyboard.press("c")  # C toggles subtitles
    expect(page.locator("#subtitles")).to_have_value("off")
    expect(page.locator("#toast")).to_have_text("Đã tắt phụ đề")


def test_video_without_subtitles_has_no_selector(page, hub):
    base, ids = hub
    page.goto(f"{base}/watch/{ids['Phim lẻ/Tài liệu/Đại dương xanh.webm']}")
    wait_ready(page)
    expect(page.locator("#sub-select")).to_be_hidden()
    expect(page.locator("#notice")).to_be_hidden()


# ======================================================================= resume


def test_resume_where_you_stopped(page, hub):
    base, ids = hub
    page.goto(f"{base}/watch/{ids[LONG]}")
    wait_ready(page)
    page.evaluate("document.querySelector('#video').currentTime = 30")
    wait_time(page, "t >= 30")
    page.evaluate("document.querySelector('#video').pause()")  # saves the position
    saved = page.evaluate(f"JSON.parse(localStorage.getItem('hub:pos:{ids[LONG]}'))")
    assert 30 <= saved["t"] < 32 and saved["d"] == pytest.approx(60, abs=1)

    page.goto("about:blank")  # "close the tab", then open the video again
    page.goto(f"{base}/watch/{ids[LONG]}")
    wait_ready(page)
    wait_time(page, "t >= 30 && t < 34")
    expect(page.locator("#resume")).to_contain_text("Đang phát tiếp từ 0:30")

    page.get_by_role("button", name="Xem từ đầu").click()
    wait_time(page, "t < 3")
    expect(page.locator("#resume")).to_be_hidden()


def test_near_the_end_counts_as_finished(page, hub):
    base, ids = hub
    page.goto(f"{base}/watch/{ids[LONG]}")
    wait_ready(page)
    page.evaluate("document.querySelector('#video').currentTime = 55")  # last 10 s
    wait_time(page, "t >= 55")
    page.evaluate("document.querySelector('#video').pause()")
    assert page.evaluate(f"localStorage.getItem('hub:pos:{ids[LONG]}')") is None
    page.reload()
    wait_ready(page)
    assert current_time(page) < 3
    expect(page.locator("#resume")).to_be_hidden()


# ======================================================================= cannot play


def test_unsupported_format_says_so_with_codec(page, hub_no_convert):
    base, ids = hub_no_convert
    streams: list[str] = []
    page.on("request", lambda r: "/stream/" in r.url and streams.append(r.url))
    page.goto(f"{base}/watch/{ids[HEVC]}")
    notice = page.locator("#notice")
    expect(notice).to_be_visible()
    expect(notice.locator("h2")).to_have_text("Định dạng chưa hỗ trợ")
    expect(notice).to_contain_text("Hình: hevc")
    expect(notice).to_contain_text("matroska (.mkv)")
    expect(notice).to_contain_text("HEVC")  # the hub's reason (playable_note)
    expect(notice).to_contain_text("Hub không chuyển đổi được: Chức năng chuyển đổi định dạng đang tắt")
    expect(notice.get_by_role("button", name="Vẫn thử phát")).to_be_visible()
    expect(notice.get_by_role("button", name="Sao chép link cho VLC")).to_be_visible()
    assert streams == []  # nothing downloaded until the user asks to try anyway

    # "Try anyway": this browser either really plays it (HEVC support installed), or says
    # it got no picture (sound only) / cannot decode it, instead of a silent black screen.
    notice.get_by_role("button", name="Vẫn thử phát").click()
    page.wait_for_function("""() => {
        const v = document.querySelector('#video');
        return (v.videoWidth > 0 && v.currentTime > 0.2) || !document.querySelector('#notice').hidden;
    }""", timeout=15000)
    if page.locator("#notice").is_visible():
        expect(page.locator("#notice")).to_contain_text(re.compile("không giải mã được|không phát được"))


def garbage_stream(page, media_id):
    page.route(f"**/stream/{media_id}", lambda route: route.fulfill(
        status=200, headers={"Content-Type": "video/mp4"}, body=b"\x00\x00\x00\x18ftypisom" + b"\xde\xad" * 4000))


def test_playback_error_at_runtime_shows_a_message(page, hub_no_convert):
    """The hub said 'playable' but the browser fails: a message, not a silent black box."""
    base, ids = hub_no_convert
    garbage_stream(page, ids[LONG])
    page.goto(f"{base}/watch/{ids[LONG]}")
    expect(page.locator("#notice h2")).to_have_text("Định dạng chưa hỗ trợ", timeout=15000)
    expect(page.locator("#notice")).to_contain_text("Hình: h264")


def test_playback_error_at_runtime_falls_back_to_conversion(page, hub):
    """Same failure with conversion on (5.1): the hub's remuxed copy is played instead."""
    base, ids = hub
    garbage_stream(page, ids[LONG])
    page.goto(f"{base}/watch/{ids[LONG]}")
    page.wait_for_function("""() => { const v = document.querySelector('#video');
        return v.currentTime > 0.5 && v.videoWidth > 0 && v.src.startsWith('blob:'); }""", timeout=30000)
    expect(page.locator("#notice")).to_be_hidden()


def test_broken_file(page, hub):
    base, ids = hub
    page.goto(f"{base}/watch/{ids[BROKEN]}")
    expect(page.locator("#notice h2")).to_have_text("Không đọc được file video")
    expect(page.locator("#notice")).to_contain_text("moov atom not found")


@pytest.mark.parametrize("media_id", ["0123456789abcdef", "khong-co"])
def test_unknown_video(page, hub, media_id):
    base, _ = hub
    page.goto(f"{base}/watch/{media_id}")
    expect(page.locator("#notice h2")).to_have_text("Không tìm thấy video")
    expect(page.get_by_role("button", name="Về thư viện")).to_be_visible()


def test_disconnected_drive(page, tmp_path):
    lib = tmp_path / "USB"
    (lib / "x").mkdir(parents=True)
    shutil.copy(LIBRARY / "Phim lẻ" / "Hành động" / "Cuộc Chiến Cuối Cùng.mp4", lib / "x" / "Phim.mp4")
    index, cache = tmp_path / "library.db", tmp_path / "cache"
    with live_hub(lib, index, cache, tmp_path) as (base, ids):
        lib.rename(tmp_path / "rút ra")  # the USB drive is unplugged, then a rescan runs
        with LibraryIndex(index) as idx:
            idx.apply_scan(scan_library([lib], EXTS, known_files=idx.known_files()))
        page.goto(f"{base}/watch/{ids['x/Phim.mp4']}")
        expect(page.locator("#notice h2")).to_have_text("Ổ chứa video đang ngắt kết nối")


# ======================================================================= from the library


def test_card_opens_player_and_back_returns(page, hub):
    base, ids = hub
    page.goto(f"{base}/?folder=D%C3%A0i")
    page.locator("a.card").first.click()
    expect(page).to_have_url(re.compile(rf"/watch/{ids[LONG]}$"))
    wait_ready(page)
    page.get_by_role("link", name="Thư viện").click()
    expect(page).to_have_url(re.compile(r"/\?folder=D%C3%A0i$"))
    expect(page.locator(".card .title")).to_have_text(["Phim dài 60 giây"])


def test_phone_layout(browser, hub):
    base, ids = hub
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True)
    page = ctx.new_page()
    try:
        page.goto(f"{base}/watch/{ids[LONG]}")
        wait_ready(page)
        box = page.locator("#video").bounding_box()
        assert box["width"] >= 380  # full width
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        expect(page.locator(".keys")).to_be_hidden()  # no keyboard hints on a touch screen
    finally:
        ctx.close()
