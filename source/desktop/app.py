"""House Cinema desktop app: the hub runs in the background with a tray icon.

Windows / Linux: a system-tray icon. Double-click it (left button) to show or hide the
console window (everything the hub prints); right-click for the quick menu.
macOS: a menu-bar icon (click for the menu); the log opens in the Console app.

    python -m source.desktop [--port 8080] [--no-browser]      # from source
    HouseCinema.exe [--port 8080] [--no-browser]               # packaged app
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Callable, Sequence
from pathlib import Path

APP_NAME = "House Cinema"
IS_MAC = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"

logger = logging.getLogger("house_cinema.desktop")


# ============================================================================ hub in a thread


class Hub:
    """Runs the hub (source.pipeline's server path) in a background thread and reports its
    state: starting, running, stopped, error."""

    def __init__(self, args: argparse.Namespace, on_change: Callable[[], None]):
        self.args = args
        self.on_change = on_change
        self.state = "stopped"
        self.detail = ""
        self.settings = None
        self.started_once = threading.Event()
        self._server = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def port(self) -> int:
        if self.settings is not None:
            return self.settings.port
        return self.args.port or 8000

    @property
    def has_library(self) -> bool:
        return bool(self.settings is not None and self.settings.library_paths)

    def _set(self, state: str, detail: str = "") -> None:
        self.state, self.detail = state, detail
        try:
            self.on_change()
        except Exception as exc:  # the tray may already be gone while quitting
            logger.debug("state callback failed: %s", exc)

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._server = None
            self._thread = threading.Thread(target=self._run, name="hub", daemon=True)
            self._set("starting")
            self._thread.start()

    def stop(self, timeout: float = 20) -> None:
        with self._lock:
            thread, server = self._thread, self._server
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout)

    def restart(self) -> None:
        print("\n---------------- Khởi động lại hub ----------------\n", flush=True)
        self.stop()
        self.start()

    def _got_server(self, server) -> None:
        with self._lock:
            self._server = server

        def wait_started() -> None:
            while not server.started and self._thread is not None and self._thread.is_alive():
                if server.should_exit:
                    return
                time.sleep(0.1)
            if server.started and not server.should_exit:
                self.started_once.set()
                self._set("running")

        threading.Thread(target=wait_started, name="hub-started", daemon=True).start()

    def _run(self) -> None:
        from source.modules.library import LibraryIndexError, read_known_root_paths
        from source.modules.server import ServerStartError, run_server
        from source.pipeline import _setup_logging
        from source.utils.config import ConfigError, describe, load_settings

        try:
            settings = load_settings(
                config_file=self.args.config, env_file=self.args.env_file, cli_paths=self.args.paths,
                cli_overrides={"host": self.args.host, "port": self.args.port, "log_level": self.args.log_level},
                known_roots_reader=read_known_root_paths,
            )
        except ConfigError as exc:
            for line in exc.lines:
                print(f"Config error: {line}", file=sys.stderr)
            self._set("error", "Lỗi cấu hình: " + (exc.lines[0] if exc.lines else ""))
            return
        self.settings = settings
        _setup_logging(settings.log_level)
        logger.info("Effective configuration:\n%s", describe(settings))
        try:
            run_server(settings, on_server=self._got_server)
        except ServerStartError as exc:
            print(f"Server error: {exc}", file=sys.stderr)
            self._set("error", str(exc))
            return
        except LibraryIndexError as exc:
            print(f"Index error: {exc}", file=sys.stderr)
            self._set("error", f"Lỗi chỉ mục thư viện: {exc}")
            return
        except Exception as exc:  # anything else: show it instead of dying silently
            logger.exception("The hub stopped with an error")
            self._set("error", f"Hub dừng vì lỗi: {exc}")
            return
        self._set("stopped")


# ============================================================================ helpers


def open_folder(path: Path) -> None:
    """Open a folder (made if missing) in Explorer / Finder."""
    path.mkdir(parents=True, exist_ok=True)
    if IS_WINDOWS:
        os.startfile(str(path))  # noqa: S606
    elif IS_MAC:
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def lan_url(port: int) -> str | None:
    from source.modules.server import network

    urls = network.find_addresses().urls(port)
    return urls[0] if urls else None


class SingleInstance:
    """Windows: a named mutex, so a second double-click does not start a second hub (it
    would fail on the busy port); it opens the browser instead. Elsewhere the system
    already keeps one copy of an app running (macOS) or nothing is checked."""

    def __init__(self, name: str = "Local\\HouseCinemaDesktop"):
        self.handle = None
        self.already_running = False
        if not IS_WINDOWS:
            return
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        self.handle = kernel32.CreateMutexW(None, False, name)
        self.already_running = ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


def configured_port(args: argparse.Namespace) -> int:
    """The port a running copy would use (for the second launch)."""
    if args.port:
        return args.port
    try:
        from source.utils.config import load_settings

        return load_settings(config_file=args.config, env_file=args.env_file).port
    except Exception:
        return 8000


# ============================================================================ the tray app


class DesktopApp:
    def __init__(self, args: argparse.Namespace, log_buffer, log_path: Path):
        import pystray

        from source.desktop.icon import BLUE, GREY, RED, draw_icon
        from source.utils.paths import config_dir, data_home

        self.pystray = pystray
        self.args = args
        self.log_path = log_path
        self.data_dir = data_home()
        self.config_dir = config_dir()
        self.images = {"ok": draw_icon(64, BLUE), "starting": draw_icon(64, BLUE, badge=GREY),
                       "error": draw_icon(64, GREY, badge=RED)}
        self.window_icon = draw_icon(256, BLUE)
        self.console = None
        self.icon = None
        self._quitting = False
        self._browser_opened = False
        self.hub = Hub(args, self._on_hub_change)
        if not IS_MAC:
            from source.desktop.console import ConsoleWindow

            self.console = ConsoleWindow(
                log_buffer, title=f"{APP_NAME} — console", icon_image=self.window_icon,
                actions=[("Mở thư viện", lambda: self.open_page("/")),
                         ("Trang quản trị", lambda: self.open_page("/admin")),
                         ("Sao chép địa chỉ cho điện thoại", self.copy_lan_url)])

    # ------------------------------------------------------------------ menu actions

    def open_page(self, path: str) -> None:
        webbrowser.open(f"http://localhost:{self.hub.port}{path}")

    def toggle_console(self) -> None:
        if self.console is not None:
            self.console.call_soon(self.console.toggle)
        else:  # macOS: the Console app follows the log file live
            subprocess.Popen(["open", "-a", "Console", str(self.log_path)])

    def show_console(self) -> None:
        if self.console is not None:
            self.console.call_soon(self.console.show)

    def copy_lan_url(self) -> None:
        def work() -> None:
            url = lan_url(self.hub.port)
            if not url:
                self.notify("Không tìm thấy địa chỉ mạng LAN. Máy này đã kết nối Wi-Fi/mạng dây chưa?")
                return
            if self.console is not None:
                self.console.call_soon(lambda: self.console.copy_to_clipboard(url))
            elif IS_MAC:
                subprocess.run(["pbcopy"], input=url.encode("utf-8"), check=False)
            self.notify(f"Đã sao chép: {url} — mở địa chỉ này trên điện thoại cùng mạng.")

        threading.Thread(target=work, name="copy-url", daemon=True).start()

    def restart_hub(self) -> None:
        threading.Thread(target=self.hub.restart, name="hub-restart", daemon=True).start()

    def quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True

        def work() -> None:
            self._ui(lambda: setattr(self.icon, "title", f"{APP_NAME} — đang tắt…"))
            self.hub.stop()
            self._ui(self.icon.stop)
            if self.console is not None:
                self.console.call_soon(self.console.quit)

        threading.Thread(target=work, name="quit", daemon=True).start()

    def notify(self, message: str) -> None:
        def show() -> None:
            try:
                self.icon.notify(message, APP_NAME)
            except Exception as exc:  # notifications off / not supported
                logger.debug("notification failed: %s", exc)

        self._ui(show)

    # ------------------------------------------------------------------ state → icon

    def _ui(self, func: Callable[[], None]) -> None:
        """macOS: AppKit must be touched from the main thread only."""
        if IS_MAC:
            from PyObjCTools import AppHelper

            AppHelper.callAfter(func)
        else:
            func()

    def status_text(self) -> str:
        hub = self.hub
        if hub.state == "running":
            return f"Hub đang chạy — cổng {hub.port}"
        if hub.state == "starting":
            return "Hub đang khởi động…"
        if hub.state == "error":
            return "Hub bị lỗi — xem console"
        return "Hub đã dừng"

    def _on_hub_change(self) -> None:
        if self.icon is None or self._quitting:
            return
        state = self.hub.state
        image = self.images["ok" if state == "running" else "starting" if state == "starting" else "error"]

        def update() -> None:
            self.icon.icon = image
            self.icon.title = f"{APP_NAME} — {self.status_text()}"
            self.icon.update_menu()

        self._ui(update)
        if state == "error":
            self.notify(f"Hub không chạy được: {self.hub.detail}"[:250])
            self.show_console()
        elif state == "running" and not self._browser_opened:
            self._browser_opened = True
            if not self.args.no_browser:
                # No folder yet: the admin page is where folders are added.
                self.open_page("/" if self.hub.has_library else "/admin")

    # ------------------------------------------------------------------ build and run

    def _menu(self):
        item, menu = self.pystray.MenuItem, self.pystray.Menu
        console_label = "Xem nhật ký (Console)" if IS_MAC else "Hiện / ẩn cửa sổ console"
        return menu(
            item(lambda _: self.status_text(), None, enabled=False),
            menu.SEPARATOR,
            item("Mở thư viện", lambda: self.open_page("/")),
            item("Trang quản trị (thêm thư mục, mã QR)", lambda: self.open_page("/admin")),
            item("Sao chép địa chỉ cho điện thoại", self.copy_lan_url),
            item(console_label, self.toggle_console, default=not IS_MAC),
            menu.SEPARATOR,
            item("Mở thư mục dữ liệu", lambda: open_folder(self.data_dir)),
            item("Mở thư mục cấu hình (cổng, PIN…)", lambda: open_folder(self.config_dir)),
            item("Khởi động lại hub", self.restart_hub),
            menu.SEPARATOR,
            item("Thoát", self.quit),
        )

    def _make_icon(self):
        icon_class = self.pystray.Icon
        if IS_WINDOWS:
            icon_class = _double_click_icon_class(icon_class, self.toggle_console)
        return icon_class("house_cinema", self.images["starting"], f"{APP_NAME} — đang khởi động…",
                          menu=self._menu())

    def run(self) -> None:
        if IS_MAC:
            # pystray needs the main thread on macOS; there is no Tk window.
            self.icon = self._make_icon()
            self.icon.run(setup=self._setup)
            self.hub.stop()
            return

        def tray_thread() -> None:
            self.icon = self._make_icon()
            self.icon.run(setup=self._setup)

        thread = threading.Thread(target=tray_thread, name="tray", daemon=True)
        thread.start()
        try:
            self.console.run()  # Tk loop on the main thread until Thoát
        except KeyboardInterrupt:
            pass
        if not self._quitting:  # window loop ended some other way: stop cleanly anyway
            self._quitting = True
            self.hub.stop()
            if self.icon is not None:
                self.icon.stop()
        thread.join(timeout=5)

    def _setup(self, icon) -> None:
        icon.visible = True
        self.hub.start()


def _double_click_icon_class(base, on_double_click: Callable[[], None]):
    """pystray (Windows) runs the default menu item on a single left click. Here a single
    click does nothing and a double click (two clicks within the system double-click time)
    calls ``on_double_click``; the right button still opens the menu."""
    import ctypes

    wm_lbuttonup = 0x0202
    interval = ctypes.windll.user32.GetDoubleClickTime() / 1000

    class DoubleClickIcon(base):
        _last_click = 0.0

        def _on_notify(self, wparam, lparam):
            if lparam == wm_lbuttonup:
                now = time.monotonic()
                if now - self._last_click <= interval:
                    self._last_click = 0.0
                    on_double_click()
                else:
                    self._last_click = now
                return None
            return super()._on_notify(wparam, lparam)

    return DoubleClickIcon


# ============================================================================ entry point


def build_parser() -> argparse.ArgumentParser:
    from source.pipeline import _add_common_options

    parser = argparse.ArgumentParser(prog="HouseCinema", description=f"{APP_NAME}: the hub with a tray icon.")
    _add_common_options(parser, in_subcommand=False)
    parser.add_argument("--no-browser", action="store_true", help="Do not open the browser when the hub starts.")
    return parser


def _fatal(message: str) -> None:
    """Show an error when even the window cannot be built."""
    print(message, file=sys.stderr)
    if IS_WINDOWS:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10)
    elif IS_MAC:
        subprocess.run(["osascript", "-e", f'display alert "{APP_NAME}" message "{message}"'], check=False)


def main(argv: Sequence[str] | None = None) -> int:
    from source.desktop.logs import capture_output
    from source.utils.paths import data_home, ensure_user_config

    # Finder may pass -psn_… arguments: ignore what is not ours.
    args, _unknown = build_parser().parse_known_args(argv)
    ensure_user_config()
    log_buffer, log_path = capture_output(data_home() / "logs")
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", force=True)

    instance = SingleInstance()
    if instance.already_running:
        webbrowser.open(f"http://localhost:{configured_port(args)}/")
        return 0

    print(f"{APP_NAME} — dữ liệu: {data_home()}  |  nhật ký: {log_path}", flush=True)
    try:
        app = DesktopApp(args, log_buffer, log_path)
    except Exception as exc:
        logger.exception("Cannot start the desktop app")
        _fatal(f"Không mở được {APP_NAME}: {exc}\n\nNhật ký: {log_path}")
        return 1
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
