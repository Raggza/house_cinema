"""FastAPI application factory."""

from __future__ import annotations

import logging
import os
import re
import time
from collections import Counter

import anyio
from pydantic import ValidationError
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import Body, FastAPI, Query, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from source.image.thumbnails import DEFAULT_THUMBNAIL, thumb_path
from source.modules.library.folders import FolderError, FoldersLocked, LibraryFolders, store_path_for
from source.modules.library.scanner import natural_key
from source.modules.server.catalog import (
    Library,
    MediaCatalog,
    MediaNotFound,
    StreamSource,
    _key,
    content_type_for,
)
from source.modules.server.diagnostics import MAX_REPORT_BYTES, SUBTITLE_FILE, SampleLibrary, save_report
from source.modules.server import network
from source.modules.server.access import LanOnly, PinGate, PinGuard
from source.modules.server.guard import PathGuard
from source.modules.server.local_only import ServerOnlyAdmin, is_server_request
from source.modules.server.rescan import Rescanner
from source.modules.server.screens import (
    CommandQueueFull,
    ScreenError,
    ScreenNotFound,
    ScreenOffline,
    ScreenRegistry,
    TooManyScreens,
    WrongKey,
)
from source.modules.server.streaming import RangeFileResponse
from source.modules.subtitles import SubtitleError, find_subtitles, load_as_vtt
from source.modules.transcode import Busy, CannotConvert, ClientCaps, ConvertFailed, SessionGone, TranscodeManager, decide
from source.modules.transcode.plan import EIGHT_BIT_420
from source.modules.transcode.sessions import REMUX_VARIANTS, SESSION_ID
from source.schemas.api import (
    AdminFolder,
    AdminFolders,
    AdminFoldersRejected,
    AdminFoldersUpdate,
    CmdPlayList,
    CmdPlayMedia,
    Conversion,
    Conversions,
    DeviceReport,
    DiagnosticSample,
    DiagnosticSamples,
    FolderInfo,
    FolderList,
    FolderProblem,
    HubInfo,
    LibraryPage,
    LibraryRef,
    MediaDetail,
    MediaSummary,
    NetworkAddress,
    NetworkInfo,
    PickedFolder,
    PinIn,
    PlayPlan,
    PlayRequest,
    RescanStatus,
    ScreenCommand,
    ScreenInfo,
    ScreenList,
    ScreenRegister,
    ScreenRegistered,
    ScreenState,
    ToolsInfo,
)
from source.schemas.health import HealthResponse
from source.schemas.media import MediaItem
from source.schemas.settings import Settings
from source.utils import folder_picker
from source.utils.ffmpeg import INSTALL_HINT, disabled_features, find_tools
from source.schemas.subtitle import SubtitleTrackOut

logger = logging.getLogger("house_cinema.server")
security_log = logging.getLogger("house_cinema.server.security")

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEMO_PAGE = PROJECT_ROOT / "demo" / "subtitles.html"
WEB_DIR = Path(__file__).resolve().parents[2] / "web"
MAX_PAGE = 200
NOT_FOUND = {404: {"description": "Not found (unknown ID, or not available)",
                   "content": {"application/json": {"example": {"detail": "Not Found"}}}}}
HLS_FILE = re.compile(r"index\.m3u8|init\.mp4|seg\d{5}\.m4s")
HLS_WAIT_SECONDS = 60
"""How long a request for a piece that is being made waits before giving up."""
M3U8 = "application/vnd.apple.mpegurl"


def _can_open(path: Path) -> bool:
    """A piece ffmpeg has just renamed into place can refuse to open for a few milliseconds on
    Windows (PermissionError). Serving it then sent a 200 with a cut-off body: the player got
    a broken piece and the hub logged a traceback (found by the soak test of step 6.2)."""
    try:
        with open(path, "rb"):
            return True
    except (PermissionError, FileNotFoundError):  # held, or deleted just now: ask the manager again
        return False
    except OSError:
        return True  # other trouble: let FileResponse answer it as before


def busy_response(exc: Busy) -> JSONResponse:
    return JSONResponse({"detail": f"Hub đang bận: đang chuyển đổi {exc.active} video cùng lúc (tối đa "
                                   f"{exc.limit}). Thử lại sau ít phút, hoặc mở video bằng VLC.",
                         "active": exc.active, "limit": exc.limit},
                        status_code=503, headers={"Retry-After": "30"})


def plays_directly(info, caps: ClientCaps) -> bool:
    """The file is an MP4 whose picture and sound this browser decodes: no conversion."""
    if info is None or info.container != "mp4" or info.video_codec not in caps.video:
        return False
    if info.video_codec == "h264" and (info.pix_fmt or "yuv420p") not in EIGHT_BIT_420:
        return False
    return info.audio_codec is None or info.audio_codec in caps.audio


def not_found() -> JSONResponse:
    """The one 404 answer, the same as for an unknown URL: nothing tells a client whether a
    video is missing, outside the library, or was never there."""
    return JSONResponse({"detail": "Not Found"}, status_code=404)


def _ref(lib: Library) -> LibraryRef:
    return LibraryRef(id=lib.id, name=lib.name, available=lib.available)


