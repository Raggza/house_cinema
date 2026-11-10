"""Tests for plan step 3.2: nothing outside the library folders can be served."""

from __future__ import annotations

import logging
import os
import socket
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from source.modules.library import LibraryIndex, scan_library
from source.modules.server import bind_socket, create_app
from source.modules.server.guard import suspicious_path
from source.utils.config import load_settings

EXTS = (".mp4",)
IS_WINDOWS = sys.platform == "win32"
INSIDE = b"inside the library " * 50
SECRET = b"SECRET outside the library " * 50


# ======================================================================= setup


def link_dir(link: Path, target: Path) -> None:
    """Directory symlink, or a junction on Windows without symlink privilege."""
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        if not IS_WINDOWS:
            pytest.skip("cannot create directory symlink")
        import _winapi

        _winapi.CreateJunction(str(target), str(link))


@pytest.fixture
def outside(tmp_path) -> Path:
    """A folder next to the library, with a 'secret' file named like a library file."""
    folder = tmp_path / "outside"
    folder.mkdir()
    (folder / "Một.mp4").write_bytes(SECRET)
    (folder / "win.ini").write_bytes(SECRET)
    return folder


@pytest.fixture
def lib(tmp_path) -> Path:
    root = tmp_path / "lib"
    (root / "Phim").mkdir(parents=True)
    (root / "Phim" / "Một.mp4").write_bytes(INSIDE)
    (root / "Khác").mkdir()
    (root / "Khác" / "Một.mp4").write_bytes(INSIDE + b"other")
    return root


@pytest.fixture
def ids(lib, _isolated_index) -> dict[str, str]:
    with LibraryIndex(_isolated_index) as index:
        index.apply_scan(scan_library([lib], EXTS))
        return {i.relative_path: i.id for i in index.items()}


def make_client(index_path: Path, roots: list[Path], tmp_path: Path) -> TestClient:
    settings = load_settings(environ={"HUB_INDEX_PATH": str(index_path)},
                             env_file=tmp_path / "none.env", cli_paths=[str(r) for r in roots])
    return TestClient(create_app(settings))


@pytest.fixture
def client(lib, ids, _isolated_index, tmp_path):
    with make_client(_isolated_index, [lib], tmp_path) as c:
        yield c


@pytest.fixture
def warnings(caplog):
    caplog.set_level(logging.INFO, logger="house_cinema")
    return lambda: [r for r in caplog.records
                    if r.levelno == logging.WARNING and r.name == "house_cinema.server.security"]


def assert_refused(r, warnings, client_ip="testclient"):
    assert r.status_code == 404
    assert SECRET[:20] not in r.content and INSIDE[:20] not in r.content
    assert "lib" not in r.text and "outside" not in r.text and ":\\" not in r.text
    logged = warnings()
    assert logged, "no security warning was logged"
    assert client_ip in logged[-1].getMessage()


# ============================================================= guard: URL patterns


@pytest.mark.parametrize("path", [
    "/stream/../../windows/win.ini",
    "/stream/..%2F..%2Fwindows%2Fwin.ini",
    "/stream/%2e%2e%2f%2e%2e%2fwindows%2fwin.ini",
    "/stream/%2E%2E/win.ini",
    "/stream/%252e%252e%252fwindows%252fwin.ini",  # double-encoded
    "/stream/..\\..\\windows\\win.ini",
    "/stream/..%5C..%5Cwin.ini",
    "/stream/C:%5CWindows%5Cwin.ini",
    "/stream/C:/Windows/win.ini",
    "/C:/Windows/win.ini",
    "/stream/abc%00.mp4",
    "//server/share/movie.mp4",
    "/health/..",
])
def test_suspicious_path(path):
    assert suspicious_path(path)


@pytest.mark.parametrize("path", [
    "/stream/0123456789abcdef", "/health", "/stream/a..b", "/stream/...", "/docs",
    "/stream/0123456789abcdef?x=..",  # (query strings are not checked)
])
def test_normal_path(path):
    assert not suspicious_path(path.split("?")[0])


# ========================================================= through the app (encoded)


@pytest.mark.parametrize("url", [
    "/stream/..%2F..%2Fwindows%2Fwin.ini",
    "/stream/%2e%2e%2f%2e%2e%2foutside%2fwin.ini",
    "/stream/%252e%252e%252foutside%252fwin.ini",
    "/stream/..%5C..%5Coutside%5Cwin.ini",
    "/stream/C:%5CWindows%5Cwin.ini",
    "/stream/abc%00def",
    "/health/..%2F..%2Fstream%2F0123456789abcdef",
])
def test_encoded_traversal_is_refused(client, warnings, url):
    assert_refused(client.get(url), warnings)


def test_absolute_path_instead_of_id_is_refused(client, warnings, lib, outside):
    for target in (outside / "win.ini", lib / "Phim" / "Một.mp4"):
        # As the API only takes IDs, even a real file inside the library is refused by path.
        url = "/stream/" + str(target).replace("\\", "/")
        assert_refused(client.get(url), warnings)
    assert_refused(client.get("/stream/Phim/Một.mp4"), warnings)


@pytest.mark.parametrize("bad_id", ["0123456789ABCDEF", "0123456789abcde", "0123456789abcdef0",
                                    "zzzzzzzzzzzzzzzz", "%20123456789abcdef"])
