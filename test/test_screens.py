"""Tests for plan step 5.4: the smart TV as a screen (/tv), the phone as its remote (/remote).

Layers:
- ScreenRegistry on its own (ids, keys, codes, streams, limits) — no server.
- The HTTP API with a TestClient (errors, keys, PIN/LAN rules).
- The event stream and Ctrl+C on a real uvicorn (HubServer).
- In Edge: one browser context as the TV, another as the phone, a real film from phone to TV.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
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
from source.modules.server import HubServer, bind_socket, create_app
from source.modules.server.screens import (
    CODE_ALPHABET,
    CommandQueueFull,
    QUEUE_SIZE,
    ScreenNotFound,
    ScreenOffline,
    ScreenRegistry,
    TooManyScreens,
    WrongKey,
)
from source.schemas.api import ScreenState
from source.utils.config import load_settings
from source.utils.ffmpeg import FFTools

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = PROJECT_ROOT / "test" / "fixtures" / "library"
TOOLS = FFTools(ffmpeg=shutil.which("ffmpeg"), ffprobe=shutil.which("ffprobe"))
TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

MP4 = "Phim/Phim có phụ đề.mp4"
MKV = "Phim/Phim MKV.mkv"
SRT_VI = "1\n00:00:00,000 --> 00:01:00,000\nPhụ đề tiếng Việt\n"
SRT_EN = "1\n00:00:00,000 --> 00:01:00,000\nEnglish subtitle\n"


def settings(tmp_path, library=FIXTURES, index=None, **env):
    environ = {"HUB_INDEX_PATH": str(index or tmp_path / "index" / "library.db"),
               "HUB_CACHE_DIR": str(tmp_path / "cache"), **env}
    return load_settings(environ=environ, env_file=tmp_path / "none.env", cli_paths=[str(library)])


# ======================================================================= the registry alone


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_new_screen_gets_an_id_a_secret_key_and_a_short_code():
    reg = ScreenRegistry()
    s = reg.register(None, None, "Sony TV", "192.168.1.50")
    assert TOKEN.match(s.id) and TOKEN.match(s.key) and s.id != s.key
    assert len(s.code) == 4 and set(s.code) <= set(CODE_ALPHABET)
    assert not set(s.code) & set("0O1I2Z5S6G8B")
    assert reg.info(s).online is False and reg.info(s).state.status == "idle"
    assert "key" not in reg.info(s).model_dump()


def test_same_tv_comes_back_as_the_same_screen():
    reg = ScreenRegistry()
    s = reg.register(None, None, "TV", "192.168.1.50")
    again = reg.register(s.id, s.key, "Sony TV", "192.168.1.50")  # page reloaded
    assert again is s and again.name == "Sony TV" and len(reg.screens()) == 1


def test_after_a_hub_restart_the_tv_keeps_its_id():
    old = ScreenRegistry().register(None, None, "TV", "192.168.1.50")
    reg = ScreenRegistry()  # a new hub process
    s = reg.register(old.id, old.key, "TV", "192.168.1.50")
    assert (s.id, s.key) == (old.id, old.key)  # phones that remember the id still find it


def test_someone_else_cannot_take_over_a_screen_with_its_id(caplog):
    reg = ScreenRegistry()
    s = reg.register(None, None, "TV", "192.168.1.50")
    other = reg.register(s.id, "x" * 32, "Fake", "192.168.1.66")
    assert other is not s and other.id != s.id and reg.get(s.id).name == "TV"
    assert "wrong key" in caplog.text
    with pytest.raises(WrongKey):
        reg.authorize(s.id, "x" * 32, "192.168.1.66")
    with pytest.raises(WrongKey):
        reg.authorize(s.id, None, "192.168.1.66")
    with pytest.raises(ScreenNotFound):
        reg.get("y" * 20)


def test_codes_are_unique():
    reg = ScreenRegistry(max_screens=20)
    codes = [reg.register(None, None, "TV", "x").code for _ in range(20)]
    assert len(set(codes)) == 20


def test_too_many_screens_and_old_ones_forgotten():
    clock = Clock()
    reg = ScreenRegistry(max_screens=2, forget_after=3600, clock=clock)
    a = reg.register(None, None, "A", "x")
    reg.register(None, None, "B", "x")
    with pytest.raises(TooManyScreens) as err:
        reg.register(None, None, "C", "x")
    assert err.value.limit == 2
    clock.now += 3601  # A and B offline for more than an hour: forgotten
    reg.register(None, None, "C", "x")
    with pytest.raises(ScreenNotFound):
        reg.get(a.id)


def test_a_connected_screen_is_never_forgotten():
    async def go():
        clock = Clock()
        reg = ScreenRegistry(forget_after=10, clock=clock)
        s = reg.register(None, None, "TV", "x")
        reg.open_stream(s.id, s.key, "x")
        clock.now += 100
        assert reg.screens() == [s]
    asyncio.run(go())


def test_commands_reach_the_stream_in_order():
    async def go():
        reg = ScreenRegistry()
        s = reg.register(None, None, "TV", "x")
        with pytest.raises(ScreenOffline):
            reg.send(s.id, {"action": "play"}, "phone")
        screen, stream = reg.open_stream(s.id, s.key, "tv")
        assert reg.info(screen).online
        events = reg.events(screen, stream)
        assert await events.__anext__() == "retry: 3000\n\n"
        hello = await events.__anext__()
        assert hello.startswith("event: hello\n") and s.code in hello
        reg.send(s.id, {"action": "pause"}, "phone")
        reg.send(s.id, {"action": "seek", "to": 30.0}, "phone")
        assert await events.__anext__() == 'event: command\ndata: {"action":"pause"}\n\n'
        assert json.loads((await events.__anext__()).split("data: ")[1]) == {"action": "seek", "to": 30.0}
        await events.aclose()  # the TV hung up
        assert not reg.info(screen).online
        with pytest.raises(ScreenOffline):
            reg.send(s.id, {"action": "play"}, "phone")
    asyncio.run(go())


def test_full_queue_is_refused():
    async def go():
        reg = ScreenRegistry()
        s = reg.register(None, None, "TV", "x")
        reg.open_stream(s.id, s.key, "tv")
        for _ in range(QUEUE_SIZE):
            reg.send(s.id, {"action": "play"}, "phone")
        with pytest.raises(CommandQueueFull):
            reg.send(s.id, {"action": "play"}, "phone")
    asyncio.run(go())


def test_a_new_stream_ends_the_old_one():
    async def go():
        reg = ScreenRegistry()
        s = reg.register(None, None, "TV", "x")
        screen, first = reg.open_stream(s.id, s.key, "tv")
        old = reg.events(screen, first)
        await old.__anext__(), await old.__anext__()
        _, second = reg.open_stream(s.id, s.key, "tv")  # the TV page reloaded
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(old.__anext__(), timeout=2)
        assert screen.stream is second and reg.info(screen).online  # the old one did not log it out
        reg.send(s.id, {"action": "play"}, "phone")
        assert second.queue.qsize() == 1
    asyncio.run(go())


def test_close_ends_every_stream_at_once():
    async def go():
        reg = ScreenRegistry()
        gens = []
        for _ in range(3):
            s = reg.register(None, None, "TV", "x")
            screen, stream = reg.open_stream(s.id, s.key, "tv")
            g = reg.events(screen, stream)
            await g.__anext__(), await g.__anext__()
            gens.append(g)
        reg.close()
        for g in gens:
            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(g.__anext__(), timeout=1)
        assert not any(s.online for s in reg.screens())
    asyncio.run(go())


def test_pings_keep_the_stream_alive():
    async def go():
        reg = ScreenRegistry(ping_seconds=0.6)
        s = reg.register(None, None, "TV", "x")
        screen, stream = reg.open_stream(s.id, s.key, "tv")
        g = reg.events(screen, stream)
        await g.__anext__(), await g.__anext__()
        assert await asyncio.wait_for(g.__anext__(), timeout=3) == ": ping\n\n"
        await g.aclose()
    asyncio.run(go())


def test_state_needs_the_key():
    reg = ScreenRegistry()
    s = reg.register(None, None, "TV", "x")
    with pytest.raises(WrongKey):
        reg.set_state(s.id, "z" * 20, ScreenState(status="playing"), "phone")
    reg.set_state(s.id, s.key, ScreenState(status="playing", position=12.5), "tv")
    info = reg.info(s)
    assert info.state.status == "playing" and info.state.position == 12.5 and info.state_age is not None


# ======================================================================= the API


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(settings(tmp_path)), client=("192.168.1.50", 4000)) as c:
        yield c


def register(client, **body) -> dict:
    r = client.post("/api/screens", json={"name": "Sony TV", **body})
    assert r.status_code == 201, r.text
    return r.json()


def test_register_gives_the_remote_link_and_qr(client):
    s = register(client)
    assert TOKEN.match(s["id"]) and TOKEN.match(s["key"]) and len(s["code"]) == 4
    assert s["remote_url"] == f"http://testserver/remote?screen={s['id']}"
    assert s["qr_svg"].startswith("data:image/svg+xml")
    assert s["online"] is False and s["state"]["status"] == "idle"
    again = register(client, id=s["id"], key=s["key"])
    assert (again["id"], again["code"]) == (s["id"], s["code"])


def test_on_localhost_the_qr_uses_the_lan_address(tmp_path, monkeypatch):
    from source.modules.server import network

    class Found:
        def urls(self, port):
            return [f"http://192.168.1.2:{port}/"]

    monkeypatch.setattr(network, "find_addresses", lambda: Found())
    with TestClient(create_app(settings(tmp_path)), base_url="http://localhost:8000") as c:
        s = c.post("/api/screens", json={"name": "TV"}).json()
    assert s["remote_url"] == f"http://192.168.1.2:8000/remote?screen={s['id']}"


def test_phones_never_see_the_key(client):
    s = register(client)
    listed = client.get("/api/screens").json()["screens"]
    assert [x["id"] for x in listed] == [s["id"]] and "key" not in listed[0]
    one = client.get(f"/api/screens/{s['id']}").json()
    assert "key" not in one and one["code"] == s["code"]
    assert s["key"] not in client.get("/api/screens").text


def test_state_round_trip_and_wrong_key(client):
    s = register(client)
    url = f"/api/screens/{s['id']}/state"
    state = {"status": "playing", "media_id": "abc", "title": "Phim", "position": 42.5, "duration": 60,
             "volume": 0.5, "muted": False, "subtitles": [{"key": "vi", "label": "Tiếng Việt"}], "subtitle": "vi",
             "notice": None, "sound_blocked": True, "started": True,
             "playlist": {"title": "Mùa 1", "position": 2, "length": 5, "shuffle": False, "repeat": "all",
                          "next_title": "Tập 3", "up_next_in": None},
             "problem": None}
    assert client.put(url, json=state, params={"key": "w" * 32}).status_code == 403
    assert client.put(url, json=state).status_code == 403  # no key at all
    assert client.put(url, json=state, params={"key": s["key"]}).status_code == 204
    got = client.get(f"/api/screens/{s['id']}").json()["state"]
    assert got == state
    bad = {**state, "position": -1}
    assert client.put(url, json=bad, params={"key": s["key"]}).status_code == 422
    assert client.put(f"/api/screens/{'q' * 20}/state", json=state, params={"key": s["key"]}).status_code == 404


def test_events_need_the_key(tmp_path):
    # On a real server with timeouts: if the check were missing, the answer would be an endless
    # stream, which would hang a TestClient instead of failing.
    with live_hub(create_app(settings(tmp_path))) as (base, _):
        with httpx.Client(base_url=base, timeout=5) as http:
            s = http.post("/api/screens", json={"name": "TV"}).json()
            for sid, params, expected in ((s["id"], {"key": "w" * 32}, 403), (s["id"], {}, 403),
                                          ("q" * 20, {"key": s["key"]}, 404)):
                with http.stream("GET", f"/api/screens/{sid}/events", params=params) as r:
                    assert r.status_code == expected
            assert http.get(f"/api/screens/{s['id']}").json()["online"] is False


@pytest.mark.parametrize("command", [
    {"action": "dance"}, {"action": "seek", "to": -5}, {"action": "seek"}, {"action": "volume", "level": 2},
    {"action": "play", "extra": 1}, {"action": "play_media"}, {"action": "play_media", "media_id": ""},
    {"action": "seek_by", "by": "x"}, {"action": "subtitle", "key": "k" * 101}, {"action": "mute"},
])
def test_invalid_commands_refused(client, command):
    s = register(client)
    assert client.post(f"/api/screens/{s['id']}/commands", json=command).status_code == 422


def test_command_errors(client):
    s = register(client)
    url = f"/api/screens/{s['id']}/commands"
    r = client.post(url, json={"action": "play"})
    assert r.status_code == 409 and "mất kết nối" in r.json()["detail"]  # the TV page is not connected
    assert client.post(f"/api/screens/{'q' * 20}/commands", json={"action": "play"}).status_code == 404
    for media_id in ("0123456789abcdef", "../../windows/win.ini"):
        r = client.post(url, json={"action": "play_media", "media_id": media_id})
        assert r.status_code == 404 and r.json() == {"detail": "Not Found"}


def test_too_many_screens_answer(client):
    client.app.state.screens.max_screens = 1
    register(client)
    r = client.post("/api/screens", json={"name": "TV 2"})
    assert r.status_code == 503 and "tối đa 1" in r.json()["detail"] and r.headers["retry-after"] == "60"


def test_invalid_registration(client):
    for body in ({"id": "short", "key": "k" * 20}, {"name": ""}, {"name": "x" * 61}, {"id": "a/b" * 8}):
        assert client.post("/api/screens", json=body).status_code == 422


def test_pages_and_api_follow_the_pin_and_lan_rules(tmp_path):
    app = create_app(settings(tmp_path, HUB_PIN="2468"))
    with TestClient(app, client=("192.168.1.50", 4000)) as c:
        for page in ("/tv", "/remote"):
            r = c.get(page, follow_redirects=False)
            assert r.status_code == 303 and r.headers["location"] == f"/pin?next={page.replace('/', '%2F')}"
        assert c.post("/api/screens", json={"name": "TV"}).status_code == 401
        assert c.get("/api/screens").status_code == 401
        assert c.post("/api/pin", json={"pin": "2468"}).status_code == 200  # cookie set
        assert c.get("/tv").status_code == 200 and c.post("/api/screens", json={"name": "TV"}).status_code == 201
    with TestClient(app, client=("8.8.8.8", 4000)) as c:
        assert c.get("/tv").status_code == 403 and c.get("/api/screens").status_code == 403


def test_pages_are_served(client):
    for page, marker in (("/tv", "tv.js"), ("/remote", "remote.js")):
        r = client.get(page)
        assert r.status_code == 200 and marker in r.text and r.headers["cache-control"] == "no-cache"


# ======================================================================= the stream on a real server


@contextlib.contextmanager
def live_hub(app):
    sock = bind_socket("127.0.0.1", 0)
    server = HubServer(uvicorn.Config(app, log_config=None, lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    state = {"server": server, "thread": thread}
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}", state
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        sock.close()


def read_events(response, out: list, stop: threading.Event):
    try:
        for line in response.iter_lines():
            out.append(line)
            if stop.is_set():
                break
    except httpx.HTTPError:
        pass


def test_commands_flow_over_the_stream_and_ctrl_c_does_not_hang(tmp_path):
    app = create_app(settings(tmp_path))
    with live_hub(app) as (base, hub):
        with httpx.Client(base_url=base, timeout=10) as http:
            s = http.post("/api/screens", json={"name": "TV"}).json()
            lines: list[str] = []
            stop = threading.Event()
            with http.stream("GET", f"/api/screens/{s['id']}/events", params={"key": s["key"]},
                             timeout=httpx.Timeout(10, read=30)) as resp:
                assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/event-stream")
                reader = threading.Thread(target=read_events, args=(resp, lines, stop), daemon=True)
                reader.start()
                deadline = time.monotonic() + 5
                while "event: hello" not in lines and time.monotonic() < deadline:
                    time.sleep(0.05)
                assert http.get(f"/api/screens/{s['id']}").json()["online"] is True
                r = http.post(f"/api/screens/{s['id']}/commands", json={"action": "seek_by", "by": -10})
                assert r.status_code == 202 and r.json() == {"sent": "seek_by"}
                deadline = time.monotonic() + 5
                while not any(x.startswith("data: {\"action\":\"seek_by\"") for x in lines) and time.monotonic() < deadline:
                    time.sleep(0.05)
                assert 'data: {"action":"seek_by","by":-10.0}' in lines

                # Ctrl+C while the TV still holds its stream: the hub stops at once.
                started = time.monotonic()
                hub["server"].should_exit = True
                hub["thread"].join(timeout=10)
                assert not hub["thread"].is_alive() and time.monotonic() - started < 5
                stop.set()
                reader.join(timeout=5)


# ======================================================================= in the browser: phone → TV


needs_ffmpeg = pytest.mark.skipif(TOOLS.missing != [], reason="ffmpeg/ffprobe needed")


@pytest.fixture(scope="module")
def film_library(tmp_path_factory):
    if TOOLS.missing:
        pytest.skip("ffmpeg/ffprobe needed")
    root = tmp_path_factory.mktemp("tv") / "Thư viện"
    (root / "Phim").mkdir(parents=True)
    src = ["-f", "lavfi", "-i", "testsrc2=duration=60:size=320x180:rate=25",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=60"]
    jobs = {MP4: ["-c:v", "libx264", "-preset", "ultrafast", "-g", "25", "-pix_fmt", "yuv420p", "-c:a", "aac",
                  "-movflags", "+faststart"],
            MKV: ["-c:v", "libx264", "-preset", "ultrafast", "-g", "50", "-pix_fmt", "yuv420p", "-c:a", "aac"]}
    for rel, codec in jobs.items():
        subprocess.run([TOOLS.ffmpeg, "-v", "error", "-y", *src, *codec, "-shortest", str(root / rel)],
                       check=True, timeout=120)
    (root / "Phim" / "Phim có phụ đề.vi.srt").write_text(SRT_VI, encoding="utf-8")
    (root / "Phim" / "Phim có phụ đề.en.srt").write_text(SRT_EN, encoding="utf-8")
    index = tmp_path_factory.mktemp("tvidx") / "library.db"
    with LibraryIndex(index) as idx:
        idx.apply_scan(scan_library([root], (".mp4", ".mkv"), known_files=idx.known_files()))
        process_media(idx, TOOLS, index.parent / "thumbs", workers=2)
        ids = {i.relative_path: i.id for i in idx.items()}
    return root, index, ids


@pytest.fixture(scope="module")
def edge():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            # The default autoplay policy on purpose: the TV page must get its sound by the
            # "Bắt đầu" click, like on a real TV.
            b = p.chromium.launch(channel="msedge", headless=True)
        except Exception:
            pytest.skip("Microsoft Edge is needed (H.264)")
        yield b
        b.close()


# Pretend the TV browser lacks MKV (like many TV browsers): the hub must convert it (step 5.1).
NO_MKV = """(() => {
  const no = (t) => /matroska/.test(String(t));
  const cpt = HTMLMediaElement.prototype.canPlayType;
  HTMLMediaElement.prototype.canPlayType = function (t) { return no(t) ? "" : cpt.call(this, t); };
  const its = MediaSource.isTypeSupported.bind(MediaSource);
  MediaSource.isTypeSupported = (t) => !no(t) && its(t);
})();"""


def player(tv):
    """The player frame inside the TV page."""
    for f in tv.frames:
        if "/watch/" in f.url:
            return f
    return None


def tv_video(tv, expr: str):
    f = player(tv)
    return f.evaluate(f"(() => {{ const v = document.querySelector('#video'); return {expr}; }})()") if f else None


def wait_tv(tv, expr: str, timeout=30.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = tv_video(tv, expr)
        except Exception as exc:  # the frame is (re)loading
            last = exc
        if last is True:
            return
        tv.wait_for_timeout(100)  # not time.sleep: Playwright must keep handling the page's events (new frames)
    raise AssertionError(f"TV never got {expr!r} (last: {last!r})")


@needs_ffmpeg
def test_phone_plays_a_film_on_the_tv_and_controls_it(edge, film_library, tmp_path):
    root, index, ids = film_library
    app = create_app(settings(tmp_path, library=root, index=index))
    errors: list[str] = []
    with live_hub(app) as (base, _):
        tv_ctx = edge.new_context(viewport={"width": 1280, "height": 720})
        phone_ctx = edge.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        tv_ctx.add_init_script(NO_MKV)
        tv, phone = tv_ctx.new_page(), phone_ctx.new_page()
        for page in (tv, phone):
            page.on("pageerror", lambda e: errors.append(str(e)))
        plans = []
        tv.on("response", lambda r: "/api/play/" in r.url and r.request.method == "POST" and plans.append(r))
        try:
            # --- the TV waits, with its QR code and short code
            tv.goto(f"{base}/tv")
            tv.wait_for_selector("body[data-connected]")
            code = tv.get_attribute("body", "data-code")
            assert re.fullmatch(f"[{CODE_ALPHABET}]{{4}}", code)
            assert tv.get_attribute("#qr", "src").startswith("data:image/svg+xml")
            assert "/remote?screen=" in tv.text_content("#url")
            tv.click("#start")  # the one click with the TV's cursor

            # --- the phone finds the TV by its code
            phone.goto(f"{base}/remote")
            phone.fill("#code", code.lower())
            phone.click("#code-form button")
            phone.wait_for_selector("body[data-status='idle']")
            assert code in phone.text_content("#screen-name")

            # --- "Phát trên TV" from the phone's player, at 0:20
            phone.goto(f"{base}/watch/{ids[MP4]}?t=20")
            phone.wait_for_function("document.querySelector('#video').currentTime >= 20", timeout=30000)
            phone.click("#cast")
            phone.wait_for_url(re.compile(r"/remote$"))
            wait_tv(tv, "v.currentTime >= 20.5 && !v.paused && v.videoWidth > 0")
            assert tv_video(tv, "v.currentTime") < 40  # continued from 0:20, not from the start
            assert tv_video(tv, "v.muted") is False  # sound allowed by the "Bắt đầu" click
            phone.wait_for_selector("body[data-status='playing']")
            assert "Phim có phụ đề" in phone.text_content("#now-title")
            assert phone.get_attribute("#thumb", "src") == f"/thumbnails/{ids[MP4]}.jpg"
            assert phone.locator("#message").is_hidden()  # no warning: started, connected, sound on

            # --- pause, play
            phone.click("#playpause")
            wait_tv(tv, "v.paused")
            phone.wait_for_selector("body[data-status='paused']")
            phone.click("#playpause")
            wait_tv(tv, "!v.paused")

            # --- seek bar, then +10 s
            phone.eval_on_selector("#seek", "(s) => { s.value = '40'; s.dispatchEvent(new Event('change')); }")
            wait_tv(tv, "v.currentTime >= 40 && v.currentTime < 46")
            phone.click("#fwd10")
            wait_tv(tv, "v.currentTime >= 50")
            phone.click("#back10")
            wait_tv(tv, "v.currentTime < 50")

            # --- subtitles from the phone's list
            phone.wait_for_selector("#sub-box:not([hidden])")
            labels = phone.eval_on_selector_all("#subtitles option", "(os) => os.map((o) => o.value)")
            assert "off" in labels and len(labels) == 3
            vi = phone.eval_on_selector_all("#subtitles option", "(os) => os.find((o) => /Việt/.test(o.textContent)).value")
            phone.select_option("#subtitles", vi)
            wait_tv(tv, f"[...v.textTracks].some((t) => t.mode === 'showing' && t.language === 'vi')")
            phone.select_option("#subtitles", "off")
            wait_tv(tv, "[...v.textTracks].every((t) => t.mode !== 'showing')")

            # --- volume, mute
            phone.eval_on_selector("#volume", "(s) => { s.value = '50'; s.dispatchEvent(new Event('input')); }")
            wait_tv(tv, "Math.abs(v.volume - 0.5) < 0.01")
            phone.click("#mute")
            wait_tv(tv, "v.muted")
            phone.wait_for_function("document.querySelector('#mute').textContent === 'Bật tiếng'")
            phone.click("#mute")
            wait_tv(tv, "!v.muted")

            # --- another film the TV browser cannot play as it is: converted by the hub (5.1)
            r = httpx.post(f"{base}/api/screens/{phone.evaluate('localStorage.getItem(\"hub:remote-screen\")').strip(chr(34))}/commands",
                           json={"action": "play_media", "media_id": ids[MKV]})
            assert r.status_code == 202
            tv.wait_for_function(f"document.querySelector('#frame').src.includes('{ids[MKV]}')")
            deadline = time.monotonic() + 10
            while (player(tv) is None or ids[MKV] not in player(tv).url) and time.monotonic() < deadline:
                tv.wait_for_timeout(100)
            wait_tv(tv, "v.currentTime > 1 && !v.paused && v.videoWidth > 0", timeout=60)
            # The MP4 played as it is; the MKV, which this TV browser cannot play, was remuxed (5.1).
            assert [p.json()["mode"] for p in plans] == ["remux"]
            assert ids[MKV] in plans[0].url
            phone.wait_for_function("document.querySelector('#now-title').textContent.includes('Phim MKV')")

            # --- stop: the TV is back on its waiting screen, its player is gone
            phone.click("#stop")
            tv.wait_for_selector("#idle:not([hidden])")
            assert tv.is_hidden("#frame") and player(tv) is None
            phone.wait_for_selector("body[data-status='idle']")

            # --- the TV page closes: the phone says so, commands get a clear answer
            tv.close()
            phone.wait_for_selector("body[data-status='offline']", timeout=10000)
            assert "mất kết nối" in phone.text_content("#message")
        finally:
            tv_ctx.close()
            phone_ctx.close()
    assert errors == [], errors


@needs_ffmpeg
def test_tv_reconnects_and_phone_keeps_its_choice(edge, film_library, tmp_path):
    """The hub restarts (new process, nothing in memory): the TV registers again with the same
    id, so the phone's remembered TV still works without picking it again."""
    root, index, ids = film_library
    tv_ctx = edge.new_context()
    errors: list[str] = []
    try:
        app = create_app(settings(tmp_path, library=root, index=index))
        sock = bind_socket("127.0.0.1", 0)
        port = sock.getsockname()[1]
        sock.close()

        def serve(application):
            s = bind_socket("127.0.0.1", port)
            server = HubServer(uvicorn.Config(application, log_config=None, lifespan="on"))
            t = threading.Thread(target=server.run, kwargs={"sockets": [s]}, daemon=True)
            t.start()
            while not server.started:
                time.sleep(0.05)
            return server, t, s

        base = f"http://127.0.0.1:{port}"
        server, thread, s = serve(app)
        tv = tv_ctx.new_page()
        tv.on("pageerror", lambda e: errors.append(str(e)))
        tv.goto(f"{base}/tv")
        tv.wait_for_selector("body[data-connected]")
        first = httpx.get(f"{base}/api/screens").json()["screens"][0]
        server.should_exit = True
        thread.join(timeout=10)
        s.close()
        tv.wait_for_function("!document.body.dataset.connected", timeout=10000)
        assert "Mất kết nối" in tv.text_content("#status")

        app2 = create_app(settings(tmp_path, library=root, index=index))  # empty registry
        server, thread, s = serve(app2)
        try:
            tv.wait_for_selector("body[data-connected]", timeout=20000)
            again = httpx.get(f"{base}/api/screens").json()["screens"]
            assert [x["id"] for x in again] == [first["id"]] and again[0]["online"] is True
            r = httpx.post(f"{base}/api/screens/{first['id']}/commands",
                           json={"action": "play_media", "media_id": ids[MP4], "at": 5})
            assert r.status_code == 202
            wait_tv(tv, "v.currentTime >= 5 && v.readyState >= 2")
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            s.close()
    finally:
        tv_ctx.close()
    assert errors == [], errors


