"""Tests for plan step 4.1: the library page, in a real browser (Playwright + Edge/Chromium).

Phone and desktop sizes; search; folder navigation; paging by scrolling; and every
"nothing to show" case (empty library, no result, API error, hub offline) shows a message
instead of a blank page.
"""

from __future__ import annotations

import contextlib
import re
import shutil
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from source.modules.library import LibraryIndex, read_known_root_paths, scan_library
from source.modules.server import bind_socket, create_app
from source.utils.config import load_settings

playwright_api = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Page, expect, sync_playwright  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LIBRARY = PROJECT_ROOT / "test" / "fixtures" / "library"
EXTS = (".mp4", ".mkv", ".webm")
PHONE = {"width": 390, "height": 844}
DESKTOP = {"width": 1366, "height": 800}


# ======================================================================= server


@contextlib.contextmanager
def live_server(lib_paths, index_path, cache, tmp_path):
    settings = load_settings(environ={"HUB_INDEX_PATH": str(index_path), "HUB_CACHE_DIR": str(cache)},
                             env_file=tmp_path / "none.env", cli_paths=[str(p) for p in lib_paths],
                             known_roots_reader=read_known_root_paths)  # as the real hub: an unplugged
                                                                       # but known folder is allowed
    sock = bind_socket("127.0.0.1", 0)
    app = create_app(settings)
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started

    def stop():
        app.state.rescanner.wait(60)
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()

    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}", stop
    finally:
        if thread.is_alive():
            stop()


def scan(lib_paths, index_path):
    with LibraryIndex(index_path) as index:
        index.apply_scan(scan_library([Path(p) for p in lib_paths], EXTS, known_files=index.known_files()))
        return {i.relative_path: i.id for i in index.items()}


@pytest.fixture
def lib(tmp_path) -> Path:
    root = tmp_path / "Thư viện"
    shutil.copytree(LIBRARY, root)
    return root


@pytest.fixture
def hub(lib, _isolated_index, _isolated_cache, tmp_path):
    scan([lib], _isolated_index)
    with live_server([lib], _isolated_index, _isolated_cache, tmp_path) as (base, stop):
        yield base


# ======================================================================= browser


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch(channel="msedge", headless=True)
        except Exception:
            try:
                b = p.chromium.launch(headless=True)
            except Exception as exc:  # no Edge and no downloaded Chromium
                pytest.skip(f"no browser for Playwright: {exc}")
        yield b
        b.close()


@contextlib.contextmanager
def open_page(browser, viewport=DESKTOP, scheme="light"):
    ctx = browser.new_context(viewport=viewport, color_scheme=scheme)
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: m.type == "error" and "Failed to load resource" not in m.text
            and errors.append(m.text))
    try:
        yield page
        assert errors == [], errors  # no JavaScript error, whatever happened
    finally:
        ctx.close()


def loaded(page: Page) -> None:
    page.wait_for_selector("#grid[aria-busy=false]", state="attached")


def titles(page: Page) -> list[str]:
    return page.locator(".card:not(.skeleton) .title").all_text_contents()


def grid_columns(page: Page) -> int:
    return page.evaluate("getComputedStyle(document.querySelector('#grid')).gridTemplateColumns.split(' ').length")


def no_horizontal_scroll(page: Page) -> bool:
    return page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")


# ======================================================================= layout


@pytest.mark.parametrize(("viewport", "min_cols", "max_cols"), [(PHONE, 2, 2), (DESKTOP, 4, 8),
                                                                 ({"width": 768, "height": 1024}, 3, 5)])
def test_grid_adapts_to_screen(browser, hub, viewport, min_cols, max_cols):
    with open_page(browser, viewport) as page:
        page.goto(hub + "/?folder=Phim%20b%E1%BB%99%2FM%C3%B9a%201")
        loaded(page)
        assert titles(page) == ["Tập 01", "Tập 02 HEVC"]
        assert min_cols <= grid_columns(page) <= max_cols
        assert no_horizontal_scroll(page)
        # The search box stays usable (visible, wide enough) at every size.
        box = page.locator("#search").bounding_box()
        assert box and box["width"] >= 150 and box["x"] + box["width"] <= viewport["width"]


def test_dark_mode_and_card_contents(browser, hub):
    with open_page(browser, PHONE, scheme="dark") as page:
        page.goto(hub + "/?folder=Phim%20b%E1%BB%99%2FM%C3%B9a%201")
        loaded(page)
        bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
        assert bg != "rgb(246, 247, 249)"  # not the light background
        first = page.locator(".card").first
        expect(first.locator("img")).to_have_attribute("src", re.compile(r"^/thumbnails/[0-9a-f]{16}\.jpg$"))
        expect(first).to_have_attribute("href", re.compile(r"^/watch/[0-9a-f]{16}$"))  # player page (4.3)


