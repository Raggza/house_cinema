"""Who may use the hub (plan step 5.2).

1. **LAN only** (``access_lan_only``): a request whose source address is public (Internet) is
   refused with 403, whatever it asks for. Allowed: loopback, private ranges (10/8,
   172.16/12, 192.168/16), link-local, IPv6 unique-local, and 100.64/10 (carrier-grade NAT /
   Tailscale-style VPNs). Like the admin check of 4.2, only the TCP source address counts,
   never a header.
2. **PIN** (``access_pin``, optional): every device except the server computer types it once
   on ``/pin``. A right PIN sets a signed cookie (remembered ``pin_remember_days``); changing
   the PIN logs every device out. After ``pin_max_attempts`` wrong PINs in a row a device
   (its IP) must wait ``pin_lockout_minutes``. The PIN never opens the admin page: that one
   stays "server computer only" (4.2), checked after this.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import quote

from starlette.types import ASGIApp, Receive, Scope, Send

from source.modules.server.local_only import is_admin_path, is_server_request, parse_ip

logger = logging.getLogger("house_cinema.server.security")

COOKIE = "hub_pin"
# Listed one by one: Python's is_private also says True for documentation and reserved
# ranges (e.g. 203.0.113.0/24), which are not a LAN.
LAN_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
    "100.64.0.0/10",  # carrier-grade NAT, used by VPNs such as Tailscale
    "::1/128", "fe80::/10", "fc00::/7",
))
# Reachable without the PIN: the PIN page itself and what it needs.
OPEN_PATHS = ("/pin", "/api/pin", "/health", "/favicon.ico")
OPEN_PREFIXES = ("/static/",)
# Pages (HTML): sent to the PIN page. Anything else (API, video, pieces): 401 JSON.
PAGE_PREFIXES = ("/watch/", "/diagnostics", "/demo/", "/docs", "/redoc", "/tv", "/remote")


def is_lan_address(text: str | None) -> bool | None:
    """True/False for an IP address; None when the client is not an IP (test client, socket)."""
    ip = parse_ip(text)
    if ip is None:
        return None
    return any(ip.version == net.version and ip in net for net in LAN_NETWORKS)


def _client(scope: Scope) -> str:
    client = scope.get("client")
    return client[0] if client else "?"


async def _send(send: Send, status: int, body: bytes, ctype: bytes, extra: list | None = None) -> None:
    headers = [(b"content-type", ctype), (b"content-length", str(len(body)).encode()), (b"cache-control", b"no-store")]
    await send({"type": "http.response.start", "status": status, "headers": headers + (extra or [])})
    await send({"type": "http.response.body", "body": body})


class LanOnly:
    """ASGI middleware: refuse devices with a public IP address (403)."""

    def __init__(self, app: ASGIApp, enabled: bool = True):
        self.app = app
        self.enabled = enabled

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.enabled and scope["type"] in ("http", "websocket") and is_lan_address(_client(scope)) is False:
            logger.warning("Blocked request from public address %s: %s %s", _client(scope), scope.get("method"),
                           scope.get("path", "")[:200])
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await _send(send, 403, '{"detail":"Hub chỉ mở cho thiết bị trong mạng nội bộ (LAN)."}'.encode(),
                        b"application/json")
            return
        await self.app(scope, receive, send)


@dataclass
class _Failures:
    count: int = 0
    locked_until: float = 0.0


class PinGuard:
    """Checks PINs, issues and verifies the cookie, and counts wrong attempts per IP."""

    def __init__(self, pin: str, secret_file: Path, *, max_attempts: int, lockout_seconds: int,
                 remember_seconds: int):
        self.pin = pin
        self.max_attempts = max_attempts
        self.lockout_seconds = lockout_seconds
        self.remember_seconds = remember_seconds
        self._secret = self._load_secret(secret_file)
        # The PIN is part of the key: a new PIN invalidates every cookie made with the old one.
        self._key = hmac.new(self._secret, b"pin:" + pin.encode("utf-8"), hashlib.sha256).digest()
        self._lock = threading.Lock()
        self._failures: dict[str, _Failures] = {}

    @staticmethod
    def _load_secret(path: Path) -> bytes:
        """A random key kept in data/ so that cookies survive a restart of the hub."""
        try:
            data = bytes.fromhex(path.read_text(encoding="ascii").strip())
            if len(data) >= 32:
                return data
        except (OSError, ValueError):
            pass
        data = secrets.token_bytes(32)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(data.hex(), encoding="ascii")
            os.replace(tmp, path)
        except OSError as exc:  # still works; devices just type the PIN again after a restart
            logger.warning("Cannot save %s (%s): devices will need the PIN again after a restart", path, exc)
        return data

    # ------------------------------------------------------------------ cookie

    def make_token(self, now: float | None = None) -> str:
        issued = str(int(now if now is not None else time.time()))
        mac = hmac.new(self._key, issued.encode(), hashlib.sha256).digest()[:18]
        return f"{issued}.{base64.urlsafe_b64encode(mac).decode()}"

    def token_ok(self, token: str | None, now: float | None = None) -> bool:
        if not token or "." not in token:
            return False
        issued, _, mac = token.partition(".")
        if not issued.isdigit():
            return False
        expected = base64.urlsafe_b64encode(hmac.new(self._key, issued.encode(), hashlib.sha256).digest()[:18]).decode()
        if not hmac.compare_digest(mac, expected):
            return False
        age = (now if now is not None else time.time()) - int(issued)
        return -300 <= age <= self.remember_seconds

    def cookie_header(self) -> str:
        return (f"{COOKIE}={self.make_token()}; Max-Age={self.remember_seconds}; Path=/; HttpOnly; "
                "SameSite=Lax")

    # ------------------------------------------------------------------ attempts

    def locked_for(self, ip: str, now: float | None = None) -> int:
        """Seconds this IP must still wait (0 = may try)."""
        now = time.monotonic() if now is None else now
        with self._lock:
            f = self._failures.get(ip)
            return max(0, int(f.locked_until - now + 0.999)) if f else 0

    def check(self, ip: str, typed: str, now: float | None = None) -> tuple[bool, int, int]:
        """(right, attempts left, seconds locked). A locked IP is refused without checking."""
        now = time.monotonic() if now is None else now
        with self._lock:
            f = self._failures.setdefault(ip, _Failures())
            if f.locked_until > now:
                return False, 0, int(f.locked_until - now + 0.999)
            if f.locked_until:  # the lockout is over: a fresh start
                f.count, f.locked_until = 0, 0.0
            right = hmac.compare_digest(typed.encode("utf-8"), self.pin.encode("utf-8"))
            if right:
                self._failures.pop(ip, None)
                return True, self.max_attempts, 0
            f.count += 1
            if f.count >= self.max_attempts:
                f.locked_until = now + self.lockout_seconds
                logger.warning("PIN: %d wrong attempts from %s, locked for %d s", f.count, ip, self.lockout_seconds)
                return False, 0, self.lockout_seconds
            logger.info("PIN: wrong attempt %d/%d from %s", f.count, self.max_attempts, ip)
            return False, self.max_attempts - f.count, 0


def cookie_from(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == b"cookie":
            jar = SimpleCookie()
            try:
                jar.load(value.decode("latin-1"))
            except Exception:
                return None
            if COOKIE in jar:
                return jar[COOKIE].value
    return None


class PinGate:
    """ASGI middleware: without a valid PIN cookie, pages go to /pin and the rest gets 401.
    The server computer and the admin paths (server only anyway, 4.2) are not asked."""

    def __init__(self, app: ASGIApp, guard: PinGuard):
        self.app = app
        self.guard = guard

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._free(scope):
            await self.app(scope, receive, send)
            return
        if self.guard.token_ok(cookie_from(scope)):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == "/" or path.startswith(PAGE_PREFIXES):
            query = scope.get("query_string", b"").decode("latin-1")
            target = path + (f"?{query}" if query else "")
            await _send(send, 303, b"", b"text/plain", [(b"location", f"/pin?next={quote(target, safe='')}".encode())])
            return
        await _send(send, 401, b'{"detail":"PIN required","pin_url":"/pin"}', b"application/json")

    @staticmethod
    def _free(scope: Scope) -> bool:
        path = scope.get("path", "")
        if path in OPEN_PATHS or path.startswith(OPEN_PREFIXES) or is_admin_path(path):
            return True
        return is_server_request(scope)