def test_malformed_id_is_refused(client, warnings, bad_id):
    assert_refused(client.get(f"/stream/{bad_id}"), warnings)


def test_legit_requests_still_work_and_log_no_warning(client, warnings, ids):
    r = client.get(f"/stream/{ids['Phim/Một.mp4']}", headers={"Range": "bytes=0-9"})
    assert (r.status_code, r.content) == (206, INSIDE[:10])
    assert client.get("/health").status_code == 200
    assert client.get("/stream/0123456789abcdef").status_code == 404  # unknown ID: not a probe
    assert warnings() == []


# ================================================================= links on disk


def test_folder_swapped_for_link_outside_is_refused(client, warnings, ids, lib, outside):
    (lib / "Phim").rename(lib / "Phim-cũ")
    link_dir(lib / "Phim", outside)  # lib/Phim/Một.mp4 now opens outside/Một.mp4
    assert_refused(client.get(f"/stream/{ids['Phim/Một.mp4']}"), warnings)
    assert "outside the library" in warnings()[-1].getMessage()


def test_folder_swapped_for_link_inside_is_refused(client, warnings, ids, lib):
    # Inside the library, but still a link: the scanner never follows links, nor does /stream.
    (lib / "Phim").rename(lib / "Phim-cũ")
    link_dir(lib / "Phim", lib / "Khác")
    assert_refused(client.get(f"/stream/{ids['Phim/Một.mp4']}"), warnings)
    assert "through a link" in warnings()[-1].getMessage()


def test_file_swapped_for_symlink_is_refused(client, warnings, ids, lib, outside):
    target = lib / "Phim" / "Một.mp4"
    target.unlink()
    try:
        os.symlink(outside / "Một.mp4", target)
    except OSError:
        pytest.skip("no privilege to create file symlinks (Windows without Developer Mode)")
    assert_refused(client.get(f"/stream/{ids['Phim/Một.mp4']}"), warnings)


def test_library_folder_given_as_link_works(tmp_path, outside, _isolated_index):
    """A library folder configured through a link (e.g. a junction to another drive) is fine:
    links *inside* the library are what is refused."""
    real = tmp_path / "real-lib"
    real.mkdir()
    (real / "a.mp4").write_bytes(INSIDE)
    linked = tmp_path / "linked-lib"
    link_dir(linked, real)
    with LibraryIndex(_isolated_index) as index:
        index.apply_scan(scan_library([linked], EXTS))
        media_id = index.items()[0].id
    with make_client(_isolated_index, [linked], tmp_path) as c:
        assert c.get(f"/stream/{media_id}").content == INSIDE


# ======================================================= index vs. current config


def test_folder_removed_from_config_is_refused(lib, ids, _isolated_index, tmp_path, warnings):
    other = tmp_path / "other-lib"
    other.mkdir()
    # The index still lists lib/ (until the next scan); the config now only has other-lib/.
    with make_client(_isolated_index, [other], tmp_path) as c:
        assert_refused(c.get(f"/stream/{ids['Phim/Một.mp4']}"), warnings)
    assert "not in the current config" in warnings()[-1].getMessage()


@pytest.mark.parametrize("tampered", ["../outside/win.ini", "Phim/../../outside/win.ini",
                                      "C:/Windows/win.ini", "/etc/passwd", "Phim\\..\\..\\outside\\win.ini"])
def test_tampered_index_path_is_refused(client, warnings, ids, _isolated_index, outside, tampered):
    with sqlite3.connect(_isolated_index) as conn:
        conn.execute("UPDATE media SET relative_path = ? WHERE id = ?", (tampered, ids["Phim/Một.mp4"]))
    assert_refused(client.get(f"/stream/{ids['Phim/Một.mp4']}"), warnings)


# ============================================================ real server, raw HTTP


@pytest.fixture
def live_port(lib, ids, _isolated_index, tmp_path):
    settings = load_settings(environ={"HUB_INDEX_PATH": str(_isolated_index)},
                             env_file=tmp_path / "none.env", cli_paths=[str(lib)])
    sock = bind_socket("127.0.0.1", 0)
    server = uvicorn.Server(uvicorn.Config(create_app(settings), log_config=None, lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        yield sock.getsockname()[1]
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


def raw_get(port: int, target: str) -> bytes:
    """Send the request line exactly as given (like `curl --path-as-is`)."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
        conn.sendall(f"GET {target} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode("latin-1"))
        data = b""
        while chunk := conn.recv(65536):
            data += chunk
    return data


@pytest.mark.parametrize("target", [
    "/stream/../../outside/win.ini",
    "/stream/..\\..\\outside\\win.ini",
    "/stream/..%2F..%2Foutside%2Fwin.ini",
    "/../outside/win.ini",
    "/stream/C:\\Windows\\win.ini",
])
def test_raw_traversal_on_real_server(live_port, warnings, target):
    response = raw_get(live_port, target)
    status = response.split(b"\r\n", 1)[0]
    assert status in (b"HTTP/1.1 404 Not Found", b"HTTP/1.1 400 Bad Request"), status
    assert SECRET[:20] not in response
    if status.endswith(b"404 Not Found"):
        assert "127.0.0.1" in warnings()[-1].getMessage()