# ======================================================================= navigation


def test_folder_navigation_and_back_button(browser, hub):
    with open_page(browser) as page:
        page.goto(hub + "/")
        loaded(page)
        expect(page.locator(".folder .name")).to_have_text(["Hỏng", "Phim bộ", "Phim lẻ", "UPPERCASE EXT"])
        expect(page.locator(".folder .sub").nth(1)).to_have_text("2 video")
        page.get_by_role("link", name=re.compile("Phim bộ")).click()
        loaded(page)
        page.get_by_role("link", name=re.compile("Mùa 1")).click()
        loaded(page)
        assert titles(page) == ["Tập 01", "Tập 02 HEVC"]
        expect(page.locator("#crumbs")).to_have_text(re.compile(r"Thư viện\s*/\s*Phim bộ\s*/\s*Mùa 1"))
        assert "folder=Phim+b%E1%BB%99%2FM%C3%B9a+1" in page.url
        # Breadcrumb back to the top, then the browser's Back button.
        page.locator("#crumbs a", has_text="Phim bộ").click()
        loaded(page)
        expect(page.locator(".folder .name")).to_have_text(["Mùa 1"])
        page.go_back()
        loaded(page)
        assert titles(page) == ["Tập 01", "Tập 02 HEVC"]
        # A deep link opens the same view directly (e.g. shared or bookmarked).
        page.goto(page.url)
        loaded(page)
        assert titles(page) == ["Tập 01", "Tập 02 HEVC"]


def test_search_filters_as_you_type(browser, hub):
    with open_page(browser, PHONE) as page:
        page.goto(hub + "/")
        loaded(page)
        page.locator("#search").press_sequentially("cuoc chi", delay=30)  # no marks typed
        expect(page.locator(".card .title")).to_have_text(["Cuộc Chiến Cuối Cùng"])
        expect(page.locator("#crumbs")).to_contain_text("Kết quả tìm “cuoc chi”")
        expect(page.locator(".folders")).to_be_hidden()
        assert "q=cuoc+chi" in page.url
        page.locator("#search").fill("không có phim này")
        expect(page.locator("#message")).to_contain_text("Không tìm thấy video nào")
        page.locator("#search").press("Escape")
        expect(page.locator(".folder")).to_have_count(4)
        expect(page.locator("#message")).to_be_hidden()


def test_sorting(browser, hub):
    with open_page(browser) as page:
        page.goto(hub + "/?q=phim")
        loaded(page)
        page.select_option("#sort", "title")
        loaded(page)
        expect(page.locator(".card .title").first).to_have_text("Cuộc Chiến Cuối Cùng")
        assert "sort=title" in page.url


def test_many_videos_load_while_scrolling(browser, tmp_path, _isolated_index, _isolated_cache):
    lib = tmp_path / "Nhiều"
    lib.mkdir()
    for n in range(1, 131):
        (lib / f"Tập {n}.mp4").write_bytes(b"video")
    scan([lib], _isolated_index)
    with live_server([lib], _isolated_index, _isolated_cache, tmp_path) as (base, _), \
            open_page(browser, PHONE) as page:
        page.goto(base + "/")
        loaded(page)
        expect(page.locator(".card")).to_have_count(60)
        expect(page.locator("#count")).to_have_text("130 video")
        for _ in range(10):
            page.mouse.wheel(0, 30000)
            page.wait_for_timeout(150)
        expect(page.locator(".card")).to_have_count(130)
        got = titles(page)
        assert got[:3] == ["Tập 1", "Tập 2", "Tập 3"] and got[-1] == "Tập 130"  # natural order
        assert len(set(got)) == 130  # no page loaded twice
        expect(page.locator("#more")).to_be_hidden()


# ======================================================================= states


def test_empty_library_explains_what_to_do(browser, tmp_path, _isolated_index, _isolated_cache):
    lib = tmp_path / "Trống"
    lib.mkdir()
    with live_server([lib], _isolated_index, _isolated_cache, tmp_path) as (base, _), \
            open_page(browser) as page:
        page.goto(base + "/")
        expect(page.locator("#message h2")).to_have_text("Thư viện trống")
        expect(page.locator("#message")).to_contain_text("Quét lại")
        # Add a video and use the message's own button: the page fills in.
        (lib / "Phim mới.mp4").write_bytes(b"video")
        page.locator("#message button").click()
        expect(page.locator("#toast")).to_contain_text("+1 mới")
        expect(page.locator(".card .title")).to_have_text(["Phim mới"])
        expect(page.locator("#message")).to_be_hidden()


def test_empty_folder_message(browser, hub):
    with open_page(browser) as page:
        page.goto(hub + "/?folder=Kh%C3%B4ng%20c%C3%B3")
        expect(page.locator("#message h2")).to_have_text("Thư mục trống")


