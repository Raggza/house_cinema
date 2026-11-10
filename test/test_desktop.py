"""Tests for the packaged desktop app: data folders in the packaged app, the bundled ffmpeg,
the log buffer behind the console window, the hub thread and the tray double-click."""

from __future__ import annotations

import argparse
import socket
import time
from pathlib import Path

import pytest

from source.desktop.logs import LogBuffer, LogStream
from source.utils import paths
from source.utils.ffmpeg import find_tools
from source.utils.config import load_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = PROJECT_ROOT / "test" / "fixtures" / "library"


# ======================================================================= where files go


def test_from_source_everything_stays_in_the_project(monkeypatch):
    monkeypatch.delenv(paths.HOME_ENV, raising=False)
    monkeypatch.setattr(paths, "FROZEN", False)
    assert paths.data_home() == PROJECT_ROOT
    assert paths.config_dir() == PROJECT_ROOT / "source" / "configs"
    assert paths.ensure_user_config() == paths.BUNDLED_CONFIG_DIR  # nothing copied


def test_packaged_app_uses_a_per_user_folder(monkeypatch, tmp_path):
    monkeypatch.delenv(paths.HOME_ENV, raising=False)
    monkeypatch.setattr(paths, "FROZEN", True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    home = paths.data_home()
    assert home != PROJECT_ROOT and home.is_relative_to(tmp_path)
    assert paths.config_dir() == home / "configs"


def test_home_override_and_first_start_copy(monkeypatch, tmp_path):
    monkeypatch.setenv(paths.HOME_ENV, str(tmp_path / "hc"))
    folder = paths.ensure_user_config()
    assert folder == tmp_path / "hc" / "configs"
    shipped = (paths.BUNDLED_CONFIG_DIR / "config.yml").read_text(encoding="utf-8")
    assert (folder / "config.yml").read_text(encoding="utf-8") == shipped
    assert (folder / ".env.example").is_file()
    (folder / "config.yml").write_text("server:\n  port: 9000\n", encoding="utf-8")
    paths.ensure_user_config()  # never overwrites what the user changed
    assert "9000" in (folder / "config.yml").read_text(encoding="utf-8")


def test_settings_read_the_user_config(monkeypatch, tmp_path):
    folder = tmp_path / "hc" / "configs"
    folder.mkdir(parents=True)
    (folder / "config.yml").write_text("server:\n  port: 9123\n", encoding="utf-8")
    (folder / ".env").write_text("HUB_PIN=4321\n", encoding="utf-8")
    s = load_settings(config_file=folder / "config.yml", env_file=folder / ".env", cli_paths=[str(FIXTURES)])
    assert s.port == 9123 and s.access_pin == "4321"


# ======================================================================= bundled ffmpeg


def test_no_bundled_tool_from_source(monkeypatch):
    monkeypatch.setattr(paths, "FROZEN", False)
    assert paths.bundled_tool("ffmpeg") is None


def test_bundled_ffmpeg_comes_before_path(monkeypatch, tmp_path):
    exe = ".exe" if paths.os.name == "nt" else ""
    (tmp_path / "ffmpeg").mkdir()
    for name in ("ffmpeg", "ffprobe"):
        (tmp_path / "ffmpeg" / f"{name}{exe}").write_bytes(b"")
    monkeypatch.setattr(paths, "FROZEN", True)
    monkeypatch.setattr(paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr("source.utils.ffmpeg.shutil.which", lambda name: f"/usr/bin/{name}")
    s = load_settings(env_file=tmp_path / "none.env", cli_paths=[str(FIXTURES)])
    tools = find_tools(s)
    assert tools.ffmpeg == str(tmp_path / "ffmpeg" / f"ffmpeg{exe}")
    assert tools.ffprobe == str(tmp_path / "ffmpeg" / f"ffprobe{exe}")  # the one next to it


def test_configured_ffmpeg_still_wins(monkeypatch, tmp_path):
    mine = tmp_path / "mine" / "ffmpeg.exe"
    mine.parent.mkdir()
    mine.write_bytes(b"")
    monkeypatch.setattr(paths, "FROZEN", True)
    monkeypatch.setenv("HUB_FFMPEG", str(mine))
    s = load_settings(env_file=tmp_path / "none.env", cli_paths=[str(FIXTURES)])
    assert find_tools(s).ffmpeg == str(mine)


# ======================================================================= console log buffer


def test_log_buffer_lines_and_partial_writes():
    buf = LogBuffer(max_lines=3)
    buf.add("one\ntw")
    lines, seen = buf.since(0)
    assert lines == ["one"] and seen == 1
    buf.add("o\r\nthree\rfour\n")
    lines, seen = buf.since(seen)
    assert lines == ["two", "three", "four"] and seen == 4
    buf.add("5\n6\n7\n8\n")  # more than kept: only the last 3 come back
    lines, seen = buf.since(seen)
    assert lines == ["6", "7", "8"] and seen == 8
    assert buf.since(seen) == ([], 8)


def test_log_stream_writes_file_and_buffer(tmp_path):
    buf = LogBuffer()
    with open(tmp_path / "x.log", "w", encoding="utf-8") as f:
        stream = LogStream(buf, f)
        stream.write("Xin chào ▀▄█\n")
        assert stream.encoding == "utf-8" and not stream.isatty()
    assert (tmp_path / "x.log").read_text(encoding="utf-8") == "Xin chào ▀▄█\n"
    assert buf.since(0)[0] == ["Xin chào ▀▄█"]


# ======================================================================= hub thread


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _args(**kw) -> argparse.Namespace:
    base = dict(paths=[str(FIXTURES)], host="127.0.0.1", port=_free_port(), log_level="WARNING",
                config=None, env_file=Path("missing.env"), no_browser=True)
    return argparse.Namespace(**{**base, **kw})


def _wait(predicate, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_hub_starts_restarts_and_stops():
    from source.desktop.app import Hub

    states: list[str] = []
    hub = Hub(_args(), on_change=lambda: states.append(hub.state))
    hub.start()
    try:
        assert _wait(lambda: hub.state == "running"), states
        with socket.create_connection(("127.0.0.1", hub.port), timeout=5):
            pass
        assert hub.has_library
        hub.restart()
        assert _wait(lambda: hub.state == "running" and states.count("running") == 2), states
    finally:
        hub.stop()
    assert hub.state == "stopped"
    assert states[0] == "starting" and "stopped" in states


def test_hub_reports_a_busy_port():
    from source.desktop.app import Hub

    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        hub = Hub(_args(port=taken.getsockname()[1]), on_change=lambda: None)
        hub.start()
        assert _wait(lambda: hub.state == "error")
        assert "already in use" in hub.detail
        hub.stop()


# ======================================================================= tray double-click


@pytest.mark.skipif(paths.os.name != "nt", reason="Windows tray")
def test_double_click_toggles_and_single_click_does_nothing(monkeypatch):
    from source.desktop.app import _double_click_icon_class

    class FakeIcon:
        def __init__(self):
            self.passed = []

        def _on_notify(self, wparam, lparam):
            self.passed.append(lparam)

    calls = []
    icon = _double_click_icon_class(FakeIcon, lambda: calls.append(1))()
    clock = iter([10.0, 10.2, 20.0, 30.0])
    monkeypatch.setattr("source.desktop.app.time.monotonic", lambda: next(clock))
    up, right_up = 0x0202, 0x0205
    icon._on_notify(0, up)
    assert calls == []          # one click: nothing
    icon._on_notify(0, up)
    assert calls == [1]         # second click soon after: double-click
    icon._on_notify(0, up)
    icon._on_notify(0, up)      # 10 s apart: two single clicks
    assert calls == [1]
    icon._on_notify(0, right_up)
    assert icon.passed == [right_up]  # the right button still opens the menu


# ======================================================================= packaging files


def test_spec_ships_the_files_the_hub_reads():
    spec = (PROJECT_ROOT / "packaging" / "house_cinema.spec").read_text(encoding="utf-8")
    for folder in ("source/web", "source/configs", "source/image/assets"):
        assert f'"{folder}"' in spec
        assert (PROJECT_ROOT / folder).exists()
    assert (PROJECT_ROOT / "packaging" / "house_cinema_app.py").is_file()
    iss = (PROJECT_ROOT / "packaging" / "windows" / "installer.iss").read_bytes()
    assert iss.startswith(b"\xef\xbb\xbf"), "Inno Setup reads a file without BOM as ANSI: Vietnamese breaks"
    assert b"runasoriginaluser" in iss and b"profile=private,domain" in iss
