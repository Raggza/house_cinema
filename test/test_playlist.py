"""Tests for plan step 6.5: several videos — "Phát tất cả" (one folder, not its sub-folders,
in the order shown), "Chọn" (played in the order picked), the playlist in the player (next on
end, Trước/Sau, shuffle, repeat, skipping videos that cannot play) and on the TV (play_list,
next/previous, playlist_mode), in a real browser (Edge: H.264).

The clips are 3 s long, so "ended" comes quickly.
"""

from __future__ import annotations

import contextlib
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from source.modules.library import LibraryIndex, scan_library
from source.modules.library.media_info import process_media
from source.modules.server import bind_socket, create_app
from source.modules.server.runner import HubServer
from source.utils.config import load_settings
from source.utils.ffmpeg import FFTools

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Page, expect, sync_playwright  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLS = FFTools(ffmpeg=shutil.which("ffmpeg"), ffprobe=shutil.which("ffprobe"))
EXTS = (".mp4", ".mkv")
pytestmark = pytest.mark.skipif(TOOLS.missing != [], reason="ffmpeg/ffprobe needed to make the test videos")

SEASON = "Phim bộ/Mùa 1"
EP1, EP2, EP3_BROKEN, EP10 = (f"{SEASON}/Tập 1.mp4", f"{SEASON}/Tập 2.mp4", f"{SEASON}/Tập 3 hỏng.mp4",
                              f"{SEASON}/Tập 10.mp4")
EXTRA = f"{SEASON}/Ngoại truyện/Tập đặc biệt.mp4"  # a sub-folder: never in "Phát tất cả" of Mùa 1
OTHER = "Khác/Phim khác.mp4"
BROKEN = [f"Hỏng/Hỏng {n}.mp4" for n in (1, 2, 3, 4)]
HEVC = "Chuyển đổi/Phim HEVC.mkv"

# Pretend this browser cannot decode HEVC: the hub converts it (step 5.1), as on many phones.
NO_HEVC = """(() => {
  const no = (t) => /hvc1|hev1|hevc/i.test(String(t));
  const cpt = HTMLMediaElement.prototype.canPlayType;
  HTMLMediaElement.prototype.canPlayType = function (t) { return no(t) ? "" : cpt.call(this, t); };
  const its = MediaSource.isTypeSupported.bind(MediaSource);
  MediaSource.isTypeSupported = (t) => !no(t) && its(t);
})();"""


# ======================================================================= library + hub


def _clip(path: Path, seconds: int, freq: int, codec: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([TOOLS.ffmpeg, "-v", "error", "-y",
                    "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=320x180:rate=15",
                    "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
                    *codec, "-shortest", str(path)], check=True, timeout=120)


H264 = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "15", "-c:a", "aac",
        "-movflags", "+faststart"]


@pytest.fixture(scope="module")
def library(tmp_path_factory) -> tuple[Path, Path, dict[str, str]]:
    root = tmp_path_factory.mktemp("pl") / "Thư viện"
    for i, rel in enumerate((EP1, EP2, EP10, EXTRA, OTHER)):
        _clip(root / rel, 3, 300 + 100 * i, H264)
    for rel in (EP3_BROKEN, *BROKEN):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(b"not a video " * 20)
    _clip(root / HEVC, 30, 900, ["-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=error",
                                 "-pix_fmt", "yuv420p", "-c:a", "aac"])
    index = tmp_path_factory.mktemp("plidx") / "library.db"
    with LibraryIndex(index) as idx:
        idx.apply_scan(scan_library([root], EXTS, known_files=idx.known_files()))
        process_media(idx, TOOLS, index.parent / "cache", workers=2)
        ids = {i.relative_path: i.id for i in idx.items()}
    return root, index, ids


def settings(root: Path, index: Path, **env):
    return load_settings(environ={"HUB_INDEX_PATH": str(index), "HUB_CACHE_DIR": str(index.parent / "cache"), **env},
                         env_file=index.parent / "none.env", cli_paths=[str(root)])