def test_api_error_shows_message_and_retry_works(browser, hub):
    with open_page(browser) as page:
        page.route("**/api/folders", lambda route: route.fulfill(status=500, body="boom"))
        page.goto(hub + "/")
        expect(page.locator("#message h2")).to_have_text("Hub gặp lỗi khi tải dữ liệu")
        expect(page.locator("#message")).to_contain_text("mã 500")
        expect(page.locator(".card")).to_have_count(0)
        page.unroute("**/api/folders")
        page.get_by_role("button", name="Thử lại").click()
        expect(page.locator(".folder")).to_have_count(4)
        expect(page.locator("#message")).to_be_hidden()


def test_hub_offline_shows_message(browser, lib, _isolated_index, _isolated_cache, tmp_path):
    """Plan check: stop the server, then reload the data → 'Không kết nối được tới hub'."""
    scan([lib], _isolated_index)
    with live_server([lib], _isolated_index, _isolated_cache, tmp_path) as (base, stop), \
            open_page(browser) as page:
        page.goto(base + "/")
        loaded(page)
        expect(page.locator(".folder")).to_have_count(4)
        stop()  # the hub goes away while the page is open
        page.locator(".folder").first.click()  # any action that loads data
        expect(page.locator("#message h2")).to_have_text("Không kết nối được tới hub")
        expect(page.get_by_role("button", name="Thử lại")).to_be_visible()
        # The rescan button also explains instead of failing silently.
        page.locator("#rescan").click()
        expect(page.locator("#toast")).to_contain_text("Không kết nối được tới hub")


def _rescan_route(page: Page, statuses: list[dict], idle_first: bool = True) -> list[str]:
    """Answer /api/rescan with ``statuses`` in turn (POST starts them, each GET moves on)."""
    base = {"started": False, "phase": None, "progress_done": None, "progress_total": None,
            "started_at": None, "finished_at": None, "counts": None, "error": None}
    queue = [] if idle_first else list(statuses)
    calls: list[str] = []

    def handle(route):
        calls.append(route.request.method)
        if route.request.method == "POST":
            queue[:] = list(statuses)
        if queue:
            body = queue.pop(0)
        elif idle_first and "POST" not in calls:
            body = {"state": "idle"}  # nothing running before the button is pressed
        else:
            body = statuses[-1]
        route.fulfill(status=202 if route.request.method == "POST" else 200, json={**base, **body})

    page.route("**/api/rescan", handle)
    return calls


DONE = {"state": "done", "counts": {"found": 6, "added": 2, "removed": 0, "renamed": 0, "changed": 0,
                                    "unavailable_libraries": 0, "errors": 0, "info_read": 6, "info_failed": 0,
                                    "thumbnails_created": 6}}


@pytest.mark.parametrize("viewport", [PHONE, DESKTOP], ids=["phone", "desktop"])
def test_rescan_shows_a_progress_bar(browser, hub, viewport):
    """Step 1 (folders, total unknown): a moving bar and the videos found so far; step 2
    (details + thumbnails): a real percentage; then 100 % and the bar goes away."""
    with open_page(browser, viewport) as page:
        _rescan_route(page, [
            {"state": "running", "phase": "scanning", "progress_done": 3},
            {"state": "running", "phase": "scanning", "progress_done": 6},
            {"state": "running", "phase": "media", "progress_done": 0, "progress_total": None},
            {"state": "running", "phase": "media", "progress_done": 1, "progress_total": 4},
            DONE,
        ])
        page.goto(hub + "/")
        loaded(page)
        bar, label, count = page.locator("#scan-bar"), page.locator("#scan-label"), page.locator("#scan-count")
        expect(page.locator("#scan")).to_be_hidden()  # nothing running: no bar
        page.locator("#rescan").click()
        expect(page.locator("#scan")).to_be_visible()
        expect(page.locator("#scan")).to_have_class(re.compile(r"\bindeterminate\b"))
        expect(label).to_have_text("Bước 1/2: tìm video trong các thư mục…")
        expect(count).to_have_text("đã thấy 6 video")
        expect(page.locator("#rescan")).to_be_disabled()
        expect(label).to_have_text("Bước 2/2: đọc thông tin và tạo ảnh thu nhỏ")
        expect(count).to_have_text("1/4 · 25%")
        expect(page.locator("#scan")).not_to_have_class(re.compile(r"\bindeterminate\b"))
        expect(bar).to_have_attribute("aria-valuenow", "25")
        assert page.evaluate("document.querySelector('#scan .scan-fill').style.width") == "25%"
        assert no_horizontal_scroll(page)
        # Inside the sticky top bar, under the buttons and the search box.
        top = page.evaluate("[document.querySelector('.tools').getBoundingClientRect().bottom,"
                            " document.querySelector('#scan').getBoundingClientRect().top]")
        assert top[1] >= top[0]
        expect(label).to_have_text("Đã quét xong: 6 video")
        expect(bar).to_have_attribute("aria-valuenow", "100")
        expect(page.locator("#toast")).to_contain_text("+2 mới")
        expect(page.locator("#rescan")).to_be_enabled()
        expect(page.locator("#scan")).to_be_hidden(timeout=5000)


