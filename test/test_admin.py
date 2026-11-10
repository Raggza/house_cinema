"""Tests for plan step 4.2: library folders managed on the admin page (server computer only).

Flow: the hub starts with the same command and no folder → the admin page adds folders
(saved next to the index) → the library page rescans. Other devices can never use /admin.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from source import pipeline
from source.modules.library import LibraryIndex, scan_library
from source.modules.library import folders as folders_module
from source.modules.library.folders import FolderError, FoldersLocked, LibraryFolders, store_path_for
from source.modules.server import app as app_module
from source.modules.server import bind_socket, create_app
from source.modules.server import local_only
from source.modules.server.local_only import is_server_request, parse_ip
from source.schemas.api import AdminFolders, HubInfo, LibraryPage
from source.utils.config import load_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LIBRARY = PROJECT_ROOT / "test" / "fixtures" / "library"
IS_WINDOWS = sys.platform == "win32"
LOCAL = ("127.0.0.1", 50000)
REMOTE = ("192.168.77.9", 50000)  # TEST: an address that is not one of this computer's


# ======================================================================= helpers


@pytest.fixture
def lib(tmp_path) -> Path:
    root = tmp_path / "Phim"
    shutil.copytree(LIBRARY, root)
    return root


@pytest.fixture
def other(tmp_path) -> Path:
    root = tmp_path / "Phim khác"
    root.mkdir()
    (root / "Một phim.mp4").write_bytes(b"video")
    return root


def settings(tmp_path, *, cli_paths=None, env=None):
    """As the real command: no --path, repo config.yml (paths: []), test index and cache."""
    return load_settings(environ={"HUB_INDEX_PATH": os.environ["HUB_INDEX_PATH"],
                                  "HUB_CACHE_DIR": os.environ["HUB_CACHE_DIR"], **(env or {})},
                         env_file=tmp_path / "none.env", cli_paths=cli_paths)


@contextlib.contextmanager
def client(tmp_path, *, address=LOCAL, **kwargs):
    with TestClient(create_app(settings(tmp_path, **kwargs)), client=address) as c:
        yield c
        c.app.state.rescanner.wait(60)


def put(c, paths):
    return c.put("/api/admin/libraries", json={"paths": [str(p) for p in paths]})


def wait_rescan(c, timeout=60):
    deadline = time.monotonic() + timeout
    while c.get("/api/rescan").json()["state"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.1)
    return c.get("/api/rescan").json()


# ======================================================================= LibraryFolders


@pytest.fixture
def store(tmp_path) -> Path:
    return tmp_path / "data" / "library-paths.json"


def test_validate_rules(tmp_path, lib, other, store):
    f = LibraryFolders([], store=store, source="config")
    a_file = tmp_path / "file.txt"
    a_file.write_text("x")
    with pytest.raises(FolderError) as exc:
        f.validate([str(tmp_path / "không có"), str(a_file), "Phim", "\\Phim" if IS_WINDOWS else "Phim2",
                    str(lib), str(lib).upper() if IS_WINDOWS else str(lib), str(lib / "Phim bộ"), "  ", ""])
    messages = [m for _, m in exc.value.problems]
    assert any(m.startswith("Không tìm thấy thư mục") for m in messages)
    assert any(m.startswith("Đây là một file") for m in messages)
    assert sum(m.startswith("Cần đường dẫn đầy đủ") for m in messages) == 2
    assert any(m == "Thư mục này đã có trong danh sách" for m in messages)  # same folder, other case
    assert any(m.startswith("Nằm bên trong thư mục") for m in messages)  # nested
    # Quotes from Explorer's "Copy as path" and spaces are fine.
    assert f.validate([f'  "{lib}"  ', str(other)]) == [lib.resolve(), other.resolve()]


def test_unreadable_folder_refused(lib, store, monkeypatch):
    def deny(path):
        raise PermissionError(13, "Access is denied", str(path))

    monkeypatch.setattr(folders_module.os, "scandir", deny)
    with pytest.raises(FolderError, match="Không có quyền đọc"):
        LibraryFolders([], store=store, source="config").validate([str(lib)])


def test_missing_folder_already_in_list_is_kept(tmp_path, store):
    unplugged = tmp_path / "USB" / "Phim"  # does not exist (drive unplugged)
    f = LibraryFolders([unplugged], store=store, source="saved")
    assert f.validate([str(unplugged)]) == [unplugged]  # kept as is
    with pytest.raises(FolderError):  # but a *new* missing folder is refused
        f.validate([str(unplugged), str(tmp_path / "USB" / "Khác")])


def test_save_writes_file_and_applies(lib, other, store, caplog):
    caplog.set_level(logging.INFO, logger="house_cinema.library.folders")
    f = LibraryFolders([], store=store, source="config")
    v0 = f.version
    f.save([str(lib), str(other)], by="127.0.0.1")
    assert f.paths() == [lib.resolve(), other.resolve()] and f.version == v0 + 1 and f.source == "saved"
    data = json.loads(store.read_text(encoding="utf-8"))
    assert data["paths"] == [str(lib.resolve()), str(other.resolve())] and "updated_at" in data
    assert not list(store.parent.glob("*.tmp*"))  # written atomically, no leftovers
    assert any("added" in r.getMessage() and "127.0.0.1" in r.getMessage() for r in caplog.records)
    with pytest.raises(FolderError):
        f.save([str(lib), str(lib / "Phim bộ")])
    assert f.paths() == [lib.resolve(), other.resolve()]  # unchanged after a refused save


def test_cli_paths_are_locked(lib, store):
    with pytest.raises(FoldersLocked):
        LibraryFolders([lib], store=store, source="cli").save([])
    assert not store.exists()


# ======================================================================= config


def test_saved_list_wins_over_config_and_cli_wins_over_saved(tmp_path, lib, other):
    store = store_path_for(Path(os.environ["HUB_INDEX_PATH"]))
    assert settings(tmp_path, env={"HUB_LIBRARY_PATHS": str(other)}).library_paths == (other.resolve(),)
    LibraryFolders([], store=store, source="config").save([str(lib)])
    s = settings(tmp_path, env={"HUB_LIBRARY_PATHS": str(other)})
    assert (s.library_paths, s.library_source) == ((lib.resolve(),), "saved")
    s = settings(tmp_path, cli_paths=[str(other)])
    assert (s.library_paths, s.library_source) == ((other.resolve(),), "cli")


def test_saved_folder_missing_at_start_does_not_stop_the_hub(tmp_path, lib):
    store = store_path_for(Path(os.environ["HUB_INDEX_PATH"]))
    LibraryFolders([], store=store, source="config").save([str(lib)])
    lib.rename(lib.with_name("rút ổ"))
    s = settings(tmp_path)  # no ConfigError
    assert s.library_paths == (lib.resolve(),)


def test_damaged_saved_file_is_ignored(tmp_path, caplog):
    store = store_path_for(Path(os.environ["HUB_INDEX_PATH"]))
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text("{not json", encoding="utf-8")
    s = settings(tmp_path)
    assert (s.library_paths, s.library_source) == ((), "config")
    assert any("Ignoring unreadable" in r.getMessage() for r in caplog.records)


def test_cli_scan_uses_saved_folders_and_refuses_without_any(capsys, tmp_path, lib):
    args = ["--env-file", str(tmp_path / "none.env")]
    assert pipeline.main(["scan", "--no-media", *args]) == pipeline.EXIT_CONFIG_ERROR
    err = capsys.readouterr().err
    assert "no library folder yet" in err and "/admin" in err and "Traceback" not in err

    LibraryFolders([], store=store_path_for(Path(os.environ["HUB_INDEX_PATH"])), source="config").save([str(lib)])
    assert pipeline.main(["scan", "--no-media", *args]) == pipeline.EXIT_OK  # same command, no --path
    assert "6 found" in capsys.readouterr().out
    assert pipeline.main(["--check-config", *args]) == pipeline.EXIT_OK
    out = capsys.readouterr().out
    assert str(lib.resolve()) in out and "admin page" in out


# ======================================================================= server only


@pytest.mark.parametrize(("client_ip", "server_ip", "expected"), [
    ("127.0.0.1", "127.0.0.1", True),
    ("127.0.0.5", "0.0.0.0", True),          # all of 127/8 is loopback
    ("::1", "::1", True),
    ("::ffff:127.0.0.1", "::", True),        # IPv4-mapped loopback
    ("192.168.1.10", "192.168.1.10", True),  # the server opening its own LAN address
    ("192.168.1.50", "192.168.1.10", False),
    ("fe80::1%12", "fe80::2%12", False),
    ("testclient", "testserver", False),     # not an IP at all
    (None, "127.0.0.1", False),
])
def test_is_server_request(client_ip, server_ip, expected, monkeypatch):
    monkeypatch.setattr(local_only, "local_addresses", lambda: frozenset())
    scope = {"client": (client_ip, 1234) if client_ip else None, "server": (server_ip, 8000)}
    assert is_server_request(scope) is expected


def test_own_adapter_address_counts_as_server(monkeypatch):
    monkeypatch.setattr(local_only, "local_addresses", lambda: frozenset({parse_ip("10.0.0.7")}))
    assert is_server_request({"client": ("10.0.0.7", 1), "server": ("10.0.0.8", 8000)})
    assert not is_server_request({"client": ("10.0.0.9", 1), "server": ("10.0.0.8", 8000)})


def test_real_local_addresses_include_this_computer():
    addrs = local_only.local_addresses()
    assert all(not a.is_unspecified for a in addrs)


@pytest.mark.parametrize(("method", "url"), [
    ("GET", "/admin"), ("GET", "/admin/"), ("GET", "/api/admin/libraries"), ("PUT", "/api/admin/libraries"),
    ("GET", "/api/admin/anything-added-later"), ("POST", "/api/admin/libraries"),
    ("POST", "/api/admin/pick-folder"),  # a phone must never pop a dialog up on the server
])
def test_other_devices_never_reach_admin(tmp_path, lib, method, url, caplog, monkeypatch):
    caplog.set_level(logging.WARNING, logger="house_cinema.server.security")
    opened = []
    monkeypatch.setattr("source.utils.folder_picker.pick_folder", lambda *a, **k: opened.append(1))
    with client(tmp_path, address=REMOTE) as c:
        r = c.request(method, url, json={"paths": [str(lib)]},
                      headers={"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1",
                               "Host": "localhost:8000", "Forwarded": "for=127.0.0.1"})
        assert r.status_code == 403
        if url.startswith("/api/"):
            assert r.json()["detail"].startswith("The admin page can only be used on the server")
        else:
            assert "Trang quản trị chỉ mở được trên máy chủ" in r.text and "localhost:8000/admin" in r.text
        assert not store_path_for(Path(os.environ["HUB_INDEX_PATH"])).exists()  # nothing saved
    assert opened == []  # no folder picker shown
    assert any("Blocked admin access from 192.168.77.9" in rec.getMessage() for rec in caplog.records)


# ======================================================================= folder picker


def test_pick_folder_returns_the_choice_without_saving(tmp_path, lib, monkeypatch):
    from source.utils import folder_picker

    asked = []
    monkeypatch.setattr(folder_picker, "pick_folder", lambda initial=None, *a, **k: asked.append(initial) or str(lib))
    with client(tmp_path) as c:
        r = c.post("/api/admin/pick-folder")
        assert r.status_code == 200 and r.json() == {"path": str(lib), "cancelled": False}
        assert asked == [folder_picker.downloads_folder()]  # starts in Downloads
        assert c.get("/api/admin/libraries").json()["folders"] == []  # the page saves it, not this


@pytest.mark.parametrize(("raised", "status", "text"), [
    (None, 200, None),
    ("busy", 409, "đang mở"),
    ("unavailable", 501, "dán đường dẫn"),
])
def test_pick_folder_cancel_busy_unavailable(tmp_path, monkeypatch, raised, status, text):
    from source.utils import folder_picker

    def fake(*a, **k):
        if raised == "busy":
            raise folder_picker.PickerBusy("open")
        if raised == "unavailable":
            raise folder_picker.PickerUnavailable("No desktop session on the server.")
        return None  # cancelled

    monkeypatch.setattr(folder_picker, "pick_folder", fake)
    with client(tmp_path) as c:
        r = c.post("/api/admin/pick-folder")
        assert r.status_code == status
        if text is None:
            assert r.json() == {"path": None, "cancelled": True}
        else:
            assert text in r.json()["detail"]


def test_only_one_picker_at_a_time(monkeypatch):
    from source.utils import folder_picker

    gate, inside = threading.Event(), threading.Event()

    def slow(initial, title):
        inside.set()
        gate.wait(5)
        return "X"

    impl = "_" + {"win32": "windows", "darwin": "mac"}.get(sys.platform, "linux") + "_pick"
    monkeypatch.setattr(folder_picker, impl, slow)
    results = []
    t = threading.Thread(target=lambda: results.append(folder_picker.pick_folder(Path("."))))
    t.start()
    assert inside.wait(5)
    with pytest.raises(folder_picker.PickerBusy):
        folder_picker.pick_folder(Path("."))
    gate.set()
    t.join(5)
    assert results == ["X"]
    monkeypatch.setattr(folder_picker, impl, lambda i, t: "Y")
    assert folder_picker.pick_folder(Path(".")) == "Y"  # free again


def test_downloads_folder_is_the_users():
    from source.utils.folder_picker import downloads_folder

    d = downloads_folder()
    assert d.is_absolute()
    if IS_WINDOWS:  # Windows' own record of it (also when the user moved it)
        assert d.is_dir()


def test_mac_picker_command_and_answers(tmp_path):
    from types import SimpleNamespace

    from source.utils import folder_picker as fp

    cmd = fp._mac_command(tmp_path, 'Chọn "phim"')
    assert cmd[:3] == ["osascript", "-e", "activate"]
    assert 'choose folder with prompt "Chọn \\"phim\\""' in cmd[4]
    assert f"default location (POSIX file {fp._applescript_text(str(tmp_path))})" in cmd[4]
    assert cmd[-1] == "POSIX path of chosen"
    assert "path to downloads folder" in fp._mac_command(tmp_path / "missing", "x")[4]
    ok = lambda *a, **k: SimpleNamespace(returncode=0, stdout="/Users/a/Phim hay/\n", stderr="")
    assert fp._mac_pick(tmp_path, "x", run=ok) == "/Users/a/Phim hay"
    cancel = lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="execution error: User canceled. (-128)")
    assert fp._mac_pick(tmp_path, "x", run=cancel) is None
    broken = lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="Not authorized (-1743)")
    with pytest.raises(fp.PickerUnavailable, match="1743"):
        fp._mac_pick(tmp_path, "x", run=broken)


def test_linux_without_desktop_is_unavailable(tmp_path, monkeypatch):
    from source.utils import folder_picker as fp

    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    with pytest.raises(fp.PickerUnavailable, match="desktop"):
        fp._linux_pick(tmp_path, "x")


@pytest.mark.skipif(not IS_WINDOWS or os.environ.get("HUB_TEST_REAL_DIALOG") != "1",
                    reason="opens a real dialog on the screen: set HUB_TEST_REAL_DIALOG=1")
def test_real_windows_picker_opens_in_downloads_and_cancels():
    """Opt-in: the real Explorer dialog shows (in front, in Downloads) and Cancel returns None."""
    import ctypes
    from ctypes import wintypes

    from source.utils import folder_picker as fp

    user32 = ctypes.WinDLL("user32")
    user32.FindWindowW.restype = wintypes.HWND
    result = []
    t = threading.Thread(target=lambda: result.append(fp.pick_folder()))
    t.start()
    try:
        hwnd = None
        for _ in range(100):
            hwnd = user32.FindWindowW("#32770", fp.TITLE)
            if hwnd:
                break
            time.sleep(0.1)
        assert hwnd, "the dialog did not open"
        time.sleep(0.5)
    finally:
        if hwnd:
            user32.PostMessageW(hwnd, 0x0111, 2, 0)  # Cancel
        t.join(10)
    assert result == [None]


def test_other_devices_still_use_the_library(tmp_path, lib):
    store = store_path_for(Path(os.environ["HUB_INDEX_PATH"]))
    LibraryFolders([], store=store, source="config").save([str(lib)])
    with client(tmp_path, address=REMOTE) as c:
        assert HubInfo.model_validate(c.get("/api/hub").json()) == HubInfo(version="0.1.0", is_server=False, folders=1)
        assert str(lib) not in c.get("/api/hub").text  # a count, never paths
        assert c.post("/api/rescan").status_code == 202
        wait_rescan(c)
        assert c.get("/api/library").json()["total"] == 6


# ======================================================================= admin API (on the server)


def test_full_flow_from_nothing(tmp_path, lib, other):
    with client(tmp_path) as c:
        assert c.get("/api/hub").json() == {"version": "0.1.0", "is_server": True, "folders": 0, "pin": False}
        start = AdminFolders.model_validate(c.get("/api/admin/libraries").json())
        assert (start.folders, start.source, start.editable) == ([], "config", True)
        assert c.post("/api/rescan").status_code == 409  # nothing to scan yet

        r = put(c, [lib])
        assert r.status_code == 200, r.text
        saved = AdminFolders.model_validate(r.json())
        assert [(f.path, f.exists, f.videos, f.problem) for f in saved.folders] == [(str(lib.resolve()), True, 0, None)]
        assert saved.source == "saved"
        assert c.get("/api/hub").json()["folders"] == 1

        assert c.post("/api/rescan").status_code == 202  # then the library page rescans
        assert wait_rescan(c)["counts"]["found"] == 6
        assert c.get("/api/library").json()["total"] == 6
        assert AdminFolders.model_validate(c.get("/api/admin/libraries").json()).folders[0].videos == 6

        # Add a second folder: applied at once, its videos come with the next rescan.
        assert put(c, [lib, other]).status_code == 200
        c.post("/api/rescan")
        wait_rescan(c)
        assert c.get("/api/library").json()["total"] == 7

    # "Restart" with the same command: the folders are still there.
    with client(tmp_path) as c:
        assert [f["path"] for f in c.get("/api/admin/libraries").json()["folders"]] == [
            str(lib.resolve()), str(other.resolve())]
        assert c.get("/api/library").json()["total"] == 7


def test_removing_a_folder_hides_its_videos_at_once(tmp_path, lib, other):
    with client(tmp_path) as c:
        put(c, [lib, other])
        c.post("/api/rescan")
        wait_rescan(c)
        gone = next(i for i in c.get("/api/library").json()["items"] if i["title"] == "Một phim")
        assert c.get(gone["stream_url"]).status_code == 200

        assert put(c, [lib]).status_code == 200
        page = LibraryPage.model_validate(c.get("/api/library").json())
        assert page.total == 6 and "Một phim" not in [i.title for i in page.items]  # no rescan needed
        for url in (gone["stream_url"], gone["thumbnail_url"], gone["detail_url"]):
            assert c.get(url).status_code == 404
        # The next rescan drops it from the index; the files on disk are untouched.
        c.post("/api/rescan")
        assert wait_rescan(c)["state"] == "done"
        with LibraryIndex(Path(os.environ["HUB_INDEX_PATH"])) as index:
            assert [r.path for r in index.roots()] == [str(lib.resolve())]
        assert (other / "Một phim.mp4").exists()


def test_invalid_paths_are_refused_with_reasons(tmp_path, lib):
    with client(tmp_path) as c:
        r = put(c, [lib, tmp_path / "không có", lib / "Phim bộ", "tương đối"])
        assert r.status_code == 422
        problems = r.json()["problems"]
        assert [p["message"].split(":")[0] for p in problems] == [
            "Không tìm thấy thư mục", "Cần đường dẫn đầy đủ, ví dụ D", "Nằm bên trong thư mục " + str(lib.resolve()).split(":")[0]]
        assert c.get("/api/admin/libraries").json()["folders"] == []  # nothing saved
        assert c.put("/api/admin/libraries", json={"paths": "C:\\"}).status_code == 422  # not a list


def test_cli_paths_make_the_list_read_only(tmp_path, lib, other):
    with client(tmp_path, cli_paths=[str(lib)]) as c:
        view = AdminFolders.model_validate(c.get("/api/admin/libraries").json())
        assert (view.source, view.editable, [f.path for f in view.folders]) == ("cli", False, [str(lib.resolve())])
        r = put(c, [other])
        assert r.status_code == 409 and "--path" in r.json()["detail"]


def test_unplugged_saved_folder_is_shown(tmp_path, lib):
    LibraryFolders([], store=store_path_for(Path(os.environ["HUB_INDEX_PATH"])), source="config").save([str(lib)])
    moved = lib.rename(lib.with_name("rút ổ"))
    with client(tmp_path) as c:
        f = AdminFolders.model_validate(c.get("/api/admin/libraries").json()).folders[0]
        assert (f.exists, f.problem) == (False, f"Không tìm thấy thư mục: {lib.resolve()}")
        assert put(c, [lib]).status_code == 200  # still in the list: saving it again is allowed
    moved.rename(lib)


def test_rescan_with_no_folder_keeps_the_index(tmp_path, lib):
    """Removing every folder must not be followed by an accidental 'scan of nothing'."""
    with client(tmp_path) as c:
        put(c, [lib])
        c.post("/api/rescan")
        wait_rescan(c)
        put(c, [])
        assert c.get("/api/library").json()["total"] == 0  # hidden at once
        assert c.post("/api/rescan").status_code == 409
    with LibraryIndex(Path(os.environ["HUB_INDEX_PATH"])) as index:
        assert len(index.items()) == 6  # still in the index: re-adding the folder is instant


# ======================================================================= in a browser


playwright_api = pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect, sync_playwright  # noqa: E402


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch(channel="msedge", headless=True)
        except Exception:
            try:
                b = p.chromium.launch(headless=True)
            except Exception as exc:
                pytest.skip(f"no browser for Playwright: {exc}")
        yield b
        b.close()


@contextlib.contextmanager
def live_hub(tmp_path):
    app = create_app(settings(tmp_path))
    sock = bind_socket("127.0.0.1", 0)
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        app.state.rescanner.wait(60)
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


@contextlib.contextmanager
def page_of(browser, viewport=None):
    ctx = browser.new_context(viewport=viewport or {"width": 1280, "height": 800})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: m.type == "error" and "Failed to load resource" not in m.text
            and errors.append(m.text))
    try:
        yield page
        assert errors == []
    finally:
        ctx.close()


def test_browser_flow_add_folder_then_scan(browser, tmp_path, lib):
    """The plan's flow: same command, empty hub → admin page → add folder → back → scan."""
    with live_hub(tmp_path) as base, page_of(browser) as page:
        page.goto(base + "/")
        expect(page.locator("#message h2")).to_have_text("Chưa có thư mục thư viện")
        expect(page.locator("#admin-link")).to_be_visible()  # this is the server
        page.get_by_role("button", name="Mở trang quản trị").click()
        expect(page).to_have_url(base + "/admin")
        expect(page.locator("#folders .empty")).to_be_visible()

        page.fill("#path", str(tmp_path / "không có"))
        page.get_by_role("button", name="Thêm").click()
        expect(page.locator("#errors")).to_contain_text("Không tìm thấy thư mục")

        page.fill("#path", f'"{lib}"')  # as pasted from Explorer's "Copy as path"
        page.get_by_role("button", name="Thêm").click()
        expect(page.locator(".folder-row .path")).to_have_text([str(lib.resolve())])
        expect(page.locator(".folder-row .state")).to_have_text(["Sẵn sàng"])
        expect(page.locator("#errors")).to_be_hidden()
        expect(page.locator("#saved")).to_be_visible()

        page.get_by_role("link", name="Về thư viện").last.click()
        expect(page.locator("#toast")).to_contain_text("Quét lại")
        expect(page.locator("#message h2")).to_have_text("Thư viện trống")  # folder set, not scanned
        page.locator("#rescan").click()
        # The page shows the folders once the whole rescan is done (details + thumbnails made
        # with ffmpeg): over 5 s on a busy computer (seen in step 6.2 with soak tests running).
        expect(page.locator(".folder .name")).to_have_text(["Hỏng", "Phim bộ", "Phim lẻ", "UPPERCASE EXT"],
                                                           timeout=30_000)

        # Remove it again (confirm dialog): the library page is empty at once.
        page.goto(base + "/admin")
        expect(page.locator(".count-badge")).to_have_text(["6 video"])
        dialogs = []
        page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
        page.get_by_role("button", name="Bỏ").click()
        expect(page.locator("#folders .empty")).to_be_visible()
        assert "6 video" in dialogs[0] and "không bị xoá" in dialogs[0]
        page.goto(base + "/")
        expect(page.locator("#message h2")).to_have_text("Chưa có thư mục thư viện")