@contextlib.contextmanager
def live_hub(root: Path, index: Path, **env):
    app = create_app(settings(root, index, **env))
    sock = bind_socket("127.0.0.1", 0)
    server = HubServer(uvicorn.Config(app, log_config=None, lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}", app
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        sock.close()


@pytest.fixture(scope="module")
def hub(library):
    root, index, ids = library
    with live_hub(root, index) as (base, app):
        yield base, ids, app


# ======================================================================= browser


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch(channel="msedge", headless=True, args=["--autoplay-policy=no-user-gesture-required"])
        except Exception:
            pytest.skip("Microsoft Edge is needed to play H.264 in these tests")
        yield b
        b.close()


@contextlib.contextmanager
def open_page(browser, *, init_script: str | None = None, **ctx_options):
    ctx = browser.new_context(**{"viewport": {"width": 1280, "height": 860}, **ctx_options})
    if init_script:
        ctx.add_init_script(init_script)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: m.type == "error" and "Failed to load resource" not in m.text
            and errors.append(m.text))
    try:
        yield page
        assert errors == [], errors
    finally:
        ctx.close()


def titles_in_panel(page: Page) -> list[str]:
    return page.locator("#pl-items .pl-name").all_text_contents()


def wait_playing(page: Page, timeout=20000) -> None:
    page.wait_for_function("(() => { const v = document.querySelector('#video');"
                           " return v.currentTime > 0.3 && !v.paused; })()", timeout=timeout)


def mark_document(page: Page) -> None:
    """Set a marker that a page reload would lose: the next video must open in this page."""
    page.evaluate("window.__samePage = true")


def same_document(page: Page) -> bool:
    return page.evaluate("window.__samePage === true")


def open_folder(page: Page, base: str, folder: str) -> None:
    page.goto(f"{base}/?folder={folder}")
    page.wait_for_selector("#grid[aria-busy=false]", state="attached")


# ======================================================================= playlist logic (playlist.js)


def test_playlist_logic(browser, hub):
    base, _, _ = hub
    with open_page(browser) as page:
        page.goto(base + "/")
        r = page.evaluate("""async () => {
          const PL = await import('/static/playlist.js');
          const items = ['a','b','c','d','e'].map((id) => ({ id, title: id.toUpperCase() }));
          const out = {};
          let l = PL.makeList([...items, items[1], { id: '' }, null], { index: 2, title: 'T' });
          out.dedupe = l.items.map((i) => i.id).join('');
          out.start = PL.current(l).id;
          out.next = PL.itemAt(l, PL.nextPos(l)).id;
          l.pos = 4; out.endOff = PL.nextPos(l); out.endAuto = PL.nextPos(l, { auto: true });
          PL.setRepeat(l, 'all'); out.wrap = PL.nextPos(l);
          l.pos = 0; out.prevWrap = PL.prevPos(l);
          PL.setRepeat(l, 'one'); l.pos = 3; out.oneAuto = PL.nextPos(l, { auto: true }); out.oneManual = PL.nextPos(l);
          out.cycle = [PL.cycleRepeat(l), PL.cycleRepeat(l), PL.cycleRepeat(l)].join(',');
          // shuffle: the video playing stays first; every video once; off: back to the list order
          l.pos = 2;
          let seq = 0; const rand = () => ((seq = (seq * 9301 + 49297) % 233280) / 233280);
          PL.setShuffle(l, true, rand);
          out.shuffleFirst = PL.current(l).id + '@' + l.pos;
          out.perm = [...l.order].sort().join('');
          PL.setShuffle(l, false);
          out.unshuffle = PL.current(l).id + '@' + l.pos + ':' + l.order.join('');
          const many = Array.from({ length: 600 }, (_, i) => ({ id: 'v' + i, title: '' }));
          out.cap = PL.makeList(many).items.length;
          PL.saveList(l);
          out.load = PL.loadList(l.id) !== null && PL.loadList('other') === null;
          out.url = PL.watchUrl(l);
          return out;
        }""")
    assert r["dedupe"] == "abcde" and r["start"] == "c" and r["next"] == "d"
    assert r["endOff"] is None and r["endAuto"] is None and r["wrap"] == 0 and r["prevWrap"] == 4
    assert r["oneAuto"] == 3 and r["oneManual"] == 4  # "Lặp 1 video" replays; "Sau" still moves on
    assert r["cycle"] == "off,all,one"
    assert r["shuffleFirst"] == "c@0" and r["perm"] == "01234"
    assert r["unshuffle"] == "c@2:01234"
    assert r["cap"] == 500 and r["load"] is True
    assert re.fullmatch(r"/watch/c\?list=[0-9a-f]{16}&p=2", r["url"])


# ======================================================================= "Phát tất cả"


