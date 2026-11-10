"""Tests for plan step 5.2: LAN-only access and the optional PIN."""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from source.modules.server import bind_socket, create_app
from source.modules.server.access import PinGuard, is_lan_address
from source.utils.config import ConfigError, describe, load_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LIBRARY = PROJECT_ROOT / "test" / "fixtures" / "library"
PHONE = ("192.168.1.50", 40000)
OTHER_PHONE = ("192.168.1.51", 40000)
PIN = "2468"


def settings(tmp_path, **env):
    return load_settings(environ={"HUB_INDEX_PATH": os.environ["HUB_INDEX_PATH"],
                                  "HUB_CACHE_DIR": os.environ["HUB_CACHE_DIR"], **env},
                         env_file=tmp_path / "none.env", cli_paths=[str(LIBRARY)])


def client(tmp_path, who=PHONE, **env) -> TestClient:
    return TestClient(create_app(settings(tmp_path, **env)), client=who, follow_redirects=False)


# ======================================================================= LAN only


@pytest.mark.parametrize("ip,lan", [
    ("192.168.1.5", True), ("10.1.2.3", True), ("172.16.5.5", True), ("172.31.255.1", True), ("127.0.0.1", True),
    ("169.254.10.1", True), ("100.64.1.1", True), ("::1", True), ("fe80::1%12", True), ("fd12:3456::1", True),
    ("::ffff:192.168.1.9", True),
    ("8.8.8.8", False), ("172.32.0.1", False), ("1.1.1.1", False), ("2001:4860:4860::8888", False),
    ("::ffff:8.8.8.8", False), ("100.128.0.1", False), ("203.0.113.9", False), ("198.18.0.1", False),
    ("testclient", None),
])
def test_lan_addresses(ip, lan):
    assert is_lan_address(ip) is lan


@pytest.mark.parametrize("url", ["/", "/api/library", "/stream/0123456789abcdef", "/admin", "/pin", "/static/app.css"])
def test_public_address_is_refused_everywhere(tmp_path, url):
    c = client(tmp_path, who=("8.8.8.8", 5555))
    r = c.get(url, headers={"X-Forwarded-For": "192.168.1.2"})  # a header changes nothing
    assert r.status_code == 403 and "mạng nội bộ" in r.json()["detail"]


def test_lan_only_can_be_turned_off(tmp_path):
    assert client(tmp_path, who=("8.8.8.8", 5555), HUB_LAN_ONLY="false").get("/api/hub").status_code == 200


def test_public_attempt_is_logged(tmp_path, caplog):
    client(tmp_path, who=("203.0.113.9", 1)).get("/api/library")
    assert "Blocked request from public address 203.0.113.9" in caplog.text


# ======================================================================= no PIN: as before


def test_without_pin_nothing_changes(tmp_path):
    c = client(tmp_path)
    assert c.get("/").status_code == 200
    assert c.get("/api/library").status_code == 200
    assert c.get("/api/hub").json()["pin"] is False
    assert c.get("/pin").status_code == 303  # nothing to type: straight to the library
    assert c.post("/api/pin", json={"pin": "x"}).json() == {"ok": True}
    assert c.get("/admin").status_code == 403  # admin still server only (4.2)
    assert client(tmp_path, who=("127.0.0.1", 1)).get("/admin").status_code == 200


# ======================================================================= PIN on


@pytest.mark.parametrize("url,where", [("/", "/pin?next=%2F"),
                                       ("/watch/0123456789abcdef?t=1", "/pin?next=%2Fwatch%2F0123456789abcdef%3Ft%3D1"),
                                       ("/docs", "/pin?next=%2Fdocs")])
def test_pages_go_to_the_pin_page(tmp_path, url, where):
    r = client(tmp_path, HUB_PIN=PIN).get(url)
    assert r.status_code == 303 and r.headers["location"] == where


@pytest.mark.parametrize("url", ["/api/library", "/api/folders", "/api/media/0123456789abcdef",
                                 "/stream/0123456789abcdef", "/thumbnails/0123456789abcdef.jpg",
                                 "/subtitles/0123456789abcdef", "/hls/s/0123456789abcdef/index.m3u8",
                                 "/openapi.json", "/api/rescan"])
def test_everything_else_needs_the_pin(tmp_path, url):
    r = client(tmp_path, HUB_PIN=PIN).get(url)
    assert r.status_code == 401 and r.json()["detail"] == "PIN required"