def _summary_fields(item: MediaItem, lib: Library) -> dict:
    info = item.info
    return dict(
        id=item.id, title=item.title, name=item.name, library=_ref(lib), folder=item.folder,
        extension=item.extension, size=item.size,
        modified=datetime.fromtimestamp(item.mtime, tz=timezone.utc),
        duration=info.duration if info else None, width=info.width if info else None,
        height=info.height if info else None, available=item.available,
        browser_playable=info.browser_playable if info else None,
        thumbnail_url=f"/thumbnails/{item.id}.jpg", stream_url=f"/stream/{item.id}",
        detail_url=f"/api/media/{item.id}",
    )


def _add_diagnostics(app: FastAPI, settings: Settings) -> None:
    """Device test page (plan step 4.4); not in production. Any LAN device may use it: it
    only serves its own sample videos and stores small, size-limited JSON reports."""
    samples = SampleLibrary(settings.cache_dir, find_tools(settings))
    reports_dir = settings.index_path.parent / "device-reports"

    @app.get("/diagnostics", include_in_schema=False)
    def diagnostics_page():
        return FileResponse(WEB_DIR / "diagnostics.html", media_type="text/html; charset=utf-8",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/api/diagnostics/samples", response_model=DiagnosticSamples, tags=["diagnostics"])
    def diagnostics_samples() -> DiagnosticSamples:
        """Sample videos, one per format (made with ffmpeg on first call: a few seconds)."""
        ready = samples.ensure()
        return DiagnosticSamples(
            samples=[DiagnosticSample(key=s.key, label=s.label, mime=s.mime, url=f"/diagnostics/samples/{s.file}",
                                      seconds=s.seconds, must_play=s.must_play) for s in ready],
            subtitles_url=f"/diagnostics/samples/{SUBTITLE_FILE}", missing=dict(samples.failed))

    @app.api_route("/diagnostics/samples/{file}", methods=["GET", "HEAD"], include_in_schema=False)
    def diagnostics_sample(file: str, request: Request):
        path = samples.path(file)
        if path is None or not path.is_file():
            return not_found()
        if file == SUBTITLE_FILE:
            return FileResponse(path, media_type="text/vtt; charset=utf-8")
        st = path.stat()
        source = StreamSource(path=path, size=st.st_size, mtime_ns=st.st_mtime_ns, name=file,
                              content_type=content_type_for(path.suffix))
        return RangeFileResponse(source, request.headers.get("range"), request.headers.get("if-range"),
                                 request.method, request.client.host if request.client else "?")

    @app.post("/api/diagnostics/reports", tags=["diagnostics"], status_code=201,
              responses={413: {"description": "Report too large"}})
    async def diagnostics_report(request: Request):
        """Store one device's test results (data/device-reports/)."""
        body = await request.body()  # read ourselves: a size limit before parsing
        if len(body) > MAX_REPORT_BYTES:
            return JSONResponse({"detail": "Report too large"}, status_code=413)
        try:
            report = DeviceReport.model_validate_json(body)
        except ValidationError as exc:
            return JSONResponse({"detail": "Invalid report", "errors": exc.errors(include_url=False,
                                                                                   include_context=False)},
                                status_code=422)
        client = request.client.host if request.client else "?"
        name = await anyio.to_thread.run_sync(save_report, reports_dir, report.model_dump(), client)
        return {"saved": name}


SCREEN_ERRORS: dict[type, tuple[int, str]] = {
    ScreenNotFound: (404, "Không tìm thấy TV này. Mở lại trang /tv trên TV rồi chọn lại."),
    WrongKey: (403, "Sai khoá màn hình."),
    ScreenOffline: (409, "TV đang mất kết nối với hub. Kiểm tra trang /tv trên TV còn mở và TV còn mạng."),
    CommandQueueFull: (503, "TV chưa kịp nhận các lệnh trước. Thử lại sau giây lát."),
}
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def _add_screens(app: FastAPI, settings: Settings, catalog: MediaCatalog) -> None:
    """Smart TV as a screen, phone as the remote (plan step 5.4). Same access rules as the
    library (LAN only, PIN): pages /tv and /remote, API /api/screens/*."""
    screens = ScreenRegistry()
    app.state.screens = screens

    def client_of(request: Request) -> str:
        return request.client.host if request.client else "?"

    def failed(exc: ScreenError) -> JSONResponse:
        if isinstance(exc, TooManyScreens):
            return JSONResponse({"detail": f"Đang có quá nhiều màn hình TV (tối đa {exc.limit}). Đóng trang /tv "
                                           "trên các TV không dùng rồi thử lại."}, status_code=503,
                                headers={"Retry-After": "60"})
        status, detail = SCREEN_ERRORS[type(exc)]
        return JSONResponse({"detail": detail}, status_code=status)

    async def remote_base(request: Request) -> str:
        """The address phones should open: the one the TV used, unless that is this computer's
        own name (localhost), which a phone cannot reach: then the LAN address of step 5.3."""
        base = str(request.base_url)
        if (request.url.hostname or "") in LOOPBACK_HOSTS:
            found = await anyio.to_thread.run_sync(network.find_addresses)
            urls = found.urls(request.url.port or settings.port)
            if urls:
                base = urls[0]
        return base.rstrip("/")

    def page(name: str) -> FileResponse:
        return FileResponse(WEB_DIR / name, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})

    @app.get("/tv", include_in_schema=False)
    def tv_page():
        """Open this on the smart TV: it waits for a phone, then plays fullscreen."""
        return page("tv.html")

    @app.get("/remote", include_in_schema=False)
    def remote_page():
        """The phone remote: pick a TV, then play, pause, seek, subtitles, volume."""
        return page("remote.html")

    @app.post("/api/screens", response_model=ScreenRegistered, status_code=201, tags=["screens"],
              responses={503: {"description": "Too many screens"}})
    async def screen_register(body: ScreenRegister, request: Request):
        """The TV page registers (again). Keep `id` and `key` and send them next time: the
        screen stays the same, so phones need not pick the TV again."""
        try:
            screen = screens.register(body.id, body.key, body.name, client_of(request))
        except TooManyScreens as exc:
            return failed(exc)
        url = f"{await remote_base(request)}/remote?screen={screen.id}"
        return ScreenRegistered(**screens.info(screen).model_dump(), key=screen.key, remote_url=url,
                                qr_svg=network.qr_svg_data_uri(url))

    @app.get("/api/screens", response_model=ScreenList, tags=["screens"])
    async def screen_list() -> ScreenList:
        """TVs that opened /tv, connected ones first."""
        return ScreenList(screens=[screens.info(s) for s in screens.screens()])

    @app.get("/api/screens/{screen_id}", response_model=ScreenInfo, tags=["screens"], responses=NOT_FOUND)
    async def screen_get(screen_id: str):
        """One TV and what it is playing (the remote asks every second)."""
        try:
            return screens.info(screens.get(screen_id))
        except ScreenError as exc:
            return failed(exc)

    @app.get("/api/screens/{screen_id}/events", tags=["screens"],
             responses={200: {"content": {"text/event-stream": {}}}, 403: {"description": "Wrong key"}})
    async def screen_events(screen_id: str, request: Request, key: str = Query("", max_length=100)):
        """Commands for the TV page (Server-Sent Events), with its `key`. Opening it again
        closes the previous stream."""
        try:
            screen, stream = screens.open_stream(screen_id, key, client_of(request))
        except ScreenError as exc:
            return failed(exc)
        return StreamingResponse(screens.events(screen, stream), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.put("/api/screens/{screen_id}/state", status_code=204, tags=["screens"])
    async def screen_state(screen_id: str, state: ScreenState, request: Request,
                           key: str = Query("", max_length=100)):
        """The TV page reports what it plays, with its `key`."""
        try:
            screens.set_state(screen_id, key, state, client_of(request))
        except ScreenError as exc:
            return failed(exc)
        return Response(status_code=204)

    def playlist_items(command: CmdPlayList, client: str) -> tuple[list[dict], int]:
        """The videos of a play_list that are in the library and connected, with their titles
        (from the index, not from the phone), and where the start item landed."""
        items: list[dict] = []
        start = 0
        for i, media_id in enumerate(command.media_ids):
            if i == command.index:
                start = len(items)  # if this one is dropped, the next kept one starts
            try:
                item = catalog.item(media_id)
            except MediaNotFound as exc:
                if exc.suspicious:
                    security_log.warning("Blocked play_list item from %s: %r: %s", client, media_id[:80], exc.reason)
                continue
            if item.available:
                items.append({"media_id": media_id, "title": item.title})
        return items, min(start, max(0, len(items) - 1))

    @app.post("/api/screens/{screen_id}/commands", status_code=202, tags=["screens"],
              responses={409: {"description": "The TV is not connected"}, **NOT_FOUND})
    async def screen_command(screen_id: str, request: Request, command: ScreenCommand = Body()):
        """Send a command to a TV: `play_media` (a video ID, start second), `play_list` (several
        videos in turn; step 6.5), `next`, `previous`, `playlist_mode` (shuffle, repeat), `play`,
        `pause`, `seek`, `seek_by`, `subtitle`, `volume`, `mute`, `stop`."""
        payload = command.model_dump()
        if isinstance(command, CmdPlayMedia):
            try:
                await anyio.to_thread.run_sync(catalog.item, command.media_id)
            except MediaNotFound as exc:
                if exc.suspicious:
                    security_log.warning("Blocked play_media from %s for %r: %s", client_of(request),
                                         command.media_id[:80], exc.reason)
                return not_found()
        elif isinstance(command, CmdPlayList):
            items, start = await anyio.to_thread.run_sync(playlist_items, command, client_of(request))
            if not items:
                return not_found()
            payload = {"action": "play_list", "items": items, "index": start, "at": command.at,
                       "shuffle": command.shuffle, "repeat": command.repeat, "title": command.title}
        try:
            screens.send(screen_id, payload, client_of(request))
        except ScreenError as exc:
            return failed(exc)
        return JSONResponse({"sent": command.action}, status_code=202)


def create_app(settings: Settings, catalog: MediaCatalog | None = None,
               folders: LibraryFolders | None = None) -> FastAPI:
    """``catalog`` defaults to the index at ``settings.index_path`` (opened now, closed on
    shutdown). Raises LibraryIndexError if the index cannot be used. ``folders`` (the library
    folders, editable on the admin page) defaults to the ones in ``settings``."""
    folders = folders or LibraryFolders(settings.library_paths, store=store_path_for(settings.index_path),
                                        source=settings.library_source)
    catalog = catalog or MediaCatalog(settings.index_path, folders.paths)
    rescanner = Rescanner(settings, folders)
    transcoder = TranscodeManager(settings, find_tools(settings))
    if not folders.paths():
        logger.warning("No library folder yet: open http://localhost:%s/admin on this computer to add one.",
                       settings.port)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        transcoder.shutdown()  # no ffmpeg is left running after the hub stops
        catalog.close()

    app = FastAPI(
        title=settings.app_name, version=settings.version, lifespan=lifespan,
        description="LAN video hub. Videos are addressed by ID only (see `GET /api/library`).",
    )
    app.state.settings = settings
    app.state.catalog = catalog
    app.state.rescanner = rescanner
    app.state.folders = folders
    app.state.transcoder = transcoder
    denied = (WEB_DIR / "admin-denied.html").read_text(encoding="utf-8").replace("{{PORT}}", str(settings.port))
    pin_guard = None
    if settings.access_pin:
        pin_guard = PinGuard(settings.access_pin, settings.index_path.parent / "hub-secret.key",
                             max_attempts=settings.pin_max_attempts,
                             lockout_seconds=settings.pin_lockout_minutes * 60,
                             remember_seconds=settings.pin_remember_days * 86400)
    app.state.pin_guard = pin_guard
    # The last added middleware runs first: LAN only (5.2), path traversal check (3.2),
    # PIN (5.2), then "admin = server only" (4.2), which a PIN never unlocks.
    app.add_middleware(ServerOnlyAdmin, denied_page=denied.encode("utf-8"))
    if pin_guard is not None:
        app.add_middleware(PinGate, guard=pin_guard)
    app.add_middleware(PathGuard)
    app.add_middleware(LanOnly, enabled=settings.access_lan_only)

    def client_of(request: Request) -> str:
        return request.client.host if request.client else "?"

    def log_miss(exc: MediaNotFound, request: Request, what: str, media_id: str) -> None:
        # The reason may contain a real path: log it, never send it.
        if exc.suspicious:
            security_log.warning("Blocked /%s request from %s for %r: %s",
                                 what, client_of(request), media_id[:80], exc.reason)
        else:
            logger.info("404 /%s/%s from %s: %s", what, media_id[:40], client_of(request), exc.reason)

    def lookup(media_id: str, request: Request, what: str) -> StreamSource | Response:
        """The file behind ``media_id`` (checked on disk), or the 404 to return."""
        try:
            return catalog.source(media_id)
        except MediaNotFound as exc:
            log_miss(exc, request, what, media_id)
            return not_found()

    # ------------------------------------------------------------------ system

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    def health() -> HealthResponse:
        return HealthResponse(version=settings.version)

    # ------------------------------------------------------------------ API

    @app.get("/api/library", response_model=LibraryPage, tags=["api"])
    def library(
        q: str = Query("", max_length=200, description="Words to find in title or folder; case "
                       "and Vietnamese marks are ignored ('cuoc chien' finds 'Cuộc Chiến')."),
        library_id: str | None = Query(None, alias="library", description="Only this library (its id)."),
        folder: str | None = Query(None, max_length=1000, description="Only this folder ('' = top)."),
        recursive: bool = Query(False, description="With `folder`: also the folders below it."),
        sort: Literal["path", "title", "newest", "largest"] = "path",
        offset: int = Query(0, ge=0),
        limit: int = Query(60, ge=1, le=MAX_PAGE),
    ) -> LibraryPage:
        """Videos, a page at a time. Default order: library, folder, then natural title order
        ('Tập 2' before 'Tập 10')."""
        items = catalog.items(q)
        if library_id is not None:
            items = [i for i in items if catalog.library_of(i).id == library_id]
        if folder is not None:
            folder = folder.strip("/")
            items = [i for i in items
                     if i.folder == folder or (recursive and (folder == "" or i.folder.startswith(folder + "/")))]
        if sort == "title":
            items.sort(key=lambda i: (i.title.casefold(), i.id))
        elif sort == "newest":
            items.sort(key=lambda i: (-i.mtime, i.id))
        elif sort == "largest":
            items.sort(key=lambda i: (-i.size, i.id))
        page = items[offset:offset + limit]
        end = offset + len(page)
        return LibraryPage(
            items=[MediaSummary(**_summary_fields(i, catalog.library_of(i))) for i in page],
            total=len(items), offset=offset, limit=limit, next_offset=end if end < len(items) else None,
        )

    @app.get("/api/folders", response_model=FolderList, tags=["api"])
    def folder_list() -> FolderList:
        """Libraries and every folder that holds videos (for browsing by folder)."""
        direct: Counter[tuple[str, str]] = Counter()  # (library id, folder) → videos in it
        total: Counter[tuple[str, str]] = Counter()  # … in it and below
        libs: dict[str, Library] = {lib.id: lib for lib in catalog.libraries()}
        for item in catalog.items():
            lib_id = catalog.library_of(item).id
            direct[(lib_id, item.folder)] += 1
            parts = item.folder.split("/") if item.folder else []
            for n in range(len(parts) + 1):
                total[(lib_id, "/".join(parts[:n]))] += 1
        entries = [FolderInfo(library=_ref(libs[lib_id]), folder=f, videos=direct[(lib_id, f)],
                              total_videos=n) for (lib_id, f), n in total.items()]
        entries.sort(key=lambda e: (e.library.name.casefold(), e.library.id, natural_key(e.folder)))
        return FolderList(libraries=[_ref(lib) for lib in sorted(libs.values(), key=lambda x: x.name.casefold())],
                          folders=entries)

    @app.get("/api/media/{media_id}", response_model=MediaDetail, tags=["api"], responses=NOT_FOUND)
    def media(media_id: str, request: Request):
        """Everything about one video, with its stream, thumbnail and subtitle URLs."""
        try:
            item = catalog.item(media_id)
        except MediaNotFound as exc:
            log_miss(exc, request, "api/media", media_id)
            return not_found()
        subtitles: list[SubtitleTrackOut] = []
        if item.available:
            try:
                source = catalog.source(media_id)
                subtitles = [SubtitleTrackOut(**t.model_dump(), url=f"/subtitles/{media_id}/{t.key}.vtt")
                             for t in find_subtitles(source.path, settings.video_extensions)]
            except MediaNotFound as exc:  # file gone since the scan, or swapped for a link
                log_miss(exc, request, "api/media", media_id)
        info = item.info
        return MediaDetail(
            **_summary_fields(item, catalog.library_of(item)),
            info_status=info.status if info else "pending",
            info_error=info.error if info else None,
            container=info.container if info else None,
            video_codec=info.video_codec if info else None,
            pix_fmt=info.pix_fmt if info else None,
            audio_codec=info.audio_codec if info else None,
            playable_note=info.playable_note if info else None,
            subtitles=subtitles,
        )

    @app.post("/api/rescan", response_model=RescanStatus, status_code=202, tags=["api"],
              responses={409: {"description": "No library folder yet (add one on the admin page)"}})
    def rescan():
        """Scan the library folders again in the background (new, removed, renamed videos;
        details and thumbnails for new ones). Playing videos are not interrupted. Poll
        `GET /api/rescan` for progress. If a rescan is already running, its status is returned
        (`started: false`)."""
        if not folders.paths():
            # Scanning "no folder" would empty the library: refuse and say what to do.
            return JSONResponse({"detail": "No library folder yet: add one on the admin page "
                                           "(on the server computer)."}, status_code=409)
        return rescanner.start()

    @app.get("/api/hub", response_model=HubInfo, tags=["system"])
    def hub_info(request: Request) -> HubInfo:
        """Hub version, whether this device is the server (the admin page is only for it),
        and how many library folders are set (no paths: other devices must not see them)."""
        return HubInfo(version=settings.version, is_server=is_server_request(request.scope),
                       folders=len(folders.paths()), pin=pin_guard is not None)

    # ------------------------------------------------------------------ PIN (step 5.2)

    def safe_next(target: str | None) -> str:
        # Only a path of this hub: never "//other.site" or "http://…" (open redirect).
        if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
            return "/"
        return target

    @app.get("/pin", include_in_schema=False)
    def pin_page(request: Request, next: str | None = None):
        token = request.cookies.get("hub_pin")
        if pin_guard is None or is_server_request(request.scope) or pin_guard.token_ok(token):
            return RedirectResponse(safe_next(next), status_code=303)
        return FileResponse(WEB_DIR / "pin.html", media_type="text/html; charset=utf-8",
                            headers={"Cache-Control": "no-store"})

    @app.post("/api/pin", tags=["system"],
              responses={401: {"description": "Wrong PIN (`attempts_left`)"},
                         429: {"description": "Too many wrong PINs: wait (`retry_after` seconds)"}})
    def pin_check(typed: PinIn, request: Request):
        """Unlock this device with the hub's PIN (a cookie remembers it)."""
        if pin_guard is None:
            return {"ok": True}
        right, left, locked = pin_guard.check(client_of(request), typed.pin)
        if locked:
            minutes = max(1, round(locked / 60))
            return JSONResponse({"detail": f"Nhập sai quá nhiều lần. Thử lại sau khoảng {minutes} phút.",
                                 "retry_after": locked}, status_code=429, headers={"Retry-After": str(locked)})
        if not right:
            return JSONResponse({"detail": "PIN không đúng.", "attempts_left": left}, status_code=401)
        logger.info("PIN accepted from %s", client_of(request))
        return JSONResponse({"ok": True}, headers={"Set-Cookie": pin_guard.cookie_header()})

    # ------------------------------------------------------------------ admin (server only)
    # Requests from other devices never get here: ServerOnlyAdmin answers them with 403.

    def admin_view() -> AdminFolders:
        counts = catalog.video_counts()
        return AdminFolders(
            folders=[AdminFolder(path=str(c.path), exists=c.exists, problem=c.problem,
                                 videos=counts.get(_key(str(c.path)), 0)) for c in folders.check()],
            source=folders.source, editable=folders.editable, store_file=str(folders.store))

    @app.get("/api/admin/libraries", response_model=AdminFolders, tags=["admin"])
    def admin_libraries() -> AdminFolders:
        """Library folders with their state and video count. **Server computer only.**"""
        return admin_view()

    @app.put("/api/admin/libraries", response_model=AdminFolders, tags=["admin"],
             responses={422: {"model": AdminFoldersRejected, "description": "Some paths cannot be used"},
                        409: {"description": "Folders were given with --path for this run"}})
    def admin_save_libraries(update: AdminFoldersUpdate, request: Request):
        """Replace the list of library folders. Saved, and applied at once (no restart);
        videos of a removed folder disappear immediately, the next rescan drops them from
        the index. New videos appear after a rescan. **Server computer only.**"""
        try:
            folders.save(update.paths, by=client_of(request))
        except FoldersLocked:
            return JSONResponse({"detail": "The folders were given with --path for this run; "
                                           "restart the hub without --path to edit them here."},
                                status_code=409)
        except FolderError as exc:
            body = AdminFoldersRejected(detail="Some folders cannot be used.",
                                        problems=[FolderProblem(path=p, message=m) for p, m in exc.problems])
            return JSONResponse(body.model_dump(), status_code=422)
        except OSError as exc:
            logger.error("Cannot save the library folders to %s: %s", folders.store, exc)
            return JSONResponse({"detail": f"Cannot save the list: {exc.strerror or exc}"}, status_code=500)
        return admin_view()

    @app.post("/api/admin/pick-folder", response_model=PickedFolder, tags=["admin"],
              responses={409: {"description": "The picker is already open"},
                         501: {"description": "No folder picker here (no desktop session): type the path"}})
    def admin_pick_folder():
        """Open this computer's own folder picker (File Explorer on Windows, Finder on macOS),
        starting in Downloads, and wait for the choice. Nothing is saved: the admin page adds
        the folder with `PUT /api/admin/libraries` as if it was typed. **Server computer only**
        (only the person at the server can open the picker)."""
        try:
            path = folder_picker.pick_folder(folder_picker.downloads_folder())
        except folder_picker.PickerBusy:
            return JSONResponse({"detail": "Hộp chọn thư mục đang mở trên máy chủ."}, status_code=409)
        except folder_picker.PickerUnavailable as exc:
            logger.warning("Folder picker unavailable: %s", exc)
            return JSONResponse({"detail": "Không mở được hộp chọn thư mục trên máy này. Hãy dán đường dẫn "
                                           "thư mục vào ô rồi bấm Thêm."}, status_code=501)
        logger.info("Folder picker: %s", path if path is not None else "cancelled")
        return PickedFolder(path=path, cancelled=path is None)

    @app.get("/watch/{media_id}", include_in_schema=False)
    def watch_page(media_id: str):
        """The player page (plan step 4.3). The page loads /api/media/<id> itself, so an
        unknown ID shows a "video not found" message there."""
        return FileResponse(WEB_DIR / "watch.html", media_type="text/html; charset=utf-8",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/admin", include_in_schema=False)
    def admin_page():
        """The admin page (server computer only)."""
        return FileResponse(WEB_DIR / "admin.html", media_type="text/html; charset=utf-8",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/api/rescan", response_model=RescanStatus, tags=["api"])
    def rescan_status() -> RescanStatus:
        """State of the current or last rescan."""
        return rescanner.status()

    @app.get("/thumbnails/{media_id}.jpg", tags=["media"], response_class=FileResponse,
             responses={200: {"content": {"image/jpeg": {}}}, **NOT_FOUND})
    def thumbnail(media_id: str, request: Request):
        """Thumbnail of a video; a default image when none was made (yet)."""
        try:
            catalog.item(media_id)
        except MediaNotFound as exc:
            log_miss(exc, request, "thumbnails", media_id)
            return not_found()
        path = thumb_path(settings.cache_dir, media_id)
        try:
            st = os.stat(path)
        except OSError:
            path, st = DEFAULT_THUMBNAIL, os.stat(DEFAULT_THUMBNAIL)
        # no-cache = revalidate with the ETag each time: a refreshed thumbnail shows up at
        # once, an unchanged one costs a 304 without body (a grid of 60 thumbnails stays fast).
        response = FileResponse(path, media_type="image/jpeg", stat_result=st,
                                headers={"Cache-Control": "no-cache"})
        etag = response.headers.get("etag")
        if etag and etag in [t.strip() for t in request.headers.get("if-none-match", "").split(",")]:
            return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
        return response

    # ------------------------------------------------------------------ media files

    # `:path` so that anything under /stream/ (also with '/' in it) reaches the handler and
    # is logged as a probe, instead of silently falling through to the router's 404.
    def stream(media_id: str, request: Request):
        """Play a video by ID. Supports `Range` requests so players can seek."""
        # A plain `def`: the index lookup and the disk check run in the thread pool.
        source = lookup(media_id, request, "stream")
        if isinstance(source, Response):
            return source
        return RangeFileResponse(source, request.headers.get("range"), request.headers.get("if-range"),
                                 request.method, client_of(request))

    for method in ("GET", "HEAD"):  # one route each, so /docs gets two distinct operations
        app.add_api_route(
            "/stream/{media_id:path}", stream, methods=[method], tags=["media"],
            operation_id=f"stream_{method.lower()}", response_class=Response,
            summary="Play a video" if method == "GET" else "Size and type of a video (no body)",
            responses={200: {"description": "The whole file"},
                       206: {"description": "The requested byte range"},
                       416: {"description": "Range not satisfiable"}, **NOT_FOUND})

    # ------------------------------------------------------------------ conversion (step 5.1)

    @app.post("/api/play/{media_id}", response_model=PlayPlan, tags=["media"],
              responses={409: {"description": "This video cannot be converted (reason in `detail`)"},
                         503: {"description": "Too many conversions at once (`Hub đang bận`)"}, **NOT_FOUND})
    def play(media_id: str, wanted: PlayRequest, request: Request):
        """How to play a video that this browser cannot play as it is: the hub remuxes it
        (picture kept) or converts it to H.264, as HLS. The page sends what the browser can
        decode; `direct` means no conversion is needed."""
        try:
            item = catalog.item(media_id)
        except MediaNotFound as exc:
            log_miss(exc, request, "api/play", media_id)
            return not_found()
        source = lookup(media_id, request, "api/play")
        if isinstance(source, Response):
            return source
        caps = ClientCaps(video=frozenset(wanted.video), audio=frozenset(wanted.audio))
        if not wanted.avoid_direct and plays_directly(item.info, caps):
            return PlayPlan(mode="direct", url=f"/stream/{media_id}")
        try:
            decision = decide(item.info, caps)
        except CannotConvert as exc:
            return JSONResponse({"detail": str(exc)}, status_code=409)
        unavailable = transcoder.unavailable_reason()
        if unavailable:
            return JSONResponse({"detail": unavailable}, status_code=409)
        try:
            url = transcoder.open(media_id=media_id, title=item.title, source=source.path,
                                  signature=item.signature, duration=item.info.duration,
                                  decision=decision, client=client_of(request))
        except Busy as exc:
            logger.warning("Conversion of %s refused for %s: %s", media_id, client_of(request), exc)
            return busy_response(exc)
        except OSError as exc:
            logger.error("Cannot start ffmpeg for %s: %s", media_id, exc)
            return JSONResponse({"detail": f"Không chạy được ffmpeg: {exc.strerror or exc}"}, status_code=500)
        session = url.split("/")[3] if decision.mode == "transcode" else None
        return PlayPlan(mode=decision.mode, url=url, mime=decision.mime, session=session, reason=decision.reason)

    @app.post("/api/play/sessions/{sid}/stop", status_code=204, tags=["media"])
    def play_stop(sid: str):
        """End a transcode session now (the player page calls this when the tab closes)."""
        if SESSION_ID.fullmatch(sid):
            transcoder.stop_session(sid)
        return Response(status_code=204)

    async def hls_answer(get, request: Request) -> Response:
        """Wait (up to HLS_WAIT_SECONDS) for a piece that ffmpeg is making, then send it."""
        deadline = time.monotonic() + HLS_WAIT_SECONDS
        first = True
        while True:
            if not first and await request.is_disconnected():  # gone: do not ask the manager again
                return Response(status_code=499)
            first = False
            try:
                result = await anyio.to_thread.run_sync(get)
            except SessionGone:
                return not_found()
            except Busy as exc:
                return busy_response(exc)
            except ConvertFailed as exc:
                return JSONResponse({"detail": str(exc)}, status_code=500)
            if isinstance(result, Path) and not await anyio.to_thread.run_sync(_can_open, result):
                result = None  # just renamed by ffmpeg, still held for a moment (Windows): wait
            if result is not None:
                break
            if time.monotonic() > deadline:
                return JSONResponse({"detail": "Video chưa sẵn sàng, thử lại."}, status_code=503,
                                    headers={"Retry-After": "2"})
            if await request.is_disconnected():
                return Response(status_code=499)
            await anyio.sleep(0.15)
        headers = {"Cache-Control": "no-cache"}
        if isinstance(result, bytes):
            return Response(result, media_type=M3U8, headers=headers)
        media_type = M3U8 if result.suffix == ".m3u8" else "video/mp4"
        return FileResponse(result, media_type=media_type, headers=headers)

    @app.get("/hls/s/{sid}/{name}", include_in_schema=False)
    async def hls_session(sid: str, name: str, request: Request):
        if not (SESSION_ID.fullmatch(sid) and HLS_FILE.fullmatch(name)):
            return not_found()
        return await hls_answer(lambda: transcoder.transcode_file(sid, name), request)

    @app.get("/hls/r/{media_id}/{variant}/{name}", include_in_schema=False)
    async def hls_remux(media_id: str, variant: str, name: str, request: Request):
        if variant not in REMUX_VARIANTS or not HLS_FILE.fullmatch(name):
            return not_found()
        try:  # a cached copy is only served while its video is in the library
            await anyio.to_thread.run_sync(catalog.item, media_id)
        except MediaNotFound as exc:
            log_miss(exc, request, "hls", media_id)
            return not_found()
        return await hls_answer(lambda: transcoder.remux_file(media_id, variant, name), request)

    @app.get("/api/admin/network", response_model=NetworkInfo, tags=["admin"])
    def admin_network(request: Request) -> NetworkInfo:
        """The address (and QR code) phones should open, chosen among this computer's network
        adapters (Wi-Fi/Ethernet with a router first; VPN and virtual ones apart).
        **Server computer only.**"""
        server = request.scope.get("server")
        port = server[1] if server else settings.port
        found = network.find_addresses()
        warnings = list(found.warnings) + network.public_profile_warnings(found)

        def row(a, url=None):
            return NetworkAddress(ip=a.ip, name=a.name, kind=a.kind, up=a.up, url=url)

        urls = found.urls(port)
        return NetworkInfo(port=port, addresses=[row(a, u) for a, u in zip(found.best, urls)],
                           others=[row(a) for a in found.others],
                           qr_svg=network.qr_svg_data_uri(urls[0]) if urls else None, warnings=warnings)

    @app.get("/api/admin/tools", response_model=ToolsInfo, tags=["admin"])
    def admin_tools() -> ToolsInfo:
        """ffmpeg/ffprobe found on this computer, and the features that are off without them.
        **Server computer only.**"""
        tools = find_tools(settings)  # looked up again: installing ffmpeg shows here without a restart
        return ToolsInfo(ffmpeg=tools.ffmpeg, ffprobe=tools.ffprobe, disabled=disabled_features(tools),
                         hint=INSTALL_HINT)

    @app.get("/api/admin/transcode", response_model=Conversions, tags=["admin"])
    def admin_transcode() -> Conversions:
        """Conversions running now (ffmpeg processes). **Server computer only.**"""
        return Conversions(active=transcoder.active_count(), limit=transcoder.limit,
                           items=[Conversion(**row) for row in transcoder.status()])

    @app.get("/subtitles/{media_id}", tags=["media"], response_model=list[SubtitleTrackOut],
             responses=NOT_FOUND)
    def subtitle_tracks(media_id: str, request: Request):
        """Subtitle tracks of a video (`.srt`/`.vtt` files next to it), with their URLs.
        An empty list when the video has none."""
        source = lookup(media_id, request, "subtitles")
        if isinstance(source, Response):
            return source
        return [SubtitleTrackOut(**t.model_dump(), url=f"/subtitles/{media_id}/{t.key}.vtt")
                for t in find_subtitles(source.path, settings.video_extensions)]

    @app.get("/subtitles/{media_id}/{key}.vtt", tags=["media"], response_class=PlainTextResponse,
             responses={200: {"description": "WebVTT, UTF-8", "content": {"text/vtt": {}}},
                        422: {"description": "The subtitle file cannot be read (encoding/format)"},
                        **NOT_FOUND})
    def subtitle_vtt(media_id: str, key: str, request: Request):
        """One subtitle track as WebVTT (`.srt` is converted; any encoding becomes UTF-8)."""
        source = lookup(media_id, request, "subtitles")
        if isinstance(source, Response):
            return source
        track = next((t for t in find_subtitles(source.path, settings.video_extensions) if t.key == key), None)
        if track is None:
            logger.info("404 /subtitles/%s/%s.vtt from %s: no such track", media_id, key[:40],
                        client_of(request))
            return not_found()
        path = source.path.parent / track.file_name
        try:
            vtt = load_as_vtt(path, track.format)
        except SubtitleError as exc:
            logger.warning("Subtitle %s cannot be served: %s", path, exc)
            # The reason (encoding/format) helps the user fix the file; it holds no path.
            return JSONResponse({"detail": f"Subtitle file cannot be used: {exc}"}, status_code=422)
        if vtt.skipped_blocks:
            logger.info("Subtitle %s: %d cues, %d unreadable blocks skipped", path, vtt.cues,
                        vtt.skipped_blocks)
        if vtt.source_encoding not in ("utf-8", "utf-8-sig"):
            logger.info("Subtitle %s decoded as %s", path.name, vtt.source_encoding)
        return Response(vtt.text.encode("utf-8"), media_type="text/vtt; charset=utf-8",
                        headers={"Cache-Control": "no-cache"})

    # ------------------------------------------------------------------ web client

    @app.get("/", include_in_schema=False)
    def index():
        """The library page (plan step 4.1). Its state lives in the query string."""
        return FileResponse(WEB_DIR / "index.html", media_type="text/html; charset=utf-8",
                            headers={"Cache-Control": "no-cache"})

    class _RevalidatedStatic(StaticFiles):
        """CSS/JS of the hub: revalidated each load, so an update shows up without a hard refresh."""

        def file_response(self, *args, **kwargs):
            response = super().file_response(*args, **kwargs)
            response.headers["Cache-Control"] = "no-cache"
            return response

    app.mount("/static", _RevalidatedStatic(directory=WEB_DIR), name="static")

    _add_screens(app, settings, catalog)

    if settings.app_env != "production":
        _add_diagnostics(app, settings)

    if settings.app_env != "production" and DEMO_PAGE.is_file():
        @app.get("/demo/subtitles", include_in_schema=False)
        def demo_subtitles():
            """Test page for step 3.3 (<video> + <track>), until the player of step 4.2."""
            return FileResponse(DEMO_PAGE, media_type="text/html; charset=utf-8")

    return app