def test_play_all_plays_the_folder_in_order_next_on_end_and_skips_what_cannot_play(browser, hub):
    base, ids, _ = hub
    with open_page(browser) as page:
        open_folder(page, base, "Phim%20b%E1%BB%99%2FM%C3%B9a%201")
        expect(page.locator("#play-all")).to_be_visible()
        page.click("#play-all")
        page.wait_for_url(re.compile(rf"/watch/{ids[EP1]}\?list=[0-9a-f]{{16}}&p=0$"))
        # This folder only, natural order ("Tập 2" before "Tập 10"), not the sub-folder's video.
        expect(page.locator("#pl-items .pl-name")).to_have_text(["Tập 1", "Tập 2", "Tập 3 hỏng", "Tập 10"])
        expect(page.locator("#pl-pos")).to_have_text("1/4")
        expect(page.locator("#pl-title")).to_have_text("Danh sách phát · Mùa 1")
        expect(page.locator("#pl-prev")).to_be_disabled()
        mark_document(page)

        # End of "Tập 1" → "Tiếp theo: Tập 2" → "Phát ngay": the next one, in this same page.
        expect(page.locator("#upnext")).to_be_visible(timeout=15000)
        expect(page.locator("#upnext-text")).to_contain_text("Tiếp theo: Tập 2")
        page.click("#upnext-now")
        expect(page.locator("#title")).to_have_text("Tập 2")
        assert re.search(rf"/watch/{ids[EP2]}\?list=\w+&p=1$", page.url)
        expect(page.locator("#pl-items [aria-current]")).to_have_text("2Tập 2")
        wait_playing(page)
        assert same_document(page)

        # "Huỷ" stops the countdown: no next video.
        expect(page.locator("#upnext")).to_be_visible(timeout=15000)
        page.click("#upnext-cancel")
        expect(page.locator("#upnext")).to_be_hidden()
        page.wait_for_timeout(6000)
        expect(page.locator("#title")).to_have_text("Tập 2")

        # Trước / the list panel / the keyboard (N).
        page.click("#pl-prev")
        expect(page.locator("#title")).to_have_text("Tập 1")
        page.locator("#pl-items .pl-item").nth(1).click()
        expect(page.locator("#title")).to_have_text("Tập 2")
        page.locator("body").press("n")

        # "Tập 3 hỏng" cannot be read: its notice, then on to "Tập 10" by itself (countdown).
        expect(page.locator("#notice h2")).to_have_text("Không đọc được file video")
        expect(page.locator("#upnext-text")).to_contain_text("Chuyển sang: Tập 10")
        expect(page.locator("#title")).to_have_text("Tập 10", timeout=12000)
        wait_playing(page)
        expect(page.locator("#notice")).to_be_hidden()

        # After the last one: "Đã phát hết danh sách".
        expect(page.locator("#notice h2")).to_have_text("Đã phát hết danh sách", timeout=15000)
        expect(page.locator("#pl-next")).to_be_disabled()
        page.get_by_role("button", name="Phát lại từ đầu").click()
        expect(page.locator("#title")).to_have_text("Tập 1")
        assert same_document(page)

        # A reload keeps the playlist and the place in it.
        page.locator("#pl-items .pl-item").nth(3).click()
        expect(page.locator("#title")).to_have_text("Tập 10")
        page.reload()
        expect(page.locator("#pl-pos")).to_have_text("4/4")
        expect(page.locator("#title")).to_have_text("Tập 10")


def test_no_play_all_for_search_results_or_an_empty_folder(browser, hub):
    base, _, _ = hub
    with open_page(browser) as page:
        page.goto(base + "/?q=tap")
        page.wait_for_selector("#grid[aria-busy=false]", state="attached")
        expect(page.locator("#play-all")).to_be_hidden()  # several folders: only "Chọn"
        expect(page.locator("#select-mode")).to_be_visible()
        open_folder(page, base, "Phim%20b%E1%BB%99")  # only sub-folders, no video of its own
        expect(page.locator("#play-all")).to_be_hidden()