def test_rescan_started_on_another_device_shows_its_progress(browser, hub):
    with open_page(browser, PHONE) as page:
        calls = _rescan_route(page, [
            {"state": "running", "phase": "media", "progress_done": 3, "progress_total": 4},
            DONE,
        ], idle_first=False)
        page.goto(hub + "/")
        expect(page.locator("#scan-count")).to_have_text("3/4 · 75%")
        expect(page.locator("#rescan")).to_be_disabled()  # no second rescan meanwhile
        expect(page.locator("#scan-label")).to_have_text("Đã quét xong: 6 video")
        expect(page.locator("#scan")).to_be_hidden(timeout=5000)
        assert "POST" not in calls  # only watched, not started again


def test_failed_rescan_hides_the_bar(browser, hub):
    with open_page(browser) as page:
        _rescan_route(page, [{"state": "running", "phase": "scanning", "progress_done": 1},
                             {"state": "failed", "error": "OSError (details in the server log)"}])
        page.goto(hub + "/")
        loaded(page)
        page.locator("#rescan").click()
        expect(page.locator("#scan")).to_be_visible()
        expect(page.locator("#toast")).to_contain_text("không thành công")
        expect(page.locator("#scan")).to_be_hidden()
        expect(page.locator("#rescan")).to_be_enabled()


def test_disconnected_library_is_shown_greyed(browser, lib, _isolated_index, _isolated_cache, tmp_path):
    scan([lib], _isolated_index)
    lib.rename(lib.with_name("rút ổ"))
    scan([lib], _isolated_index)  # the folder is now "not connected"
    with live_server([lib], _isolated_index, _isolated_cache, tmp_path) as (base, _), \
            open_page(browser) as page:
        page.goto(base + "/?folder=Phim%20b%E1%BB%99%2FM%C3%B9a%201")
        loaded(page)
        expect(page.locator(".card.off")).to_have_count(2)
        expect(page.locator(".card .badge.off").first).to_have_text("Ổ đang ngắt")
        assert page.locator("a.card").count() == 0  # not clickable
        expect(page.locator("#toast")).to_contain_text("không kết nối")


def test_file_names_are_shown_as_text_not_html(browser, tmp_path, _isolated_index, _isolated_cache):
    lib = tmp_path / "Lạ"
    lib.mkdir()
    (lib / "Tom &amp; Jerry {x} [1].mp4").write_bytes(b"video")
    scan([lib], _isolated_index)
    with live_server([lib], _isolated_index, _isolated_cache, tmp_path) as (base, _), \
            open_page(browser) as page:
        page.goto(base + "/")
        expect(page.locator(".card .title")).to_have_text(["Tom &amp; Jerry {x} [1]"])


def test_two_libraries(browser, lib, tmp_path, _isolated_index, _isolated_cache):
    other = tmp_path / "Phim khác"
    other.mkdir()
    (other / "Một phim.mp4").write_bytes(b"video")
    scan([lib, other], _isolated_index)
    with live_server([lib, other], _isolated_index, _isolated_cache, tmp_path) as (base, _), \
            open_page(browser) as page:
        page.goto(base + "/")
        loaded(page)
        expect(page.locator(".folder .name")).to_have_text(["Phim khác", "Thư viện"])
        expect(page.locator(".folder .sub")).to_have_text(["1 video", "6 video"])
        page.get_by_role("link", name=re.compile("Phim khác")).click()
        expect(page.locator(".card .title")).to_have_text(["Một phim"])
        expect(page.locator("#crumbs")).to_have_text(re.compile(r"Tất cả thư viện\s*/\s*Phim khác"))


def test_page_and_assets_are_served(hub):
    import httpx

    r = httpx.get(hub + "/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert r.headers["cache-control"] == "no-cache"
    for asset in ("/static/app.js", "/static/app.css"):
        a = httpx.get(hub + asset)
        assert a.status_code == 200 and a.headers["cache-control"] == "no-cache"
    # Works offline: no address of another host (the SVG namespace is only a name; the
    # localhost address is text telling the user where the admin page is).
    external = re.compile(r"https?://(?!localhost[:/]|www\.w3\.org/2000/svg)")
    for path in ("/", "/static/app.js", "/static/app.css", "/static/admin.js"):
        assert not external.search(httpx.get(hub + path).text), path
    assert httpx.get(hub + "/static/../pyproject.toml").status_code == 404