def test_browser_add_with_the_folder_picker(browser, tmp_path, lib, monkeypatch):
    """Thêm with an empty box opens the system picker (faked here: no real dialog in tests);
    the chosen folder is added. Cancel adds nothing; no picker → a clear message."""
    from source.utils import folder_picker

    answers = iter([None, folder_picker.PickerUnavailable("no desktop"), str(lib)])

    def fake(initial=None, title=folder_picker.TITLE):
        time.sleep(0.3)  # the "waiting for you" line shows meanwhile
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(folder_picker, "pick_folder", fake)
    with live_hub(tmp_path) as base, page_of(browser) as page:
        page.goto(base + "/admin")
        expect(page.locator("#folders .empty")).to_contain_text("Bấm Thêm")
        add = page.get_by_role("button", name="Thêm")

        add.click()  # 1: cancelled
        expect(page.locator("#picking")).to_contain_text("hộp chọn thư mục")
        expect(page.locator("#picking")).to_be_hidden()
        expect(page.locator("#folders .empty")).to_be_visible()
        expect(page.locator("#errors")).to_be_hidden()

        add.click()  # 2: no picker on this computer
        expect(page.locator("#errors")).to_contain_text("dán đường dẫn")
        expect(page.locator("#folders .empty")).to_be_visible()

        add.click()  # 3: a folder chosen
        expect(page.locator(".folder-row .path")).to_have_text([str(lib.resolve())])
        expect(page.locator("#errors")).to_be_hidden()
        expect(page.locator("#toast")).to_contain_text("Đã thêm thư mục")
        expect(add).to_be_enabled()


def test_browser_other_device_sees_no_admin(browser, tmp_path, lib, monkeypatch):
    """A phone in the LAN: no admin button, /admin explains, the library works."""
    monkeypatch.setattr(local_only, "is_server_request", lambda scope: False)
    monkeypatch.setattr(app_module, "is_server_request", lambda scope: False)
    with live_hub(tmp_path) as base, page_of(browser, {"width": 390, "height": 844}) as page:
        page.goto(base + "/")
        expect(page.locator("#message h2")).to_have_text("Chưa có thư mục thư viện")
        expect(page.locator("#message")).to_contain_text("chỉ thêm được trên máy chủ")
        expect(page.locator("#message button")).to_have_count(0)
        expect(page.locator("#admin-link")).to_be_hidden()
        response = page.goto(base + "/admin")
        assert response.status == 403
        expect(page.locator("h2")).to_have_text("Trang quản trị chỉ mở được trên máy chủ")