def test_shuffle_and_repeat_on_the_playlist_bar(browser, hub):
    base, ids, _ = hub
    with open_page(browser) as page:
        open_folder(page, base, "Phim%20b%E1%BB%99%2FM%C3%B9a%201")
        page.click("#play-all")
        expect(page.locator("#title")).to_have_text("Tập 1")
        page.click("#pl-shuffle")
        expect(page.locator("#pl-shuffle")).to_have_attribute("aria-pressed", "true")
        names = titles_in_panel(page)
        assert names[0] == "Tập 1" and sorted(names) == sorted(["Tập 1", "Tập 2", "Tập 3 hỏng", "Tập 10"])
        expect(page.locator("#pl-pos")).to_have_text("1/4")
        page.click("#pl-shuffle")
        expect(page.locator("#pl-shuffle")).to_have_attribute("aria-pressed", "false")
        expect(page.locator("#pl-items .pl-name")).to_have_text(["Tập 1", "Tập 2", "Tập 3 hỏng", "Tập 10"])

        repeat = page.locator("#pl-repeat")
        expect(repeat).to_have_text("Không lặp")
        repeat.click()
        expect(repeat).to_have_text("Lặp danh sách")
        expect(repeat).to_have_attribute("aria-pressed", "true")
        page.locator("#pl-items .pl-item").nth(3).click()  # the last one
        expect(page.locator("#pl-next")).to_be_enabled()  # wraps to the first
        repeat.click()
        expect(repeat).to_have_text("Lặp 1 video")
        mark_document(page)
        # "Lặp 1 video": at the end the same video starts again, no countdown.
        page.wait_for_function("document.querySelector('#video').currentTime > 2.2", timeout=15000)
        page.wait_for_function("(() => { const v = document.querySelector('#video');"
                               " return v.currentTime < 1.5 && !v.paused; })()", timeout=15000)
        expect(page.locator("#upnext")).to_be_hidden()
        expect(page.locator("#title")).to_have_text("Tập 10")
        assert same_document(page)
        repeat.click()
        expect(repeat).to_have_text("Không lặp")


# ======================================================================= "Chọn"


def test_pick_videos_from_several_folders_and_play_them_in_the_order_picked(browser, hub):
    base, ids, _ = hub
    with open_page(browser, viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True) as page:
        open_folder(page, base, "Phim%20b%E1%BB%99%2FM%C3%B9a%201")
        page.click("#select-mode")
        expect(page.locator("#selbar")).to_be_visible()
        expect(page.locator("#sel-play")).to_be_disabled()
        card = lambda title: page.locator(f".card[data-title='{title}']")  # noqa: E731
        card("Tập 10").click()
        card("Tập 2").click()
        expect(card("Tập 10").locator(".pick")).to_have_text("1")
        expect(card("Tập 2").locator(".pick")).to_have_text("2")
        expect(page.locator("#sel-count")).to_have_text("Đã chọn 2 video")
        assert page.url.endswith("folder=Phim%20b%E1%BB%99%2FM%C3%B9a%201")  # picking did not open a video

        open_folder(page, base, "Kh%C3%A1c")  # another folder: the selection stays
        expect(page.locator("#sel-count")).to_have_text("Đã chọn 2 video")
        card("Phim khác").click()
        expect(card("Phim khác").locator(".pick")).to_have_text("3")

        open_folder(page, base, "Phim%20b%E1%BB%99%2FM%C3%B9a%201")
        card("Tập 10").click()  # picked again = unpicked: the others move up
        expect(card("Tập 2").locator(".pick")).to_have_text("1")
        card("Tập 1").click()
        expect(page.locator("#sel-count")).to_have_text("Đã chọn 3 video")

        page.click("#sel-play")
        page.wait_for_url(re.compile(rf"/watch/{ids[EP2]}\?list="))
        expect(page.locator("#pl-items .pl-name")).to_have_text(["Tập 2", "Phim khác", "Tập 1"])
        expect(page.locator("#pl-title")).to_have_text("Danh sách phát · Video đã chọn")
        wait_playing(page)

        page.goto(base + "/")  # after playing, the selection is cleared
        page.wait_for_selector("#grid[aria-busy=false]", state="attached")
        expect(page.locator("#selbar")).to_be_hidden()


def test_clear_and_done(browser, hub):
    base, _, _ = hub
    with open_page(browser) as page:
        open_folder(page, base, "Kh%C3%A1c")
        page.click("#select-mode")
        page.locator(".card[data-title='Phim khác']").click()
        expect(page.locator("#sel-count")).to_have_text("Đã chọn 1 video")
        page.click("#sel-clear")
        expect(page.locator("#sel-count")).to_contain_text("Bấm vào video để chọn")
        expect(page.locator(".card.picked")).to_have_count(0)
        page.click("#sel-done")
        expect(page.locator("#selbar")).to_be_hidden()
        page.locator(".card[data-title='Phim khác']").click()  # not picking any more: it opens
        page.wait_for_url(re.compile(r"/watch/"))


# ======================================================================= errors