# Headless Edge lets a page play with sound without any click, unlike a real TV browser. This
# imitates Chrome's autoplay rule in each frame: play() with sound is refused until the page was
# clicked, and unmuting a playing video without a click pauses it.
SOUND_NEEDS_A_CLICK = """(() => {
  let clicked = false;
  addEventListener("click", () => { clicked = true; }, true);
  const play = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function () {
    if (!this.muted && !clicked) return Promise.reject(new DOMException("needs a click", "NotAllowedError"));
    return play.call(this);
  };
  const muted = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, "muted");
  Object.defineProperty(HTMLMediaElement.prototype, "muted", {
    configurable: true,
    get() { return muted.get.call(this); },
    set(v) { muted.set.call(this, v); if (!v && !clicked && !this.paused) this.pause(); },
  });
})();"""


@needs_ffmpeg
def test_phone_without_a_tv_is_asked_to_pick_one_then_the_film_starts(edge, film_library, tmp_path):
    root, index, ids = film_library
    app = create_app(settings(tmp_path, library=root, index=index))
    with live_hub(app) as (base, _):
        tv_ctx, phone_ctx = edge.new_context(), edge.new_context(is_mobile=True, has_touch=True)
        tv_ctx.add_init_script(SOUND_NEEDS_A_CLICK)
        try:
            tv = tv_ctx.new_page()
            tv.goto(f"{base}/tv")
            tv.wait_for_selector("body[data-connected]")
            code = tv.get_attribute("body", "data-code")
            phone = phone_ctx.new_page()
            phone.goto(f"{base}/remote?play={ids[MP4]}&t=12")
            phone.wait_for_selector("#picker:not([hidden])")
            assert "Chọn TV để phát phim" in phone.text_content("#message")
            assert phone.url.endswith("/remote")  # a reload will not play it twice
            phone.click(f"#screens button[data-code='{code}']")
            wait_tv(tv, "v.currentTime >= 12 && v.readyState >= 2")
            # Nobody clicked "Bắt đầu" on this TV: the phone tells what to do.
            phone.wait_for_function("document.querySelector('#message').textContent.includes('Bắt đầu')")
            # Without that click the browser refuses sound: the film plays muted, the phone says why,
            # and one click on the TV screen brings the sound back.
            wait_tv(tv, "!v.paused && v.muted")
            phone.wait_for_function("document.querySelector('#message').textContent.includes('Bấm vào màn hình TV')")
            assert tv.text_content("#badge") == "Bấm vào màn hình TV một lần để bật tiếng."
            phone.click("#mute")  # unmuting from the phone cannot work without that click: still playing, muted
            phone.wait_for_timeout(1500)
            assert tv_video(tv, "!v.paused && v.muted") is True
            tv.mouse.click(640, 360)
            wait_tv(tv, "!v.paused && !v.muted")
            phone.wait_for_function("!document.querySelector('#message').textContent.includes('Bấm vào màn hình TV')")
        finally:
            tv_ctx.close()
            phone_ctx.close()
