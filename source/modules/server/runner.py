"""Bind the listening socket ourselves, then hand it to uvicorn.

Binding before uvicorn lets us:
- hold the port exclusively on Windows (SO_EXCLUSIVEADDRUSE), so a second process
  cannot silently share it;
- turn bind failures into one readable line instead of a traceback.
"""

from __future__ import annotations

import errno
import logging
import socket
import sys
import threading
from collections.abc import Callable

import uvicorn

from source.modules.server import network
from source.modules.server.app import create_app
from source.schemas.settings import Settings
from source.utils.ffmpeg import INSTALL_HINT, disabled_features, find_tools

logger = logging.getLogger("house_cinema.server")

# Windows Winsock codes alongside their POSIX equivalents.
_IN_USE = {errno.EADDRINUSE, 10048}
_ACCESS = {errno.EACCES, 10013}
_NOT_AVAILABLE = {errno.EADDRNOTAVAIL, 10049}


class ServerStartError(Exception):
    """The server could not start; the message is meant for the user."""


def _error_code(exc: OSError) -> int | None:
    return getattr(exc, "winerror", None) or exc.errno


def bind_socket(host: str, port: int) -> socket.socket:
    """Create a bound, listening TCP socket or raise ServerStartError."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
    except socket.gaierror as exc:
        raise ServerStartError(f"cannot resolve host '{host}': {exc.strerror}") from None
    family, socktype, proto, _, address = infos[0]

    sock = socket.socket(family, socktype, proto)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:  # POSIX: allow quick restart after TIME_WAIT; still refuses a live listener
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            # IPv6 socket serves IPv6 only, so it can sit next to the IPv4 one.
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        sock.bind(address)
        sock.listen(2048)
    except OSError as exc:
        sock.close()
        code = _error_code(exc)
        if code in _IN_USE:
            msg = (f"port {port} is already in use on {host} (is another hub or app running?). "
                   f"Stop it or start with --port <other port>.")
        elif code in _ACCESS:
            msg = (f"not allowed to listen on port {port}. Windows may reserve it "
                   f"(check: netsh interface ipv4 show excludedportrange protocol=tcp). "
                   f"Try --port <other port>.")
        elif code in _NOT_AVAILABLE:
            msg = f"host '{host}' is not an address of this machine. Use 0.0.0.0 to listen on all networks."
        else:
            msg = f"cannot listen on {host}:{port}: {exc.strerror or exc}"
        raise ServerStartError(msg) from None
    sock.set_inheritable(True)
    return sock


def announce_addresses(port: int, out=None) -> network.Addresses:
    """Print the address phones should open, with a QR code (plan step 5.3); log warnings
    (no LAN at all, only VPN…). The Windows "Public network" check runs in the background."""
    out = out or sys.stdout
    found = network.find_addresses()
    for a in found.best:
        logger.info("LAN address: http://%s:%s/ (%s %s)", a.ip, port, a.kind, a.name)
    for w in found.warnings:
        logger.warning("%s", w)
    try:
        text = network.startup_text(found, port, qr=True)
        if text:
            out.write(text + "\n")
            out.flush()
    except UnicodeEncodeError:  # a console that cannot show the QR blocks (or Vietnamese)
        for url in found.urls(port):
            out.write(f"Open on a phone on the same network: {url}\n")
        out.flush()
    except Exception as exc:  # never stop the hub for a QR code
        logger.warning("Cannot show the QR code: %s", exc)

    def check_profile() -> None:
        for w in network.public_profile_warnings(found):
            logger.warning("%s", w)

    threading.Thread(target=check_profile, name="network-profile", daemon=True).start()
    return found


def warn_missing_tools(settings: Settings) -> list[str]:
    """Say at start which features are off because ffmpeg/ffprobe is missing (plan step 6.1).
    Returns the features (empty when nothing is missing)."""
    tools = find_tools(settings)
    features = disabled_features(tools)
    if features:
        logger.warning("Không tìm thấy %s. Hub vẫn chạy, nhưng các tính năng sau bị tắt:\n%s\n%s",
                       " và ".join(tools.missing), "\n".join(f"  - {f}" for f in features), INSTALL_HINT)
    return features


class HubServer(uvicorn.Server):
    """uvicorn, plus: the TV event streams (plan step 5.4) end as soon as the hub stops.
    uvicorn waits for open requests before exiting, and a TV's stream never ends by itself,
    so without this Ctrl+C would hang while a TV shows /tv."""

    async def shutdown(self, sockets=None) -> None:
        screens = getattr(getattr(self.config.app, "state", None), "screens", None)
        if screens is not None:
            screens.close()
        await super().shutdown(sockets=sockets)


def run_server(settings: Settings, on_server: Callable[[HubServer], None] | None = None) -> None:
    """Start the HTTP server and block until it stops (Ctrl+C).

    ``on_server`` (the desktop app) receives the server before it runs: setting its
    ``should_exit`` stops the hub from another thread.

    Raises ServerStartError if the socket cannot be bound.
    """
    app = create_app(settings)  # opens the library index first: fail before taking the port
    warn_missing_tools(settings)
    try:
        sock = bind_socket(settings.host, settings.port)
    except ServerStartError:
        app.state.catalog.close()
        raise
    bound_host, bound_port = sock.getsockname()[:2]
    sockets = [sock]

    logger.info("Listening on http://%s:%s", bound_host, bound_port)
    if bound_host == "0.0.0.0" and socket.has_ipv6:
        # Windows resolves "localhost" to ::1 first; without an IPv6 listener some
        # clients stall ~2 s before falling back to 127.0.0.1. Best effort only.
        try:
            sockets.append(bind_socket("::", bound_port))
            logger.info("Listening on http://[::]:%s", bound_port)
        except ServerStartError as exc:
            logger.warning("IPv6 listener not started, IPv4 only: %s", exc)
    if bound_host in ("0.0.0.0", "::"):
        logger.info("Library page on this computer: http://localhost:%s/", bound_port)
        logger.info("Admin page (library folders, address and QR code for phones), on this computer only: "
                    "http://localhost:%s/admin", bound_port)
        announce_addresses(bound_port)
    else:
        logger.info("Listening only on %s: other devices can reach it only if that is a LAN address", bound_host)

    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_config=None,  # use our logging setup; uvicorn loggers propagate to root
        log_level=settings.log_level.lower(),
        access_log=True,
        server_header=False,
        timeout_graceful_shutdown=10,  # a last resort; HubServer already ends the TV streams
    )
    server = HubServer(config)
    if on_server is not None:
        on_server(server)
    try:
        server.run(sockets=sockets)
    finally:
        for s in sockets:
            s.close()
    logger.info("Server stopped")