def _start_list(page: Page, base: str, ids: list[str], titles: list[str]) -> None:
    page.goto(base + "/")
    url = page.evaluate("""async ([ids, titles]) => {
      const PL = await import('/static/playlist.js');
      const l = PL.makeList(ids.map((id, i) => ({ id, title: titles[i] })), { title: 'Thử' });
      PL.saveList(l);
      return PL.watchUrl(l, 0);
    }""", [ids, titles])
    page.goto(base + url)


def test_three_failures_in_a_row_stop_the_playlist_and_a_deleted_video_is_skipped(browser, hub):
    base, ids, _ = hub
    with open_page(browser) as page:
        gone = "0123456789abcdef"  # no longer in the library
        _start_list(page, base, [gone, ids[EP1], ids[BROKEN[0]], ids[BROKEN[1]], ids[BROKEN[2]], ids[EP2]],
                    ["Đã xoá", "Tập 1", "Hỏng 1", "Hỏng 2", "Hỏng 3", "Tập 2"])
        expect(page.locator("#notice h2")).to_have_text("Không tìm thấy video")
        expect(page.locator("#upnext-text")).to_contain_text("Chuyển sang: Tập 1")
        page.click("#upnext-now")
        wait_playing(page)  # a video that plays: the count starts again
        page.locator("#pl-items .pl-item").nth(2).click()
        for n in (1, 2):
            expect(page.locator("#title")).to_have_text(f"Hỏng {n}")
            expect(page.locator("#upnext")).to_be_visible()
            page.click("#upnext-now")
        expect(page.locator("#title")).to_have_text("Hỏng 3")
        expect(page.locator("#notice")).to_contain_text("3 video liên tiếp không phát được")
        expect(page.locator("#upnext")).to_be_hidden()
        page.wait_for_timeout(6000)
        expect(page.locator("#title")).to_have_text("Hỏng 3")  # stopped: no endless jumping


def test_busy_hub_holds_the_playlist(browser, library):
    root, index, ids = library
    with live_hub(root, index, HUB_TRANSCODE_MAX_SESSIONS="1") as (base, _):
        # Another viewer takes the only conversion slot.
        taken = httpx.post(f"{base}/api/play/{ids[HEVC]}", json={"video": ["h264"], "audio": ["aac"]})
        assert taken.status_code == 200 and taken.json()["mode"] == "transcode"
        with open_page(browser, init_script=NO_HEVC) as page:
            _start_list(page, base, [ids[HEVC], ids[EP1]], ["Phim HEVC", "Tập 1"])
            expect(page.locator("#notice h2")).to_have_text("Hub đang bận", timeout=20000)
            page.wait_for_timeout(6000)
            expect(page.locator("#upnext")).to_be_hidden()  # the next would be refused the same way
            expect(page.locator("#title")).to_have_text("Phim HEVC")


def test_conversion_of_the_previous_video_stops_when_moving_on(browser, hub):
    base, ids, app = hub
    tc = app.state.transcoder
    with open_page(browser, init_script=NO_HEVC) as page:
        _start_list(page, base, [ids[HEVC], ids[EP1]], ["Phim HEVC", "Tập 1"])
        wait_playing(page, timeout=60000)  # converted by the hub (transcode session)
        assert tc.active_count() == 1
        page.click("#pl-next")
        expect(page.locator("#title")).to_have_text("Tập 1")
        wait_playing(page)
        deadline = time.monotonic() + 10
        while tc.active_count() and time.monotonic() < deadline:
            time.sleep(0.2)
        assert tc.active_count() == 0 and tc.running_pids() == []


# ======================================================================= the TV


def test_play_list_command_checks_every_id(library, tmp_path):
    root, index, ids = library
    with TestClient(create_app(settings(root, index))) as c:
        s = c.post("/api/screens", json={"name": "TV"}).json()
        url = f"/api/screens/{s['id']}/commands"
        for bad in ({"action": "play_list", "media_ids": []},
                    {"action": "play_list", "media_ids": ["a"] * 501},
                    {"action": "play_list", "media_ids": [ids[EP1]], "index": 500},
                    {"action": "play_list", "media_ids": [ids[EP1]], "repeat": "twice"},
                    {"action": "playlist_mode", "repeat": "twice"},
                    {"action": "next", "extra": 1}):
            assert c.post(url, json=bad).status_code == 422, bad
        r = c.post(url, json={"action": "play_list", "media_ids": ["0123456789abcdef", "../../x"]})
        assert r.status_code == 404  # nothing left to play
        # Known ids reach the (not connected) TV: 409, after the check.
        r = c.post(url, json={"action": "play_list", "media_ids": [ids[EP1], "0123456789abcdef"]})
        assert r.status_code == 409


