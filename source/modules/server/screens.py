"""TV screens (plan step 5.4): the smart TV shows the film, a phone is the remote.

The TV browsers tried (BrowseHere and TV Bro on a Sony Android TV) move a virtual mouse
cursor with the remote's arrows and never pass the keys to the page. So the hub is not driven
by the remote: the TV opens ``/tv`` once and becomes a *screen*, and phones (``/remote``)
send it commands.

- A screen has a public ``id`` (phones send commands with it), a secret ``key`` that only the
  TV page holds (to receive commands and report its state), and a short ``code`` that people
  can type.
- Commands reach the TV over one Server-Sent Events stream per screen (``EventSource`` exists
  in every TV browser). The TV reports its state with PUT. Opening a new stream ends the old
  one (a reloaded TV page).
- Everything lives in memory and is used from the event loop only (async endpoints). After a
  hub restart the TV registers again with its old id + key, so phones keep working.
- ``close()`` ends every stream at once: the hub calls it when it stops, otherwise uvicorn
  would wait for the TVs to hang up before exiting.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

from source.schemas.api import ScreenInfo, ScreenState

logger = logging.getLogger("house_cinema.server.screens")
security_log = logging.getLogger("house_cinema.server.security")

CODE_ALPHABET = "ACDEFHJKLMNPRTUVWXY3479"
"""No look-alikes on a TV across the room: no 0/O, 1/I, 2/Z, 5/S, 6/G, 8/B."""
CODE_LENGTH = 4
MAX_SCREENS = 20
FORGET_AFTER_SECONDS = 6 * 3600
"""A screen offline for this long is forgotten (the TV registers again when it comes back)."""
QUEUE_SIZE = 32
PING_SECONDS = 15
"""A comment line this often keeps the stream alive and shows when the TV is gone."""
TICK_SECONDS = 0.5
"""How often a stream checks whether it must end (hub stopping, newer stream)."""


class ScreenError(Exception):
    """Base of the errors the API turns into HTTP answers."""


class ScreenNotFound(ScreenError):
    pass


class WrongKey(ScreenError):
    pass


class ScreenOffline(ScreenError):
    pass


class TooManyScreens(ScreenError):
    def __init__(self, limit: int):
        super().__init__(f"at most {limit} screens")
        self.limit = limit


class CommandQueueFull(ScreenError):
    pass


@dataclass(eq=False)
class Stream:
    """One open /events connection of a TV page."""

    queue: asyncio.Queue
    client: str
    closed: bool = False

    def end(self) -> None:
        self.closed = True
        try:
            self.queue.put_nowait(None)  # wakes the stream up at once
        except asyncio.QueueFull:
            pass  # it sees `closed` at its next tick


@dataclass(eq=False)
class Screen:
    id: str
    key: str
    code: str
    name: str
    last_seen: float
    state: ScreenState = field(default_factory=ScreenState)
    state_at: float | None = None
    stream: Stream | None = None

    @property
    def online(self) -> bool:
        return self.stream is not None and not self.stream.closed


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


class ScreenRegistry:
    def __init__(self, *, max_screens: int = MAX_SCREENS, forget_after: float = FORGET_AFTER_SECONDS,
                 ping_seconds: float = PING_SECONDS, clock: Callable[[], float] = time.monotonic):
        self.max_screens = max_screens
        self.forget_after = forget_after
        self.ping_seconds = ping_seconds
        self.clock = clock
        self.closing = False
        self._screens: dict[str, Screen] = {}

    # ------------------------------------------------------------------ screens

    def register(self, screen_id: str | None, key: str | None, name: str, client: str) -> Screen:
        """The TV page's screen: the same one again when ``screen_id`` + ``key`` match (reload),
        or the old id + key restored after a hub restart; otherwise a new screen."""
        now = self.clock()
        self._forget_old(now)
        if screen_id and key:
            known = self._screens.get(screen_id)
            if known is not None and hmac.compare_digest(known.key.encode(), key.encode()):
                known.name, known.last_seen = name, now
                logger.info("Screen %s (%s, %s) is back from %s", known.code, known.name, known.id, client)
                return known
            if known is None:  # hub restarted: keep the id phones remember
                return self._add(screen_id, key, name, client, now)
            security_log.warning("Screen %s: registration from %s with a wrong key; a new screen is made",
                                 screen_id, client)
        return self._add(secrets.token_urlsafe(12), secrets.token_urlsafe(24), name, client, now)

    def _add(self, screen_id: str, key: str, name: str, client: str, now: float) -> Screen:
        if len(self._screens) >= self.max_screens:
            raise TooManyScreens(self.max_screens)
        screen = Screen(id=screen_id, key=key, code=self._new_code(), name=name, last_seen=now)
        self._screens[screen_id] = screen
        logger.info("Screen %s (%s, %s) registered from %s", screen.code, name, screen_id, client)
        return screen

    def _new_code(self) -> str:
        used = {s.code for s in self._screens.values()}
        while True:
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            if code not in used:
                return code

    def _forget_old(self, now: float) -> None:
        for sid, s in list(self._screens.items()):
            if not s.online and now - s.last_seen > self.forget_after:
                del self._screens[sid]
                logger.info("Screen %s (%s) forgotten: offline for a long time", s.code, s.name)

    def get(self, screen_id: str) -> Screen:
        screen = self._screens.get(screen_id)
        if screen is None:
            raise ScreenNotFound(screen_id)
        return screen

    def screens(self) -> list[Screen]:
        self._forget_old(self.clock())
        return sorted(self._screens.values(), key=lambda s: (not s.online, s.name.casefold(), s.code))

    def authorize(self, screen_id: str, key: str | None, client: str) -> Screen:
        """The screen, for its own TV page only (the right key)."""
        screen = self.get(screen_id)
        if not key or not hmac.compare_digest(screen.key.encode(), key.encode()):
            security_log.warning("Screen %s: wrong key from %s", screen.code, client)
            raise WrongKey(screen_id)
        return screen

    def info(self, screen: Screen) -> ScreenInfo:
        age = None if screen.state_at is None else round(self.clock() - screen.state_at, 1)
        return ScreenInfo(id=screen.id, code=screen.code, name=screen.name, online=screen.online,
                          state=screen.state, state_age=age)

    # ------------------------------------------------------------------ TV side

    def open_stream(self, screen_id: str, key: str | None, client: str) -> tuple[Screen, Stream]:
        screen = self.authorize(screen_id, key, client)
        if screen.stream is not None:
            screen.stream.end()  # a reloaded page: the newest connection wins
        stream = Stream(queue=asyncio.Queue(maxsize=QUEUE_SIZE), client=client)
        screen.stream = stream
        screen.last_seen = self.clock()
        logger.info("Screen %s (%s) connected from %s", screen.code, screen.name, client)
        return screen, stream

    def close_stream(self, screen: Screen, stream: Stream) -> None:
        stream.closed = True
        if screen.stream is stream:
            screen.stream = None
            screen.last_seen = self.clock()
            logger.info("Screen %s (%s) disconnected", screen.code, screen.name)

    def set_state(self, screen_id: str, key: str | None, state: ScreenState, client: str) -> None:
        screen = self.authorize(screen_id, key, client)
        screen.state = state
        screen.state_at = screen.last_seen = self.clock()

    async def events(self, screen: Screen, stream: Stream) -> AsyncIterator[str]:
        """The text of the TV's event stream: a hello, then one event per command, with pings."""
        try:
            yield "retry: 3000\n\n"
            yield sse("hello", {"id": screen.id, "code": screen.code, "name": screen.name})
            last = self.clock()
            while not stream.closed and not self.closing:
                try:
                    command = await asyncio.wait_for(stream.queue.get(), timeout=TICK_SECONDS)
                except asyncio.TimeoutError:
                    if self.clock() - last >= self.ping_seconds:
                        last = self.clock()
                        yield ": ping\n\n"
                    continue
                if command is None:
                    break
                last = self.clock()
                yield sse("command", command)
        finally:
            self.close_stream(screen, stream)

    # ------------------------------------------------------------------ phone side

    def send(self, screen_id: str, command: dict, client: str) -> Screen:
        screen = self.get(screen_id)
        if not screen.online:
            raise ScreenOffline(screen_id)
        try:
            screen.stream.queue.put_nowait(command)
        except asyncio.QueueFull as exc:
            raise CommandQueueFull(screen_id) from exc
        logger.info("Screen %s (%s): %s from %s", screen.code, screen.name, command.get("action"), client)
        return screen

    # ------------------------------------------------------------------ hub

    def close(self) -> None:
        """End every stream now (the hub is stopping)."""
        self.closing = True
        for screen in self._screens.values():
            if screen.stream is not None:
                screen.stream.end()
