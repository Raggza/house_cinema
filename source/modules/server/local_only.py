"""Is this request made on the server computer itself? (admin page, plan step 4.2)

Only the address of the TCP connection counts, never a header (``X-Forwarded-For``,
``Host``…), which any client can write. A request is from the server when its source IP is:
- a loopback address (``127.0.0.1``, ``::1``), or
- the very address it connected to (opening ``http://<LAN-IP>:8000`` on the server itself:
  the source and destination are then the same address of this machine), or
- any address of this computer's own network adapters.
A remote device cannot open a TCP connection with one of our own addresses as its source.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import time

from starlette.types import Scope

logger = logging.getLogger("house_cinema.server.security")

IP = ipaddress.IPv4Address | ipaddress.IPv6Address
_CACHE_SECONDS = 30.0
_cache: tuple[float, frozenset[IP]] = (0.0, frozenset())


def parse_ip(text: str | None) -> IP | None:
    if not text:
        return None
    try:
        ip = ipaddress.ip_address(text.split("%", 1)[0])  # drop an IPv6 zone ("fe80::1%12")
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return ip.ipv4_mapped  # "::ffff:192.168.1.10" is that IPv4 address
    return ip


def local_addresses() -> frozenset[IP]:
    """Addresses of this computer's network adapters (refreshed every 30 s: Wi-Fi changes)."""
    global _cache
    now = time.monotonic()
    if now - _cache[0] < _CACHE_SECONDS:
        return _cache[1]
    found: set[IP] = set()
    host = socket.gethostname()
    try:
        found.update(filter(None, (parse_ip(a) for a in socket.gethostbyname_ex(host)[2])))
    except OSError:
        pass
    try:
        found.update(filter(None, (parse_ip(info[4][0]) for info in socket.getaddrinfo(host, None))))
    except OSError:
        pass
    _cache = (now, frozenset(found))
    return _cache[1]


ADMIN_PREFIXES = ("/admin", "/api/admin")
_DENIED_JSON = b'{"detail":"The admin page can only be used on the server computer."}'


def is_admin_path(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in ADMIN_PREFIXES)


class ServerOnlyAdmin:
    """ASGI middleware: /admin and /api/admin/* answer only requests from the server
    computer. Others get 403 (an explanation page, or JSON for the API) and a warning is
    logged with their IP. Checked before routing, so every admin route is covered, also
    ones added later."""

    def __init__(self, app, denied_page: bytes):
        self.app = app
        self.denied_page = denied_page

    async def __call__(self, scope: Scope, receive, send) -> None:
        if scope["type"] == "http" and is_admin_path(scope.get("path", "")) and not is_server_request(scope):
            client = scope.get("client")
            logger.warning("Blocked admin access from %s (not the server computer): %s %s",
                           client[0] if client else "?", scope.get("method"), scope.get("path", "")[:200])
            api = scope.get("path", "").startswith("/api/")
            body = _DENIED_JSON if api else self.denied_page
            ctype = b"application/json" if api else b"text/html; charset=utf-8"
            await send({"type": "http.response.start", "status": 403,
                        "headers": [(b"content-type", ctype), (b"content-length", str(len(body)).encode()),
                                    (b"cache-control", b"no-store")]})
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def is_server_request(scope: Scope) -> bool:
    client = scope.get("client")
    ip = parse_ip(client[0] if client else None)
    if ip is None:
        return False
    if ip.is_loopback:
        return True
    server = scope.get("server")
    if server and parse_ip(server[0]) == ip:
        return True
    return ip in local_addresses()
