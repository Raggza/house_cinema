"""Tests for plan step 3.1: /stream/{id} with HTTP Range."""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import unicodedata
from pathlib import Path
from urllib.parse import quote

import pytest
import uvicorn
from fastapi.testclient import TestClient

from source.modules.library import LibraryIndex, scan_library
from source.modules.server import bind_socket, create_app
from source.modules.server.catalog import MediaCatalog, content_type_for
from source.modules.server.streaming import RangeNotSatisfiable, open_file_count, parse_range
from source.utils.config import load_settings

EXTS = (".mp4", ".mkv", ".webm")
SIZE = 1000
DATA = bytes(i % 251 for i in range(SIZE))  # no repeating 256-byte pattern: offsets are checked
NFD_NAME = unicodedata.normalize("NFD", "[23]【神ボディ】② Tập 1.mp4")


# ================================================================ parse_range


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("", None),
        ("bytes=0-99", (0, 99)),
        ("bytes=0-0", (0, 0)),
        ("bytes=900-", (900, 999)),
        ("bytes=990-5000", (990, 999)),  # end past the file: clipped
        ("bytes=-100", (900, 999)),  # last 100 bytes
        ("bytes=-5000", (0, 999)),  # suffix longer than the file: whole file
        ("BYTES = 10 - 19", (10, 19)),
        ("items=0-5", None),  # unknown unit: ignored
        ("bytes=0-1,5-6", None),  # several ranges: ignored, whole file
    ],
)
def test_parse_range(header, expected):
    assert parse_range(header, SIZE) == expected


@pytest.mark.parametrize("header", ["bytes=1000-", "bytes=5000-6000", "bytes=5-1", "bytes=abc",
                                    "bytes=-0", "bytes=-", "bytes=", "bytes=1-2-3", "bytes=0x10-"])
def test_parse_range_not_satisfiable(header):
    with pytest.raises(RangeNotSatisfiable):
        parse_range(header, SIZE)


def test_parse_range_empty_file():
    assert parse_range(None, 0) is None
    for header in ("bytes=0-", "bytes=-1"):
        with pytest.raises(RangeNotSatisfiable):
            parse_range(header, 0)


@pytest.mark.parametrize(("ext", "ctype"), [(".mp4", "video/mp4"), (".MKV", "video/x-matroska"),
                                            (".webm", "video/webm"), (".mov", "video/quicktime")])
def test_content_type(ext, ctype):
    assert content_type_for(ext) == ctype


# ================================================================ the endpoint


@pytest.fixture
def lib(tmp_path) -> Path:
    root = tmp_path / "lib"
    (root / "Phim").mkdir(parents=True)
    (root / "Phim" / "Một.mp4").write_bytes(DATA)
    (root / "Phim" / "Rỗng.webm").write_bytes(b"")
    (root / NFD_NAME).write_bytes(DATA[::-1])
    return root


@pytest.fixture
def ids(lib, _isolated_index) -> dict[str, str]:
    with LibraryIndex(_isolated_index) as index:
        index.apply_scan(scan_library([lib], EXTS))
        return {i.relative_path: i.id for i in index.items()}


@pytest.fixture
def client(lib, ids, _isolated_index, tmp_path):
    settings = load_settings(environ={"HUB_INDEX_PATH": str(_isolated_index)},
                             env_file=tmp_path / "none.env", cli_paths=[str(lib)])
    with TestClient(create_app(settings)) as c:  # `with`: runs startup/shutdown (closes the index)
        yield c
    assert open_file_count() == 0


def test_whole_file(client, ids):
    r = client.get(f"/stream/{ids['Phim/Một.mp4']}")
    assert r.status_code == 200
    assert r.content == DATA
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-type"] == "video/mp4"
    assert r.headers["content-length"] == str(SIZE)
    assert "content-range" not in r.headers


@pytest.mark.parametrize(
    ("header", "start", "end"),
    [("bytes=0-1023", 0, 999), ("bytes=0-99", 0, 99), ("bytes=500-", 500, 999),
     ("bytes=-10", 990, 999), ("bytes=123-123", 123, 123)],
)
def test_range(client, ids, header, start, end):
    r = client.get(f"/stream/{ids['Phim/Một.mp4']}", headers={"Range": header})
    assert r.status_code == 206
    assert r.content == DATA[start:end + 1]
    assert r.headers["content-range"] == f"bytes {start}-{end}/{SIZE}"
    assert r.headers["content-length"] == str(end - start + 1)
    assert r.headers["accept-ranges"] == "bytes"
    assert r.headers["content-type"] == "video/mp4"


@pytest.mark.parametrize("header", ["bytes=1000-", "bytes=5-1", "bytes=abc", "bytes=-0"])
def test_bad_range_is_416(client, ids, header):
    r = client.get(f"/stream/{ids['Phim/Một.mp4']}", headers={"Range": header})
    assert r.status_code == 416
    assert r.headers["content-range"] == f"bytes */{SIZE}"
    assert DATA[:20] not in r.content


def test_ignored_ranges_send_whole_file(client, ids):
    for header in ("items=0-5", "bytes=0-1,5-6"):
        r = client.get(f"/stream/{ids['Phim/Một.mp4']}", headers={"Range": header})
        assert (r.status_code, r.content) == (200, DATA)


