"""Serve a video file with HTTP Range support (RFC 9110 §14), so players can seek.

- ``Range: bytes=a-b``, ``bytes=a-`` and ``bytes=-n`` (last n bytes) → ``206 Partial Content``.
- No Range → ``200`` with the whole file (still ``Accept-Ranges: bytes``).
- A malformed or unsatisfiable byte range → ``416`` with ``Content-Range: bytes */<size>``.
- Ranges in another unit, several ranges at once (``bytes=0-1,5-6``) and an ``If-Range``
  that no longer matches the file are ignored: the whole file is sent (allowed by the RFC;
  browsers and VLC only ever ask for one range).
- The file is read in chunks in a worker thread and closed as soon as the response ends,
  fails, or the client goes away (closed tab, seek that aborts the previous request).
"""

from __future__ import annotations

import logging
import re
import threading
from email.utils import formatdate
from typing import BinaryIO
from urllib.parse import quote

import anyio
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from source.modules.server.catalog import StreamSource

logger = logging.getLogger("house_cinema.server.stream")

CHUNK_SIZE = 256 * 1024
_SPEC = re.compile(r"^\s*(\d*)\s*-\s*(\d*)\s*$")

_open_lock = threading.Lock()
_open_files = 0


def open_file_count() -> int:
    """Video files the server currently holds open (for tests and diagnostics)."""
    with _open_lock:
        return _open_files


class RangeNotSatisfiable(Exception):
    pass


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """The (first, last) byte to send, both inclusive; None = send the whole file.

    Raises RangeNotSatisfiable for a byte range that is malformed or outside the file.
    """
    if header is None or not header.strip():
        return None
    unit, sep, ranges = header.partition("=")
    if not sep or unit.strip().lower() != "bytes":
        return None  # unknown unit: ignore the header
    specs = ranges.split(",")
    if len(specs) > 1:
        return None  # multipart/byteranges is not supported: send everything
    match = _SPEC.match(specs[0])
    if not match or match.group(1) == match.group(2) == "":
        raise RangeNotSatisfiable(f"malformed range {header!r}")
    first, last = match.groups()
    if first:
        start = int(first)
        end = int(last) if last else size - 1
        if last and end < start:
            raise RangeNotSatisfiable(f"range end before start in {header!r}")
        if start >= size:
            raise RangeNotSatisfiable(f"range starts at {start}, file has {size} bytes")
        return start, min(end, size - 1)
    suffix = int(last)
    if suffix == 0 or size == 0:
        raise RangeNotSatisfiable(f"empty suffix range {header!r} (file has {size} bytes)")
    return max(0, size - suffix), size - 1


def etag_for(source: StreamSource) -> str:
    return f'"{source.size:x}-{source.mtime_ns:x}"'


def last_modified_for(source: StreamSource) -> str:
    return formatdate(source.mtime_ns / 1e9, usegmt=True)


def _content_disposition(name: str) -> str:
    ascii_name = name.encode("ascii", "replace").decode().replace('"', "'").replace("?", "_")
    return f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


class RangeFileResponse(Response):
    """Response for one video file. Subclasses Starlette's Response only so FastAPI passes
    it through untouched; sending is done by ``__call__`` below."""

    def __init__(self, source: StreamSource, range_header: str | None, if_range: str | None,
                 method: str, client: str = "?"):
        self.background = None
        self.source = source
        self.head_only = method == "HEAD"
        self.client = client
        size = source.size
        etag, modified = etag_for(source), last_modified_for(source)
        self.header_list: list[tuple[str, str]] = [
            ("accept-ranges", "bytes"),
            ("etag", etag),
            ("last-modified", modified),
            ("content-disposition", _content_disposition(source.name)),
        ]
        if if_range is not None and if_range.strip() not in (etag, modified):
            range_header = None  # the file changed since the client got its first part
        try:
            span = parse_range(range_header, size)
        except RangeNotSatisfiable as exc:
            logger.info("416 for %s from %s: %s", source.name, client, exc)
            self.status_code, self.start, self.length = 416, 0, 0
            self.body = b"Requested range not satisfiable\n"
            self.header_list += [("content-range", f"bytes */{size}"),
                                 ("content-type", "text/plain; charset=utf-8"),
                                 ("content-length", str(len(self.body)))]
        else:
            self.body = b""
            if span is None:
                self.status_code, self.start, self.length = 200, 0, size
            else:
                self.status_code, self.start, self.length = 206, span[0], span[1] - span[0] + 1
                self.header_list.append(("content-range", f"bytes {span[0]}-{span[1]}/{size}"))
            self.header_list += [("content-type", source.content_type),
                                 ("content-length", str(self.length))]
        self.raw_headers = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in self.header_list]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
        if self.head_only or self.status_code == 416 or self.length == 0:
            await send({"type": "http.response.body", "body": b"" if self.head_only else self.body})
            return

        # uvicorn silently drops data sent after the client left, so without this watcher the
        # rest of a multi-GB file would still be read from disk for nothing.
        async with anyio.create_task_group() as tg:
            async def stream() -> None:
                try:
                    await self._send_file(send)
                finally:
                    tg.cancel_scope.cancel()

            async def watch_disconnect() -> None:
                while (await receive())["type"] != "http.disconnect":
                    pass
                tg.cancel_scope.cancel()

            tg.start_soon(stream)
            tg.start_soon(watch_disconnect)

    async def _send_file(self, send: Send) -> None:
        global _open_files
        # Opening can be slow on a sleeping USB disk; shield it so a cancel cannot drop an
        # already opened file on the floor. The `finally` below always closes it.
        with anyio.CancelScope(shield=True):
            f: BinaryIO = await anyio.to_thread.run_sync(open, self.source.path, "rb")
        with _open_lock:
            _open_files += 1
        remaining = self.length
        try:
            if self.start:
                await anyio.to_thread.run_sync(f.seek, self.start)
            while remaining > 0:
                chunk = await anyio.to_thread.run_sync(f.read, min(CHUNK_SIZE, remaining))
                if not chunk:  # the file got shorter while we were sending it
                    logger.warning("%s ended %d bytes early (file changed while streaming)",
                                   self.source.name, remaining)
                    break
                remaining -= len(chunk)
                await send({"type": "http.response.body", "body": chunk, "more_body": remaining > 0})
        except OSError as exc:  # client connection reset, or the drive was unplugged
            logger.info("Streaming %s to %s stopped: %s", self.source.name, self.client, exc)
        finally:
            f.close()
            with _open_lock:
                _open_files -= 1
            # Seeking in a player aborts the previous request, so partial sends are normal.
            logger.debug("Sent %d of %d bytes of %s to %s", self.length - remaining, self.length,
                         self.source.name, self.client)