def test_pin_page_and_its_files_are_open(tmp_path):
    c = client(tmp_path, HUB_PIN=PIN)
    page = c.get("/pin")
    assert page.status_code == 200 and "Nhập mã PIN" in page.text
    assert c.get("/static/pin.js").status_code == 200 and c.get("/static/app.css").status_code == 200
    assert c.get("/health").status_code == 200


def test_right_pin_unlocks_this_device(tmp_path):
    c = client(tmp_path, HUB_PIN=PIN)
    r = c.post("/api/pin", json={"pin": PIN})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("hub_pin=") and "HttpOnly" in cookie and "SameSite=Lax" in cookie
    assert "Max-Age=2592000" in cookie  # 30 days
    assert c.get("/").status_code == 200
    assert c.get("/api/library").status_code == 200
    assert c.get("/api/hub").json()["pin"] is True
    assert c.get("/pin?next=/watch/abc").headers["location"] == "/watch/abc"  # already unlocked
    # Another device is not unlocked by it.
    assert client(tmp_path, who=OTHER_PHONE, HUB_PIN=PIN).get("/api/library").status_code == 401


def test_right_pin_never_opens_the_admin_page(tmp_path):
    c = client(tmp_path, HUB_PIN=PIN)
    assert c.get("/admin").status_code == 403  # without the PIN: the "server only" page, not /pin
    c.post("/api/pin", json={"pin": PIN})
    assert c.get("/admin").status_code == 403
    assert c.get("/api/admin/libraries").status_code == 403
    assert c.put("/api/admin/libraries", json={"paths": ["C:\\"]}).status_code == 403


def test_server_computer_needs_no_pin(tmp_path):
    c = client(tmp_path, who=("127.0.0.1", 1), HUB_PIN=PIN)
    assert c.get("/").status_code == 200 and c.get("/admin").status_code == 200
    assert c.get("/api/library").status_code == 200


def test_wrong_pins_lock_the_device_for_a_while(tmp_path):
    c = client(tmp_path, HUB_PIN=PIN)
    lefts = [c.post("/api/pin", json={"pin": "0000"}).json().get("attempts_left") for _ in range(4)]
    assert lefts == [4, 3, 2, 1]
    r = c.post("/api/pin", json={"pin": "0000"})  # 5th wrong one
    assert r.status_code == 429 and r.headers["retry-after"] == "300" and "5 phút" in r.json()["detail"]
    r = c.post("/api/pin", json={"pin": PIN})  # even the right one waits
    assert r.status_code == 429 and "set-cookie" not in r.headers
    assert c.get("/api/library").status_code == 401
    # Another device on the LAN is not locked out by it.
    other = TestClient(c.app, client=OTHER_PHONE)
    assert other.post("/api/pin", json={"pin": PIN}).status_code == 200


def test_lockout_ends(tmp_path):
    g = PinGuard(PIN, tmp_path / "k", max_attempts=2, lockout_seconds=60, remember_seconds=100)
    assert g.check("1.2.3.4", "x", now=0) == (False, 1, 0)
    assert g.check("1.2.3.4", "x", now=1) == (False, 0, 60)
    assert g.check("1.2.3.4", PIN, now=30)[0] is False and g.locked_for("1.2.3.4", now=30) == 31
    assert g.check("1.2.3.4", PIN, now=62) == (True, 2, 0)
    assert g.check("1.2.3.4", "x", now=63) == (False, 1, 0)  # a fresh count after a success


def test_cookie_is_signed_expires_and_follows_the_pin(tmp_path):
    key = tmp_path / "hub-secret.key"
    g = PinGuard(PIN, key, max_attempts=5, lockout_seconds=60, remember_seconds=1000)
    token = g.make_token(now=10_000)
    assert g.token_ok(token, now=10_500)
    assert not g.token_ok(token, now=11_001)  # older than remember_seconds
    assert not g.token_ok(token[:-2] + "AA", now=10_500)  # tampered
    assert not g.token_ok("10000", now=10_500) and not g.token_ok(None) and not g.token_ok("x.y")
    assert PinGuard(PIN, key, max_attempts=5, lockout_seconds=60, remember_seconds=1000).token_ok(token, now=10_500)
    assert not PinGuard("9999", key, max_attempts=5, lockout_seconds=60, remember_seconds=1000).token_ok(token, now=10_500)
    assert key.read_text().strip() and len(bytes.fromhex(key.read_text())) == 32