def test_head(client, ids):
    r = client.head(f"/stream/{ids['Phim/Một.mp4']}", headers={"Range": "bytes=0-1023"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 0-999/{SIZE}"
    assert r.headers["content-length"] == str(SIZE)
    assert r.content == b""


def test_if_range(client, ids):
    url = f"/stream/{ids['Phim/Một.mp4']}"
    etag = client.head(url).headers["etag"]
    same = client.get(url, headers={"Range": "bytes=0-9", "If-Range": etag})
    assert (same.status_code, same.content) == (206, DATA[:10])
    changed = client.get(url, headers={"Range": "bytes=0-9", "If-Range": '"old-version"'})
    assert (changed.status_code, changed.content) == (200, DATA)


def test_empty_file(client, ids):
    url = f"/stream/{ids['Phim/Rỗng.webm']}"
    r = client.get(url)
    assert (r.status_code, r.content, r.headers["content-type"]) == (200, b"", "video/webm")
    assert client.get(url, headers={"Range": "bytes=0-"}).status_code == 416


def test_nfd_file_name(client, ids):
    r = client.get(f"/stream/{ids[unicodedata.normalize('NFC', NFD_NAME)]}", headers={"Range": "bytes=0-9"})
    assert (r.status_code, r.content) == (206, DATA[::-1][:10])
    # The name offered to the player is the NFC one, as shown everywhere else.
    nfc = quote(unicodedata.normalize("NFC", NFD_NAME), safe="")
    assert r.headers["content-disposition"].endswith(f"filename*=UTF-8''{nfc}")


@pytest.mark.parametrize("media_id", ["0123456789abcdef", "nope", "..%2F..%2Fwindows%2Fwin.ini",
                                      "0123456789ABCDEF0", "%00"])
def test_unknown_id_is_404(client, media_id, lib):
    r = client.get(f"/stream/{media_id}")
    assert r.status_code == 404
    assert str(lib) not in r.text and "lib" not in r.text


def test_file_deleted_after_scan_is_404(client, ids, lib):
    (lib / "Phim" / "Một.mp4").unlink()
    r = client.get(f"/stream/{ids['Phim/Một.mp4']}")
    assert r.status_code == 404
    assert "Một" not in r.text


def test_disconnected_folder_is_404(client, ids, lib, _isolated_index):
    lib.rename(lib.with_name("unplugged"))
    with LibraryIndex(_isolated_index) as index:
        index.apply_scan(scan_library([lib], EXTS, known_files=index.known_files()))
    assert client.get(f"/stream/{ids['Phim/Một.mp4']}").status_code == 404


def test_file_changed_since_scan_uses_disk_size(client, ids, lib):
    (lib / "Phim" / "Một.mp4").write_bytes(DATA + DATA)
    r = client.get(f"/stream/{ids['Phim/Một.mp4']}", headers={"Range": "bytes=-10"})
    assert r.headers["content-range"] == f"bytes {2 * SIZE - 10}-{2 * SIZE - 1}/{2 * SIZE}"


def test_index_is_not_written_by_the_server(lib, ids, _isolated_index):
    before = _isolated_index.stat().st_mtime_ns
    catalog = MediaCatalog(_isolated_index, [lib])
    catalog.source(ids["Phim/Một.mp4"])
    catalog.close()
    assert _isolated_index.stat().st_mtime_ns == before


# ======================================================= real server: disconnects


@pytest.fixture
def live_server(lib, ids, _isolated_index, tmp_path):
    big = lib / "Phim" / "Lớn.mp4"
    with open(big, "wb") as f:  # 64 MB: far more than the socket buffers can hold
        f.write(os.urandom(1024 * 1024) * 64)
    with LibraryIndex(_isolated_index) as index:
        index.apply_scan(scan_library([lib], EXTS, known_files=index.known_files()))
        big_id = next(i.id for i in index.items() if i.name == "Lớn.mp4")

    settings = load_settings(environ={"HUB_INDEX_PATH": str(_isolated_index)},
                             env_file=tmp_path / "none.env", cli_paths=[str(lib)])
    sock = bind_socket("127.0.0.1", 0)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(settings), log_config=None, lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    try:
        yield port, big_id, big
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


def _wait_closed(timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if open_file_count() == 0:
            return True
        time.sleep(0.05)
    return False


@pytest.mark.parametrize("request_range", [None, "bytes=33554432-"])
def test_client_disconnect_releases_the_file(live_server, request_range, caplog):
    caplog.set_level(logging.DEBUG, logger="house_cinema.server.stream")
    port, big_id, big = live_server
    extra = f"Range: {request_range}\r\n" if request_range else ""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
        conn.sendall(f"GET /stream/{big_id} HTTP/1.1\r\nHost: x\r\n{extra}\r\n".encode())
        received = b""
        while len(received) < 512 * 1024:
            received += conn.recv(65536)
        assert received.startswith(b"HTTP/1.1 206" if request_range else b"HTTP/1.1 200")
        assert open_file_count() == 1  # still streaming: the client has not read it all
    # Socket closed mid-stream, like a closed browser tab.
    assert _wait_closed(), "the server still holds the video file open"
    # It also stopped reading: the rest of the file was not read from disk for nothing.
    sent = [r for r in caplog.records if r.getMessage().startswith("Sent ")]
    assert len(sent) == 1
    sent_bytes, total = (int(x) for x in sent[0].getMessage().split()[1:4:2])
    assert sent_bytes < total // 2, sent[0].getMessage()
    # On Windows an open file cannot be renamed: this proves the handle is really gone.
    big.rename(big.with_name("renamed.mp4"))


def test_live_range_request(live_server):
    import httpx

    port, big_id, _ = live_server
    r = httpx.get(f"http://127.0.0.1:{port}/stream/{big_id}", headers={"Range": "bytes=0-1023"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 0-1023/{64 * 1024 * 1024}"
    assert len(r.content) == 1024
    assert _wait_closed()
