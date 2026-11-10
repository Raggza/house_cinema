"""Reject requests whose URL tries to walk the file system (plan step 3.2).

The API only takes media IDs, so a URL with '..', a backslash, a drive letter or a NUL byte
is never legitimate. Such requests get the same generic 404 as any unknown URL (nothing
tells the client what exists), and a warning with the client IP is logged.

Checked on the raw URL, encoded and decoded, also double-encoded ('%252e%252e'), because the
router only sees the decoded path and `%2F` turns into '/', which hides the attempt from it.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import unquote

from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("house_cinema.server.security")

_TRAVERSAL = re.compile(
    r"(^|[/\\])\.\.([/\\]|$)"  # .. as a path segment
    r"|\\"  # backslash: Windows path separator
    r"|^/?[a-zA-Z]:|/[a-zA-Z]:[/\\]"  # drive letter: C: or /C:/
    r"|\x00"  # NUL byte
    r"|^//"  # UNC-like //server/share
)

NOT_FOUND = b'{"detail":"Not Found"}'


def suspicious_path(raw: str) -> bool:
    """True if ``raw`` (a URL path, possibly %-encoded several times) tries path traversal."""
    seen = raw
    for _ in range(3):  # raw, decoded, double-decoded
        if _TRAVERSAL.search(seen):
            return True
        decoded = unquote(seen)
        if decoded == seen:
            break
        seen = decoded
    return False


def client_of(scope: Scope) -> str:
    client = scope.get("client")
    return client[0] if client else "?"


class PathGuard:
    """ASGI middleware: 404 + warning for path traversal attempts, before routing."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            raw = scope.get("raw_path", b"").decode("latin-1") or scope.get("path", "")
            query = scope.get("query_string", b"").decode("latin-1")
            if suspicious_path(raw) or suspicious_path(scope.get("path", "")):
                logger.warning("Blocked path traversal attempt from %s: %s %s%s", client_of(scope),
                               scope.get("method"), raw[:200], f"?{query[:100]}" if query else "")
                await send({"type": "http.response.start", "status": 404,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"content-length", str(len(NOT_FOUND)).encode())]})
                await send({"type": "http.response.body", "body": NOT_FOUND})
                return
        await self.app(scope, receive, send)
