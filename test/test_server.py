"""Tests for plan step 1.2: HTTP server on the LAN."""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from source import pipeline
from source.modules.server import ServerStartError, bind_socket, create_app
from source.utils.config import load_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LIBRARY = PROJECT_ROOT / "test" / "fixtures" / "library"
HUB_ENV_KEYS = ("APP_ENV", "LOG_LEVEL", "HUB_HOST", "HUB_PORT", "HUB_LIBRARY_PATHS")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in HUB_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def no_env_file(tmp_path) -> Path:
    return tmp_path / "missing.env"


@pytest.fixture
def settings(no_env_file, _isolated_index):
    # environ={} would fall back to the real data/library.db: keep the test index.
    return load_settings(environ={"HUB_INDEX_PATH": str(_isolated_index)}, env_file=no_env_file,
                         cli_paths=[str(LIBRARY)])


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------------- the app


def test_health_returns_ok(settings):
    client = TestClient(create_app(settings))
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "version": "0.1.0"}


def test_unknown_route_is_404(settings):
    assert TestClient(create_app(settings)).get("/nope").status_code == 404


# -------------------------------------------------------------- bind_socket


def test_bind_socket_listens():
    sock = bind_socket("127.0.0.1", 0)
    try:
        port = sock.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
    finally:
        sock.close()


def test_bind_socket_port_in_use():
    first = bind_socket("0.0.0.0", 0)
    port = first.getsockname()[1]
    try:
        with pytest.raises(ServerStartError, match=f"port {port} is already in use"):
            bind_socket("0.0.0.0", port)
    finally:
        first.close()


def test_bind_socket_port_in_use_by_other_app():
    """A plain socket from another app (no exclusive flag) must also be detected."""
    other = socket.socket()
    other.bind(("0.0.0.0", 0))
    other.listen()
    port = other.getsockname()[1]
    try:
        with pytest.raises(ServerStartError, match="already in use"):
            bind_socket("0.0.0.0", port)
    finally:
        other.close()


def test_bind_socket_foreign_host():
    # 203.0.113.0/24 is TEST-NET-3: never an address of this machine.
    with pytest.raises(ServerStartError, match="not an address of this machine"):
        bind_socket("203.0.113.5", free_port())


def test_bind_socket_unresolvable_host():
    with pytest.raises(ServerStartError, match="cannot resolve host"):
        bind_socket("no-such-host.invalid", free_port())


# ------------------------------------------------------------------ pipeline


def test_main_port_in_use_exits_with_server_error(capsys, no_env_file):
    blocker = bind_socket("0.0.0.0", 0)
    port = blocker.getsockname()[1]
    try:
        code = pipeline.main(
            ["--path", str(LIBRARY), "--env-file", str(no_env_file), "--port", str(port)]
        )
    finally:
        blocker.close()
    err = capsys.readouterr().err
    assert code == pipeline.EXIT_SERVER_ERROR
    assert f"Server error: port {port} is already in use" in err
    assert "Traceback" not in err


@pytest.mark.parametrize("level", ["DEBUG", "WARNING"])
def test_main_applies_log_level(monkeypatch, no_env_file, level):
    seen = {}
    monkeypatch.setattr(pipeline, "run_server", lambda s: seen.setdefault("settings", s))
    monkeypatch.setenv("LOG_LEVEL", level)
    code = pipeline.main(["--path", str(LIBRARY), "--env-file", str(no_env_file)])
    assert code == pipeline.EXIT_OK
    assert seen["settings"].log_level == level
    assert logging.getLogger().level == getattr(logging, level)


# ---------------------------------------------------------------- end to end


def _start(port: int, *extra: str) -> subprocess.Popen[str]:
    env = {k: v for k, v in os.environ.items() if k not in HUB_ENV_KEYS}
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.Popen(
        [sys.executable, "-m", "source.pipeline", "--path", str(LIBRARY),
         "--env-file", str(PROJECT_ROOT / "missing.env"), "--port", str(port), *extra],
        cwd=PROJECT_ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8",
    )


def _wait_healthy(url: str, proc: subprocess.Popen[str], timeout: float = 20) -> httpx.Response:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"server exited early ({proc.returncode}): {proc.stderr.read()}")
        try:
            return httpx.get(url, timeout=1)
        except httpx.TransportError:
            time.sleep(0.2)
    pytest.fail("server did not become healthy in time")


def test_e2e_server_and_second_instance_on_same_port():
    port = free_port()
    server = _start(port)
    try:
        resp = _wait_healthy(f"http://127.0.0.1:{port}/health", server)
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "version": "0.1.0"}
        if socket.has_ipv6:
            # "localhost" resolves to ::1 first on Windows; it must answer, not stall.
            v6 = httpx.get(f"http://[::1]:{port}/health", timeout=1)
            assert v6.status_code == 200

        second = _start(port)
        out, err = second.communicate(timeout=30)
        assert second.returncode == pipeline.EXIT_SERVER_ERROR
        assert f"port {port} is already in use" in err
        assert "Traceback" not in err

        # The first server is still serving after the failed second start.
        assert httpx.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code == 200
    finally:
        server.kill()
        _, server_log = server.communicate(timeout=10)
    assert f"Listening on http://0.0.0.0:{port}" in server_log
