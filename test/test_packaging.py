"""Tests for plan step 6.1: the one-command start (run.cmd / run.ps1) and the "ffmpeg is
missing" message (terminal at start, --check-config, admin page)."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from source.modules.server import bind_socket, create_app
from source.modules.server.runner import warn_missing_tools
from source.utils.config import describe, load_settings
from source.utils.ffmpeg import INSTALL_HINT, FFTools, disabled_features

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = PROJECT_ROOT / "test" / "fixtures" / "library"
BOTH = FFTools(ffmpeg="C:/ff/ffmpeg.exe", ffprobe="C:/ff/ffprobe.exe")
NONE = FFTools(ffmpeg=None, ffprobe=None)


def settings(tmp_path, **env):
    return load_settings(environ={"HUB_INDEX_PATH": os.environ["HUB_INDEX_PATH"],
                                  "HUB_CACHE_DIR": os.environ["HUB_CACHE_DIR"], **env},
                         env_file=tmp_path / "none.env", cli_paths=[str(FIXTURES)])


# ======================================================================= what is off without ffmpeg


def test_nothing_is_off_with_both_tools():
    assert disabled_features(BOTH) == []


def test_without_ffmpeg():
    off = disabled_features(FFTools(ffmpeg=None, ffprobe="C:/ff/ffprobe.exe"))
    assert len(off) == 3
    assert any("Ảnh thu nhỏ" in f for f in off) and any("Chuyển đổi" in f for f in off)
    assert any("/diagnostics" in f for f in off) and not any("Thông tin video" in f for f in off)


def test_without_ffprobe():
    off = disabled_features(FFTools(ffmpeg="C:/ff/ffmpeg.exe", ffprobe=None))
    assert len(off) == 2  # no details → the hub cannot decide how to convert either
    assert any("Thông tin video" in f for f in off) and any("Chuyển đổi" in f for f in off)


def test_without_both():
    assert len(disabled_features(NONE)) == 4


def test_check_config_lists_what_is_off(tmp_path, monkeypatch):
    monkeypatch.setattr("source.utils.config.find_tools", lambda s: NONE)
    table = describe(settings(tmp_path))
    assert "ffmpeg       | NOT FOUND (not on PATH)" in table
    assert table.count("  off          | ") == 4
    monkeypatch.setattr("source.utils.config.find_tools", lambda s: BOTH)
    table = describe(settings(tmp_path))
    assert "C:/ff/ffmpeg.exe" in table and "off          |" not in table


def test_start_warns_once_with_the_list_and_how_to_install(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr("source.modules.server.runner.find_tools", lambda s: NONE)
    with caplog.at_level(logging.WARNING, logger="house_cinema.server"):
        features = warn_missing_tools(settings(tmp_path))
    assert len(features) == 4
    [record] = [r for r in caplog.records if "Không tìm thấy" in r.getMessage()]
    text = record.getMessage()
    assert "ffmpeg và ffprobe" in text and INSTALL_HINT in text and "winget install Gyan.FFmpeg" in text
    assert all(f in text for f in features)
    caplog.clear()
    monkeypatch.setattr("source.modules.server.runner.find_tools", lambda s: BOTH)
    assert warn_missing_tools(settings(tmp_path)) == []
    assert not caplog.records


def test_admin_tools_api_is_for_the_server_only(tmp_path, monkeypatch):
    monkeypatch.setattr("source.modules.server.app.find_tools", lambda s: NONE)
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 5)) as c:
        body = c.get("/api/admin/tools").json()
        assert body["ffmpeg"] is None and body["ffprobe"] is None and len(body["disabled"]) == 4
        assert body["hint"] == INSTALL_HINT
    with TestClient(app, client=("192.168.1.77", 5)) as c:
        assert c.get("/api/admin/tools").status_code == 403


@pytest.mark.parametrize("tools,visible", [(NONE, True), (BOTH, False)])
def test_admin_page_shows_the_box_only_when_something_is_missing(tmp_path, monkeypatch, tools, visible):
    sync_api = pytest.importorskip("playwright.sync_api")
    monkeypatch.setattr("source.modules.server.app.find_tools", lambda s: tools)
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
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{sock.getsockname()[1]}/admin")
            page.wait_for_selector("#urls li")  # the page has loaded its data
            page.wait_for_timeout(300)
            assert page.is_visible("#tools") is visible
            if visible:
                assert page.text_content("#tools-title") == "Thiếu ffmpeg và ffprobe"
                assert page.locator("#tools-off li").count() == 4
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


# ======================================================================= run.cmd / run.ps1


def test_run_ps1_is_readable_by_windows_powershell():
    raw = (PROJECT_ROOT / "run.ps1").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "PowerShell 5.1 reads a .ps1 without BOM as ANSI: Vietnamese breaks"
    text = raw.decode("utf-8-sig")
    assert "run --no-dev python -m source.pipeline @args" in text
    assert "--exact" not in text  # would remove a developer's test tools from .venv
    cmd = (PROJECT_ROOT / "run.cmd").read_text(encoding="ascii")  # cmd.exe: keep it plain ASCII
    assert "-ExecutionPolicy Bypass -File \"%~dp0run.ps1\" %*" in cmd


@pytest.mark.skipif(os.name != "nt", reason="Windows start scripts")
def test_run_cmd_starts_the_hub_command(tmp_path):
    """run.cmd passes its arguments to the hub: --check-config proves the whole chain works
    (cmd → PowerShell → uv → Python) without starting a server."""
    env = {**os.environ, "HUB_INDEX_PATH": str(tmp_path / "i" / "library.db"), "HUB_CACHE_DIR": str(tmp_path / "c")}
    r = subprocess.run(["cmd.exe", "/c", str(PROJECT_ROOT / "run.cmd"), "--check-config"], cwd=tmp_path, env=env,
                       capture_output=True, timeout=180)
    out = r.stdout.decode("utf-8", errors="replace")
    assert r.returncode == 0, out + r.stderr.decode("utf-8", errors="replace")
    assert "Configuration OK" in out and "listen" in out


@pytest.mark.skipif(os.name != "nt", reason="Windows start scripts")
def test_run_without_uv_says_how_to_install_it(tmp_path):
    powershell = shutil.which("powershell")
    system = os.environ.get("SystemRoot", r"C:\Windows")
    env = {"SystemRoot": system, "PATH": f"{system}\\System32;{system}\\System32\\WindowsPowerShell\\v1.0",
           "USERPROFILE": str(tmp_path), "TEMP": str(tmp_path), "TMP": str(tmp_path)}
    r = subprocess.run([powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(PROJECT_ROOT / "run.ps1")],
                       env=env, capture_output=True, timeout=60)
    out = r.stdout.decode("utf-8", errors="replace")
    assert r.returncode == 1, out
    assert "Không tìm thấy uv" in out and "winget install --id=astral-sh.uv" in out