def test_play_list_on_the_tv_with_next_previous_shuffle_repeat(browser, library):
    root, index, ids = library
    with live_hub(root, index) as (base, app):
        tv_ctx = browser.new_context(viewport={"width": 1280, "height": 720})
        phone_ctx = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        tv, phone = tv_ctx.new_page(), phone_ctx.new_page()
        errors: list[str] = []
        for p in (tv, phone):
            p.on("pageerror", lambda e: errors.append(str(e)))
        sent: list[dict] = []
        phone.on("request", lambda r: r.method == "POST" and "/commands" in r.url and sent.append(r.post_data_json))
        try:
            tv.goto(f"{base}/tv")
            tv.wait_for_selector("body[data-connected]")
            code = tv.get_attribute("body", "data-code")
            tv.click("#start")
            phone.goto(f"{base}/remote")
            phone.fill("#code", code)
            phone.click("#code-form button")
            phone.wait_for_selector("body[data-status='idle']")

            # Pick 3 videos (one cannot play) and send them to the TV.
            open_folder(phone, base, "Phim%20b%E1%BB%99%2FM%C3%B9a%201")
            phone.click("#select-mode")
            for title in ("Tập 2", "Tập 3 hỏng", "Tập 1"):
                phone.locator(f".card[data-title='{title}']").click()
            phone.click("#sel-tv")
            phone.wait_for_url(re.compile(r"/remote$"))
            phone.wait_for_selector("#pl-box:not([hidden])")
            expect(phone.locator("#pl-pos")).to_have_text("1/3")
            expect(phone.locator("#pl-next-title")).to_contain_text("Tập 3 hỏng")
            assert sent[0]["action"] == "play_list" and sent[0]["media_ids"] == [ids[EP2], ids[EP3_BROKEN], ids[EP1]]
            tv.wait_for_function(f"document.querySelector('#frame').src.includes('{ids[EP2]}')")
            expect(phone.locator("#now-title")).to_have_text("Tập 2", timeout=15000)

            # "Tập 2" ends → the TV counts down, then "Tập 3 hỏng" cannot play → skipped → "Tập 1".
            expect(phone.locator("#pl-next-title")).to_contain_text("Tiếp theo trong", timeout=15000)
            tv.wait_for_function(f"document.querySelector('#frame').src.includes('{ids[EP1]}')", timeout=30000)
            expect(phone.locator("#pl-pos")).to_have_text("3/3", timeout=10000)
            expect(phone.locator("#pl-next-title")).to_have_text("Đây là video cuối của danh sách.")

            # Trước from the phone: back to the 2nd (it cannot play: the TV moves on again).
            phone.click("#pl-prev")
            expect(phone.locator("#pl-pos")).to_have_text("2/3", timeout=10000)

            # Shuffle / repeat from the phone's playlist bar.
            phone.click("#pl-repeat")
            expect(phone.locator("#pl-repeat")).to_have_text("Lặp danh sách", timeout=10000)
            phone.click("#pl-shuffle")
            expect(phone.locator("#pl-shuffle")).to_have_attribute("aria-pressed", "true", timeout=10000)
            expect(phone.locator("#pl-pos")).to_have_text("1/3", timeout=10000)  # the current one comes first
            phone.click("#pl-shuffle")
            expect(phone.locator("#pl-shuffle")).to_have_attribute("aria-pressed", "false", timeout=10000)

            # Sau from the phone.
            phone.click("#pl-next")
            tv.wait_for_function(f"document.querySelector('#frame').src.includes('{ids[EP1]}')", timeout=15000)

            # Repeat off, let the last one end: the TV says the list is over.
            phone.click("#pl-repeat")  # all → one (the phone sends the mode after the TV's)
            expect(phone.locator("#pl-repeat")).to_have_text("Lặp 1 video", timeout=10000)
            phone.click("#pl-repeat")  # one → off
            expect(phone.locator("#pl-repeat")).to_have_text("Không lặp", timeout=10000)
            tv.wait_for_selector("#idle:not([hidden])", timeout=30000)
            expect(tv.locator("#headline")).to_contain_text("Đã phát hết danh sách")
            phone.wait_for_selector("body[data-status='idle']")
            expect(phone.locator("#pl-box")).to_be_hidden()
        finally:
            tv_ctx.close()
            phone_ctx.close()
    assert errors == [], errors
