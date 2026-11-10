"""Tests for plan step 4.4: the device test page (/diagnostics) and the matrix script."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from source.modules.server import bind_socket, create_app
from source.modules.server.diagnostics import MAX_REPORT_BYTES, MAX_REPORTS, SAMPLES, save_report
from source.schemas.api import DiagnosticSamples
from source.utils.config import load_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LIBRARY = PROJECT_ROOT / "test" / "fixtures" / "library"
HAS_FFMPEG = shutil.which("ffmpeg") is not None
SCRIPT = PROJECT_ROOT / "scripts" / "device_matrix.py"


def settings(tmp_path, **env):
    return load_settings(environ={"HUB_INDEX_PATH": os.environ["HUB_INDEX_PATH"],
                                  "HUB_CACHE_DIR": os.environ["HUB_CACHE_DIR"], **env},
                         env_file=tmp_path / "none.env", cli_paths=[str(LIBRARY)])


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(settings(tmp_path)), client=("192.168.77.9", 1234)) as c:  # any LAN device
        yield c


def report(**over) -> dict:
    base = {"device": "Android – Chrome 120", "user_agent": "Mozilla/5.0 (Linux; Android 14)", "auto": False,
            "screen": "412×915@2.6", "touch": True,
            "checks": {"play": {"ok": True, "detail": "640×360"}, "seek": {"ok": True, "detail": ""},
                       "range": {"ok": True, "detail": "HTTP 206"}, "subtitles": {"ok": True, "detail": ""},
                       "resume_storage": {"ok": True, "detail": ""}, "fullscreen_api": {"ok": True, "detail": ""}},
            "formats": [{"key": "mp4-h264", "can_play_type": "probably", "plays": True, "picture": True, "sound": True,
                         "detail": "640×360"},
                        {"key": "mkv-hevc", "can_play_type": "", "plays": True, "picture": False, "sound": True,
                         "detail": "chỉ có tiếng"},
                        {"key": "mp4-h264-ac3", "can_play_type": "", "plays": True, "picture": True, "sound": False,
                         "detail": "không có tiếng"},
                        {"key": "mp4-av1", "can_play_type": "", "plays": False, "picture": None, "sound": None,
                         "detail": "định dạng không hỗ trợ"}],
            "confirmations": {"subtitle_visible": True, "fullscreen": True, "sound": True}, "notes": "ổn"}
    base.update(over)
    return base


# ======================================================================= samples


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg needed")
def test_samples_are_made_once_and_served_with_range(client, _isolated_cache):
    r = client.get("/api/diagnostics/samples")
    assert r.status_code == 200
    data = DiagnosticSamples.model_validate(r.json())
    assert [s.key for s in data.samples] == [s.key for s in SAMPLES if s.key not in data.missing]
    main = next(s for s in data.samples if s.must_play)
    assert main.key == "mp4-h264" and main.seconds == 60
    folder = _isolated_cache / "diagnostics"
    stamps = {f.name: f.stat().st_mtime_ns for f in folder.iterdir()}
    client.get("/api/diagnostics/samples")  # second call: nothing remade
    assert {f.name: f.stat().st_mtime_ns for f in folder.iterdir()} == stamps

    part = client.get(main.url, headers={"Range": "bytes=0-99"})
    assert (part.status_code, len(part.content), part.headers["content-type"]) == (206, 100, "video/mp4")
    vtt = client.get(data.subtitles_url)
    assert vtt.headers["content-type"].startswith("text/vtt") and "giây thứ 30" in vtt.text
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name", "-of", "csv=p=0",
                            str(folder / "sample-h264.mp4")], capture_output=True, text=True)
    assert probe.stdout.split() == ["h264", "aac"]


def test_only_the_known_samples_are_served(client):
    for name in ("../library.db", "..%2F..%2Fpyproject.toml", "sample.exe", "library-paths.json"):
        assert client.get(f"/diagnostics/samples/{name}").status_code == 404


# ======================================================================= reports


def test_report_is_saved(client, _isolated_index):
    r = client.post("/api/diagnostics/reports", json=report())
    assert r.status_code == 201
    saved = _isolated_index.parent / "device-reports" / r.json()["saved"]
    data = json.loads(saved.read_text(encoding="utf-8"))
    assert data["device"] == "Android – Chrome 120" and data["client_ip"] == "192.168.77.9"
    assert r.json()["saved"].endswith("-android-chrome-120.json")


@pytest.mark.parametrize("bad", [{"device": ""}, {"device": "x" * 81}, {"formats": [{"key": "a"}]},
                                 {"unknown_field": 1}, {"checks": "no"}])
def test_invalid_report_refused(client, bad, _isolated_index):
    r = client.post("/api/diagnostics/reports", json=report(**bad))
    assert r.status_code == 422
    assert not (_isolated_index.parent / "device-reports").exists()


def test_oversized_report_refused(client):
    big = report(notes="x" * 900, user_agent="y" * 600)
    big["padding"] = "z" * MAX_REPORT_BYTES
    r = client.post("/api/diagnostics/reports", content=json.dumps(big), headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def key_report(**over) -> dict:
    """A remote-key test (plan step 5.4): sent on its own, with no video checks."""
    base = {"device": "Sony TV – Chrome 120", "user_agent": "Mozilla/5.0 (Linux; Android 12; BRAVIA 4K)",
            "auto": False, "screen": "960×540@2", "touch": False, "checks": {}, "formats": [], "confirmations": {},
            "keys": [{"button": "up", "received": True, "via": "keydown", "key": "ArrowUp", "code": "ArrowUp",
                      "key_code": 38},
                     {"button": "ok", "received": True, "via": "keydown", "key": "Enter", "code": "Enter",
                      "key_code": 13},
                     {"button": "back", "received": True, "via": "popstate", "key": "", "code": "", "key_code": 0},
                     {"button": "play", "received": True, "via": "keyup", "key": "MediaPlay", "code": "",
                      "key_code": 126},
                     {"button": "stop", "received": False, "via": None, "key": "", "code": "", "key_code": 0},
                     {"button": "rewind", "received": None, "via": None, "key": "", "code": "", "key_code": 0},
                     {"button": "fast_forward", "received": True, "via": "click", "key": "", "code": "",
                      "key_code": 0}],
            "key_log": [{"type": "keydown", "key": "ArrowUp", "code": "ArrowUp", "key_code": 38, "ms": 1200},
                        {"type": "popstate", "key": "", "code": "", "key_code": 0, "ms": 5300}],
            "notes": ""}
    base.update(over)
    return base


def test_key_report_is_saved(client, _isolated_index):
    r = client.post("/api/diagnostics/reports", json=key_report())
    assert r.status_code == 201
    data = json.loads((_isolated_index.parent / "device-reports" / r.json()["saved"]).read_text(encoding="utf-8"))
    assert data["keys"][0] == {"button": "up", "received": True, "via": "keydown", "key": "ArrowUp",
                               "code": "ArrowUp", "key_code": 38}
    assert data["key_log"][1]["type"] == "popstate" and data["checks"] == {} and data["formats"] == []


def test_old_reports_without_keys_still_accepted(client):
    assert "keys" not in report()
    assert client.post("/api/diagnostics/reports", json=report()).status_code == 201


@pytest.mark.parametrize("bad", [
    {"keys": [{"button": "volume_up", "received": True}]},                       # not a button we ask for
    {"keys": [{"button": "up", "received": True, "via": "touch"}]},              # unknown event type
    {"keys": [{"button": "up", "received": True, "key": "x" * 41}]},
    {"keys": [{"button": "up", "received": True, "key_code": -1}]},
    {"keys": [{"button": "up", "received": True}] * 21},
    {"key_log": [{"type": "keydown", "ms": 1}] * 61},
    {"key_log": [{"type": "keydown", "ms": -5}]},
])
def test_invalid_key_report_refused(client, bad, _isolated_index):
    assert client.post("/api/diagnostics/reports", json=key_report(**bad)).status_code == 422
    assert not (_isolated_index.parent / "device-reports").exists()


def test_report_count_is_capped(tmp_path):
    for i in range(MAX_REPORTS + 5):
        (tmp_path / f"20200101-{i:06d}-old.json").write_text("{}")
    save_report(tmp_path, report(), "127.0.0.1")
    assert len(list(tmp_path.glob("*.json"))) == MAX_REPORTS


def test_not_in_production(tmp_path):
    with TestClient(create_app(settings(tmp_path, APP_ENV="production"))) as c:
        for url in ("/diagnostics", "/api/diagnostics/samples"):
            assert c.get(url).status_code == 404
        assert c.post("/api/diagnostics/reports", json=report()).status_code in (404, 405)


# ======================================================================= the matrix script


def run_script(reports: Path, out: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), "--reports", str(reports), "--out", str(out)],
                          capture_output=True, text=True, encoding="utf-8", cwd=PROJECT_ROOT)


def test_matrix_is_built_and_manual_notes_kept(tmp_path):
    reports = tmp_path / "reports"
    save_report(reports, report(device="Android – Chrome 119"), "192.168.1.20")
    time.sleep(1.1)  # newer report of the same device wins
    save_report(reports, report(), "192.168.1.20")
    save_report(reports, report(device="iPhone 13 – Safari 17", auto=True, confirmations={}), "192.168.1.21")
    out = tmp_path / "device-matrix.md"
    assert run_script(reports, out).returncode == 0
    text = out.read_text(encoding="utf-8")
    assert "| Android – Chrome 120 |" in text and "Android – Chrome 119" in text  # different names: both
    row = next(line for line in text.splitlines() if line.startswith("| Android – Chrome 120 |"))
    cells = [c.strip() for c in row.strip("|").split("|")]
    assert cells[2:9] == ["✔", "✔", "✔ (thấy: ✔)", "✔", "✔", "✔", "✔"]
    assert cells[9] == "✔" and cells[13] == "🔈" and cells[14] == "✘" and cells[15] == "🔇"
    assert "iPhone 13 – Safari 17 *(tự động)*" in text
    assert "MKV · HEVC (H.265) + AAC chỉ có tiếng, không có hình" in text
    assert "MP4 · H.264 + AC-3 (Dolby) có hình nhưng không có tiếng" in text

    # Hand-written notes outside the AUTO markers survive a rebuild.
    out.write_text(text.replace("## Ghi chú thủ công\n", "## Ghi chú thủ công\nĐã thử ngày 7/11 ở phòng khách.\n"),
                   encoding="utf-8")
    assert run_script(reports, out).returncode == 0
    assert "Đã thử ngày 7/11 ở phòng khách." in out.read_text(encoding="utf-8")


def test_matrix_with_no_reports(tmp_path):
    out = tmp_path / "m.md"
    assert run_script(tmp_path / "none", out).returncode == 0
    text = out.read_text(encoding="utf-8")
    assert "chưa có báo cáo nào" in text and "chưa có báo cáo phím nào" in text


def test_matrix_marks_sound_the_browser_cannot_tell(tmp_path):
    """Safari plays the samples but does not say whether it decoded their sound: not a plain ✔."""
    reports = tmp_path / "reports"
    formats = [{"key": "mp4-h264-ac3", "can_play_type": "probably", "plays": True, "picture": True, "sound": None,
                "detail": "320×180"}]
    save_report(reports, report(device="iPhone – Safari 26", formats=formats), "192.168.1.40")
    out = tmp_path / "m.md"
    assert run_script(reports, out).returncode == 0
    text = out.read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if line.startswith("| iPhone – Safari 26 |"))
    assert [c.strip() for c in row.strip("|").split("|")][15] == "✔ (tiếng?)"
    assert "*✔ (tiếng?)*" in text


def test_matrix_shows_remote_keys_without_hiding_video_results(tmp_path):
    reports = tmp_path / "reports"
    save_report(reports, report(device="Sony TV – Chrome 120"), "192.168.1.30")
    time.sleep(1.1)  # the key report is newer: it must not replace the video results of the same TV
    save_report(reports, key_report(), "192.168.1.30")
    out = tmp_path / "device-matrix.md"
    result = run_script(reports, out)
    assert result.returncode == 0 and "1 remote-key report(s)" in result.stdout
    text = out.read_text(encoding="utf-8")
    video_row = next(line for line in text.splitlines() if line.startswith("| Sony TV – Chrome 120 |"))
    assert [c.strip() for c in video_row.strip("|").split("|")][2] == "✔"  # video row still there
    keys_part = text.split("### Phím remote (bước 5.4)", 1)[1].split("###", 1)[0]
    row = next(line for line in keys_part.splitlines() if line.startswith("| Sony TV – Chrome 120 |"))
    cells = [c.strip() for c in row.strip("|").split("|")]
    # Lên, Xuống, Trái, Phải, OK, Back, Play, Pause, Stop, Tua lùi, Tua nhanh
    assert cells[2:] == ["`ArrowUp` (38)", "–", "–", "–", "`Enter` (13)", "lịch sử (Back của trình duyệt)",
                         "`MediaPlay` (126) chỉ keyup", "–", "✘", "–", "click (con trỏ)"]
    assert "Sony TV – Chrome 120 (phím remote)" in text


# ======================================================================= the page itself, in Edge


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg needed")
def test_page_runs_and_sends_a_report(tmp_path, _isolated_index):
    sync_api = pytest.importorskip("playwright.sync_api")
    app = create_app(settings(tmp_path))
    sock = bind_socket("127.0.0.1", 0)
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    try:
        with sync_api.sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="msedge", headless=True)
            except Exception:
                pytest.skip("Microsoft Edge is needed (H.264)")
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"{base}/diagnostics?auto=1&device=Test%20Edge")
            page.wait_for_selector("body[data-sent]", timeout=180000)
            browser.close()
        assert errors == []
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
    saved = list((_isolated_index.parent / "device-reports").glob("*.json"))
    assert len(saved) == 1
    data = json.loads(saved[0].read_text(encoding="utf-8"))
    assert data["device"] == "Test Edge" and data["auto"] is True
    for key in ("range", "play", "seek", "subtitles", "resume_storage"):
        assert data["checks"][key]["ok"] is True, (key, data["checks"][key])
    h264 = next(f for f in data["formats"] if f["key"] == "mp4-h264")
    assert (h264["plays"], h264["picture"], h264["sound"]) == (True, True, True)
    assert {f["key"] for f in data["formats"]} == {s.key for s in SAMPLES}


IPHONE_DESKTOP_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
                     "Version/26.6.1 Safari/605.1.15")
SONY_WEBVIEW_UA = ("Mozilla/5.0 (Linux; Android 11; BRAVIA VU1 Build/RTT7.210105.001; wv) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Version/4.0 Chrome/156.0.8078.47 Mobile Safari/537.36")


@pytest.mark.parametrize("ua,touch,viewport,expected", [
    (IPHONE_DESKTOP_UA, True, (393, 852), "iPhone – Safari 26"),   # the report that said "Mac – Safari 26"
    (IPHONE_DESKTOP_UA, True, (1024, 1366), "iPad – Safari 26"),
    (IPHONE_DESKTOP_UA, False, (1440, 900), "Mac – Safari 26"),
    (SONY_WEBVIEW_UA, True, (960, 540), "Sony TV – WebView 156"),
])
def test_device_name_guess(tmp_path, ua, touch, viewport, expected):
    sync_api = pytest.importorskip("playwright.sync_api")
    app = create_app(settings(tmp_path))
    sock = bind_socket("127.0.0.1", 0)
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    try:
        with sync_api.sync_playwright() as p:
            browser = None
            for kwargs in ({"channel": "msedge"}, {}):
                try:
                    browser = p.chromium.launch(headless=True, **kwargs)
                    break
                except Exception:
                    continue
            if browser is None:
                pytest.skip("no Chromium-based browser for Playwright")
            ctx = browser.new_context(user_agent=ua, has_touch=touch, is_mobile=touch,
                                      viewport={"width": viewport[0], "height": viewport[1]},
                                      screen={"width": viewport[0], "height": viewport[1]})
            page = ctx.new_page()
            page.goto(f"http://127.0.0.1:{sock.getsockname()[1]}/diagnostics")
            assert page.input_value("#device") == expected
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


# ======================================================================= remote-key test (5.4), in a browser


def test_remote_key_test_records_what_the_browser_receives(tmp_path, _isolated_index):
    """Every way a TV remote can reach the page: key events, keyup only, Back through the
    history, a cursor click, nothing at all (timeout), and a skipped button."""
    sync_api = pytest.importorskip("playwright.sync_api")
    app = create_app(settings(tmp_path))
    sock = bind_socket("127.0.0.1", 0)
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    try:
        with sync_api.sync_playwright() as p:
            browser = None
            for kwargs in ({"channel": "msedge"}, {}):
                try:
                    browser = p.chromium.launch(headless=True, **kwargs)
                    break
                except Exception:
                    continue
            if browser is None:
                pytest.skip("no Chromium-based browser for Playwright")
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"{base}/diagnostics?keywait=2")
            page.fill("#device", "Test TV")
            history_before = page.evaluate("history.length")
            page.click("#remote-start")

            def at(n: int) -> None:  # wait for button n, then past the 400 ms guard
                page.wait_for_function(f"document.querySelector('#remote-step').textContent.startsWith('Nút {n}/')")
                page.wait_for_timeout(500)

            at(1)
            page.keyboard.press("ArrowUp")
            at(2)
            page.keyboard.down("ArrowDown")  # held down: repeats must not answer the next buttons
            page.keyboard.down("ArrowDown")
            page.keyboard.up("ArrowDown")
            at(3)
            page.keyboard.press("ArrowLeft")
            at(4)
            page.keyboard.press("ArrowRight")
            at(5)
            page.keyboard.press("Enter")  # must not press the focused button either
            at(6)
            page.evaluate("history.back()")  # Back handled by the browser itself
            at(7)
            page.keyboard.press("MediaPlayPause")
            at(8)
            page.click("#remote-skip")
            at(9)
            page.evaluate("document.dispatchEvent(new KeyboardEvent('keyup', "
                          "{key: 'MediaStop', code: 'MediaStop', bubbles: true}))")  # keyup only
            at(10)  # nothing pressed: times out after 2 s
            page.wait_for_function("document.querySelector('#remote-step').textContent.startsWith('Nút 11/')",
                                   timeout=6000)
            page.wait_for_timeout(500)
            page.click("#remote-prompt")  # a cursor click
            page.wait_for_selector("#remote-summary:not([hidden])")
            assert page.url.startswith(f"{base}/diagnostics")  # Back did not leave the page
            page.wait_for_function("!(history.state && history.state.hubRemoteTest)")  # extra entry removed
            assert page.evaluate("history.length") >= history_before
            rows = page.locator("#remote-results tr").count()
            page.fill("#remote-notes", "thử tự động")
            page.click("#remote-send")
            page.wait_for_selector("body[data-remote-sent]")

            # After the test the page behaves normally again: keys are not swallowed.
            page.focus("#device")
            page.keyboard.press("End")
            page.keyboard.type("!")
            assert page.input_value("#device") == "Test TV!"
            browser.close()
        assert errors == []
        assert rows == 11
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
    saved = list((_isolated_index.parent / "device-reports").glob("*.json"))
    assert len(saved) == 1
    data = json.loads(saved[0].read_text(encoding="utf-8"))
    assert data["device"] == "Test TV" and data["checks"] == {} and data["formats"] == []
    assert data["notes"] == "thử tự động"
    got = {k["button"]: (k["received"], k["via"], k["key"]) for k in data["keys"]}
    assert got == {
        "up": (True, "keydown", "ArrowUp"), "down": (True, "keydown", "ArrowDown"),
        "left": (True, "keydown", "ArrowLeft"), "right": (True, "keydown", "ArrowRight"),
        "ok": (True, "keydown", "Enter"), "back": (True, "popstate", ""),
        "play": (True, "keydown", "MediaPlayPause"), "pause": (None, None, ""),
        "stop": (True, "keyup", "MediaStop"), "rewind": (False, None, ""), "fast_forward": (True, "click", ""),
    }
    up = next(k for k in data["keys"] if k["button"] == "up")
    assert up["code"] == "ArrowUp" and up["key_code"] == 38
    types = [e["type"] for e in data["key_log"]]
    assert "popstate" in types and "click" in types and types.count("keydown") >= 7