def test_cookie_survives_a_restart_but_not_a_new_pin(tmp_path):
    c = client(tmp_path, HUB_PIN=PIN)
    c.post("/api/pin", json={"pin": PIN})
    token = c.cookies.get("hub_pin")
    again = client(tmp_path, HUB_PIN=PIN)
    again.cookies.set("hub_pin", token)
    assert again.get("/api/library").status_code == 200
    changed = client(tmp_path, HUB_PIN="1357")
    changed.cookies.set("hub_pin", token)
    assert changed.get("/api/library").status_code == 401


@pytest.mark.parametrize("target", ["//evil.example/x", "https://evil.example", "/\\evil", "watch"])
def test_pin_page_never_sends_to_another_site(tmp_path, target):
    c = client(tmp_path, HUB_PIN=PIN)
    c.post("/api/pin", json={"pin": PIN})
    assert c.get("/pin", params={"next": target}).headers["location"] == "/"


def test_pin_settings(tmp_path):
    with pytest.raises(ConfigError, match="4 to 32"):
        settings(tmp_path, HUB_PIN="12")
    s = settings(tmp_path, HUB_PIN=" 2468 ")
    assert s.access_pin == "2468" and "2468" not in describe(s) and "PIN on" in describe(s)
    assert "2468" not in repr(s)
    assert settings(tmp_path, HUB_PIN="").access_pin is None
    yml = tmp_path / "config.yml"
    yml.write_text("access:\n  pin: '1234'\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"\.env"):
        load_settings(config_file=yml, environ={}, env_file=tmp_path / "none.env")


# ======================================================================= in the browser


@pytest.fixture
def phone_hub(tmp_path, monkeypatch):
    """A live hub where the browser counts as another device (it really runs on this
    computer, which would otherwise be "the server": no PIN, admin allowed)."""
    monkeypatch.setattr("source.modules.server.access.is_server_request", lambda scope: False)
    monkeypatch.setattr("source.modules.server.local_only.is_server_request", lambda scope: False)
    monkeypatch.setattr("source.modules.server.app.is_server_request", lambda scope: False)
    app = create_app(settings(tmp_path, HUB_PIN=PIN))
    sock = bind_socket("127.0.0.1", 0)
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


def test_pin_in_the_browser(phone_hub):
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="msedge", headless=True)
        except Exception:
            browser = p.chromium.launch(headless=True)
        shots = PROJECT_ROOT / "docs" / "planning" / "img" if os.environ.get("HUB_SCREENSHOTS") else None
        errors = []
        phone = {"viewport": {"width": 390, "height": 844}, "is_mobile": True, "has_touch": True}

        # The right PIN: back to the page that was asked; /admin stays closed.
        page = browser.new_context(**phone).new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{phone_hub}/?folder=Phim%20l%E1%BA%BB")
        assert "/pin?next=" in page.url
        if shots:
            page.screenshot(path=str(shots / "5.2-pin.png"))
        page.fill("#pin", PIN)
        page.press("#pin", "Enter")
        page.wait_for_url(re.compile(r"/\?folder="))  # back to the page that was asked
        page.wait_for_selector("#crumbs")  # the library page itself
        assert page.evaluate("fetch('/api/library').then(r => r.status)") == 200  # unlocked
        page.goto(f"{phone_hub}/admin")
        assert "chỉ mở được trên máy chủ" in page.inner_text("body")

        # Wrong PINs (another tab: all tabs here share 127.0.0.1, i.e. count as one device).
        page = browser.new_context(**phone).new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{phone_hub}/")
        for left in (4, 3, 2, 1):
            page.fill("#pin", "0000")
            page.press("#pin", "Enter")
            page.wait_for_function(f"document.querySelector('#pin-message').textContent.includes('Còn {left} lần thử')")
        page.fill("#pin", "0000")
        page.press("#pin", "Enter")
        page.wait_for_function("document.querySelector('#pin').disabled")
        assert "Nhập sai quá nhiều lần" in page.inner_text("#pin-message")
        if shots:
            page.screenshot(path=str(shots / "5.2-locked.png"))
        browser.close()
        assert errors == []
