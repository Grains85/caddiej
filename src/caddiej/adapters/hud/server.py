"""HUD transport: static overlay files plus the floor state feed.

FastAPI and uvicorn are used purely as transport here. Routes hold no logic;
they delegate to `FloorService` and the connection registry, and no framework
type escapes this module.
"""

import asyncio
import hashlib
import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.responses import Response
from starlette.types import Scope

from caddiej.adapters.asgi import AsgiServerError, LocalAsgiServer
from caddiej.adapters.hud.connections import ConnectionRegistry, HudConnection
from caddiej.adapters.hud.evidence import media_type as evidence_media_type
from caddiej.adapters.hud.protocol import (
    EVIDENCE_ROUTE,
    PROTOCOL_VERSION,
    ClientMessage,
    ErrorMessage,
    PongMessage,
    ReloadMessage,
    hello_message,
    snapshot_message,
)
from caddiej.floor.service import FloorService

logger = logging.getLogger(__name__)

STATIC_ROOT = Path(__file__).parent / "static"
_OVERLAY_ASSET_FILES = ("js/client.js", "js/state.js", "css/floor.css", "index.html")
_OVERLAY_JS_FILES = frozenset({"client.js", "state.js"})
# OBS Browser Source (CEF) caches ES modules by URL even when Cache-Control
# says otherwise. The content hash is substituted into HTML and into the
# client.js import of state.js so a restart + refresh loads new JS.
_NO_STORE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}
# Path-versioned overlay files. OBS CEF caches ES modules by pathname, so the
# content hash is a directory, not a query string.
_VERSIONED_OVERLAY = {
    "client.js": ("js/client.js", "text/javascript; charset=utf-8"),
    "state.js": ("js/state.js", "text/javascript; charset=utf-8"),
    "floor.css": ("css/floor.css", "text/css; charset=utf-8"),
}


def overlay_assets_version(root: Path = STATIC_ROOT) -> str:
    """Stable short hash of the overlay HTML/JS/CSS this process will serve."""

    hasher = hashlib.sha256()
    for relative in _OVERLAY_ASSET_FILES:
        hasher.update((root / relative).read_bytes())
    return hasher.hexdigest()[:12]


def _with_asset_version(text: str, version: str | None = None) -> str:
    return text.replace("__HUD_ASSETS_VERSION__", version or overlay_assets_version())


# How many times one page is sent to one build before CaddieJ stops asking.
# The second try covers a reload that raced a cache; past that, whatever is
# keeping the old renderer alive is not something another reload will fix, and
# a projector that blanks every few seconds is worse than one that is stale.
ASSETS_RELOAD_LIMIT = 2
_RELOAD_FOR = "hud_assets"
_RELOAD_ATTEMPT = "hud_reload"


def assets_reload_url(
    expected: str, reload_for: str | None, attempt: str | None
) -> str | None:
    """Where a stale page should go next, or None once it has been asked enough.

    The count lives in the page's own address rather than on the server or in
    browser storage. Every navigation carries it, so it survives the reload it
    is counting, and it cannot leak between pages or outlive the build it is
    about: a count for any other build is a count of nothing, because a page
    that reached the last build and is now behind again deserves its tries.
    A count that is not a number is treated as spent, so the one state nobody
    planned for is a stale floor and a log line rather than a reload loop.
    """

    tried = 0
    if reload_for == expected:
        text = (attempt or "").strip() or "0"
        tried = int(text) if text.isascii() and text.isdigit() else ASSETS_RELOAD_LIMIT
    if tried >= ASSETS_RELOAD_LIMIT:
        return None
    return "/?" + urlencode({_RELOAD_FOR: expected, _RELOAD_ATTEMPT: tried + 1})


def _recovery_module(expected: str) -> str:
    """What a page asking for an old build's `client.js` is given instead.

    Such a page has HTML from a previous run, so nothing it would import can
    be trusted to match it, and the current renderer cannot simply be handed
    over under the old name. All it can usefully do is load `/` again, which
    is never cached. The same address and the same limit as the WebSocket
    path, worked out in the browser because no socket will ever open.
    """

    return (
        f'const expected = "{expected}";\n'
        "const params = new URLSearchParams(window.location.search);\n"
        f'const tried = params.get("{_RELOAD_FOR}") === expected\n'
        f'  ? Number(params.get("{_RELOAD_ATTEMPT}"))\n'
        "  : 0;\n"
        f"if (Number.isInteger(tried) && tried >= 0 && tried < {ASSETS_RELOAD_LIMIT}) {{\n"
        "  window.location.replace(\n"
        f"    `/?{_RELOAD_FOR}=${{expected}}&{_RELOAD_ATTEMPT}=${{tried + 1}}`,\n"
        "  );\n"
        "}\n"
    )


class _NoStoreStaticFiles(StaticFiles):
    """OBS Browser Source caches aggressively; overlay JS must not stick."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        for key, value in _NO_STORE.items():
            response.headers[key] = value
        return response


# How long to let a queued snapshot finish sending after the client disconnects.
_SEND_DRAIN_TIMEOUT = 5.0

# Client-supplied log text is untrusted and unbounded; only a useful prefix is
# worth putting in CaddieJ's logs.
_CLIENT_LOG_LIMIT = 500

# Connects worth a line each. OBS legitimately reconnects when a source is
# shown, hidden, or refreshed, and the first few of those are what an operator
# is looking for when they ask whether the floor came up. Past this the
# interesting quantity is the rate, which `/healthz` reports as a running
# total — a page in a reconnect loop wrote 8,141 INFO lines into one session
# log and buried everything else in it.
_CONNECT_LOG_LIMIT = 20


def overlay_url(host: str, port: int) -> str:
    """The URL a browser should load to render the overlay.

    A wildcard bind address is not something a browser can connect to, so it is
    reported as loopback.
    """

    if host in {"0.0.0.0", "::", "*", ""}:  # noqa: S104 - comparison, not a bind
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}/"


def _is_same_origin(websocket: WebSocket) -> bool:
    """Whether the handshake came from the page CaddieJ itself served.

    WebSockets are exempt from the same-origin policy, so without this any page
    in any local browser could read the feed and write to CaddieJ's logs. A
    missing Origin means a non-browser client, which is allowed.
    """

    origin = websocket.headers.get("origin")
    if origin is None:
        return True
    return urlsplit(origin).netloc.lower() == websocket.headers.get("host", "").lower()


HealthProbe = Callable[[], Mapping[str, object]]
"""Anything that can report on a subsystem when `/healthz` is asked.

A callable rather than a type, so the HUD adapter needs no knowledge of the
subsystems it reports on. What it publishes is whatever they hand it.
"""


EvidenceResolver = Callable[[str, str], Path | None]
"""Capture id and frame id in, a stored still out, or None for anything else."""


def create_app(
    service: FloorService,
    registry: ConnectionRegistry,
    subsystems: Mapping[str, HealthProbe] | None = None,
    *,
    evidence: EvidenceResolver | None = None,
) -> FastAPI:
    """Build the ASGI application for the floor overlay."""

    app = FastAPI(title="CaddieJ HUD", docs_url=None, redoc_url=None, openapi_url=None)
    probes = dict(subsystems or {})
    # Hashed once. The files cannot change under a running process without a
    # restart anyway, and recomputing it meant four file reads and a SHA-256
    # on every health check, every asset request and every WebSocket connect.
    assets_version = overlay_assets_version()

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        state = service.state
        report: dict[str, object] = {
            "status": "ok",
            "protocol_version": PROTOCOL_VERSION,
            "state_version": state.version,
            "mode": state.mode.value,
            "clients": registry.count,
            "client_connects": registry.connects,
            "client_silent_seconds": (
                None
                if registry.silent_seconds is None
                else round(registry.silent_seconds, 1)
            ),
            "hud_static_root": str(STATIC_ROOT.resolve()),
            "hud_assets_version": assets_version,
        }
        for name, probe in probes.items():
            report[name] = dict(probe())
        return report

    @app.get("/")
    async def index() -> HTMLResponse:
        html = _with_asset_version(
            (STATIC_ROOT / "index.html").read_text(encoding="utf-8"), assets_version
        )
        return HTMLResponse(html, headers=_NO_STORE)

    @app.get("/snapshot")
    async def snapshot() -> Response:
        """The same JSON the Browser Source receives on the WebSocket."""

        return Response(
            snapshot_message(service.state).model_dump_json(),
            media_type="application/json",
            headers=_NO_STORE,
        )

    @app.get(EVIDENCE_ROUTE + "/{capture_id}/{frame_id}")
    async def evidence_frame(capture_id: str, frame_id: str) -> Response:
        """The still a coaching line cites, or 404. Never a stand-in."""

        if evidence is None:
            return Response(status_code=404)
        content = await asyncio.to_thread(
            _read_evidence, evidence, capture_id, frame_id
        )
        if content is None:
            return Response(status_code=404)
        data, kind = content
        return Response(data, media_type=kind, headers=_NO_STORE)

    @app.get("/static/js/{name}")
    async def overlay_javascript(name: str) -> Response:
        if name not in _OVERLAY_JS_FILES:
            return Response(status_code=404)
        text = _with_asset_version(
            (STATIC_ROOT / "js" / name).read_text(encoding="utf-8"), assets_version
        )
        return Response(
            text,
            media_type="text/javascript; charset=utf-8",
            headers={**_NO_STORE, "X-CaddieJ-Assets-Version": assets_version},
        )

    @app.get("/static/hud/{version}/{name}")
    async def versioned_overlay_asset(version: str, name: str) -> Response:
        """Overlay files whose URL pathname includes the content hash.

        Query-string cache busting is not enough: OBS's embedded CEF treats
        `/static/js/state.js?v=abc` and `/static/js/state.js` as the same
        module. A new hash is a new path, so a Browser Source refresh cannot
        keep executing the previous renderer.

        The version is checked rather than decorative. Serving today's file
        under yesterday's hash made the path a label instead of an address: a
        Browser Source pinned to the old URL kept working, kept running the
        old renderer, and nothing on either side could tell. A 404 was the
        next answer, and it was only half of one: the operator could see the
        failure, but the projector stayed dark until somebody walked over and
        refreshed it. So the entry module of an old build is answered with a
        page reload instead, and everything else of it is still not found.
        """

        spec = _VERSIONED_OVERLAY.get(name)
        if spec is None:
            return Response(status_code=404)
        if version != assets_version:
            logger.warning(
                "overlay_assets_stale",
                extra={
                    "hud_assets_version": version[:64],
                    "hud_assets_expected": assets_version,
                },
            )
            if name != "client.js":
                return Response(status_code=404)
            return Response(
                _recovery_module(assets_version),
                media_type="text/javascript; charset=utf-8",
                headers={**_NO_STORE, "X-CaddieJ-Assets-Version": assets_version},
            )
        relative, media_type = spec
        text = (STATIC_ROOT / relative).read_text(encoding="utf-8")
        if name.endswith(".js"):
            text = _with_asset_version(text, assets_version)
        return Response(
            text,
            media_type=media_type,
            headers={**_NO_STORE, "X-CaddieJ-Assets-Version": assets_version},
        )

    @app.websocket("/ws")
    async def floor_feed(websocket: WebSocket) -> None:
        if not _is_same_origin(websocket):
            logger.warning(
                "hud_client_rejected",
                extra={"hud_client_origin": websocket.headers.get("origin", "")},
            )
            # Closing before accepting rejects the handshake outright.
            await websocket.close(code=1008)
            return

        await websocket.accept()
        connection = registry.register()
        noisy = registry.connects > _CONNECT_LOG_LIMIT
        logger.log(
            logging.DEBUG if noisy else logging.INFO,
            "hud_client_connected",
            extra={"hud_clients": registry.count, "hud_connects": registry.connects},
        )
        try:
            # The handshake is written directly because the send task does not
            # exist yet. Everything after this goes through `connection`.
            await websocket.send_text(
                hello_message(hud_assets_version=assets_version).model_dump_json()
            )
            await websocket.send_text(snapshot_message(service.state).model_dump_json())
            await _serve_connection(websocket, connection, service, assets_version)
        except WebSocketDisconnect:
            pass
        finally:
            registry.unregister(connection)
            logger.log(
                logging.DEBUG if noisy else logging.INFO,
                "hud_client_disconnected",
                extra={
                    "hud_clients": registry.count,
                    "hud_connects": registry.connects,
                    "hud_dropped": connection.dropped,
                },
            )

    app.mount("/static", _NoStoreStaticFiles(directory=STATIC_ROOT), name="static")
    return app


def _read_evidence(
    resolve: EvidenceResolver, capture_id: str, frame_id: str
) -> tuple[bytes, str] | None:
    """Resolve and read off the event loop; a file pruned in between is absent."""

    try:
        path = resolve(capture_id, frame_id)
        if path is None:
            return None
        kind = evidence_media_type(path)
        if kind is None:
            return None
        return path.read_bytes(), kind
    except OSError:
        return None


async def _serve_connection(
    websocket: WebSocket,
    connection: HudConnection,
    service: FloorService,
    assets_version: str,
) -> None:
    """Send state and handle client control messages until the client goes away.

    The receive loop detects the disconnect; closing the connection then lets the
    send loop finish by itself. Nothing is cancelled, which keeps the ASGI task's
    completion clean.
    """

    sender = asyncio.create_task(_send_loop(websocket, connection), name="hud-send")
    try:
        await _receive_loop(connection, websocket, service, assets_version)
    except WebSocketDisconnect:
        pass
    finally:
        connection.close()
        try:
            # On timeout wait_for cancels the sender and waits for it, so a
            # client stalled mid-send cannot hold the handler open.
            await asyncio.wait_for(sender, _SEND_DRAIN_TIMEOUT)
        except TimeoutError:
            logger.warning("hud_send_loop_timed_out")
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("hud_send_loop_failed")


async def _send_loop(websocket: WebSocket, connection: HudConnection) -> None:
    """The only task that writes to this socket, for the life of the socket."""

    while True:
        text = await connection.next_outbound()
        if text is None:
            return
        await websocket.send_text(text)


async def _receive_loop(
    connection: HudConnection,
    websocket: WebSocket,
    service: FloorService,
    assets_version: str,
) -> None:
    while True:
        # `receive` rather than `receive_text`, because the latter assumes the
        # frame carries text and raises KeyError on a binary one. Any local page
        # can open this socket, so the frame type is not ours to assume.
        frame = await websocket.receive()
        if frame["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(frame.get("code", 1005), frame.get("reason"))

        connection.note_message()
        raw = frame.get("text")
        if raw is None:
            connection.reply(
                ErrorMessage(detail="only text frames are accepted").model_dump_json()
            )
            continue

        try:
            message = ClientMessage.model_validate_json(raw)
        except ValidationError:
            connection.reply(ErrorMessage(detail="malformed message").model_dump_json())
            continue

        match message.type:
            case "resync":
                connection.offer(service.state)
            case "ping":
                connection.reply(PongMessage().model_dump_json())
            case "hello":
                loaded = (message.assets_version or "").strip()
                connection.assets_version = loaded
                connection.page_id = (message.page_id or "")[:64]
                connection.href = (message.href or "")[:200]
                logger.info(
                    "hud_client_hello",
                    extra={
                        "hud_surface": message.surface or "unknown",
                        # An overlay old enough to predate this field is the
                        # exact case `overlay_assets_stale` was built to catch
                        # and the exact case it could not see, because the
                        # detector only ran when the field was present.
                        "hud_assets_version": loaded or "unreported",
                        # Which page this is. With two overlays connected and
                        # one of them stale, the first hardware test had no way
                        # to tell which was which: both report `surface: floor`
                        # and nothing else distinguished them. A page keeps an
                        # id for as long as it is loaded, so these two fields
                        # say which browser source a client belongs to and
                        # whether a reload actually replaced it.
                        "hud_page_id": connection.page_id or "unreported",
                        "hud_client_href": connection.href or "unreported",
                    },
                )
                if loaded != assets_version:
                    # Refused before it is asked to reload, because the asking
                    # may not work. See `without_coaching`.
                    connection.stale = True
                    connection.offer(service.state)
                    logger.warning(
                        "overlay_assets_stale",
                        extra={
                            "hud_assets_version": loaded or "unreported",
                            "hud_assets_expected": assets_version,
                            "hud_page_id": connection.page_id or "unreported",
                            "coaching_detail": (
                                "coaching withheld from this client until it "
                                "is running the served build"
                            ),
                        },
                    )
                    _request_reload(connection, message, assets_version)
                elif connection.stale:
                    # The reload worked. A page that comes back on the served
                    # build is trusted again, and gets the floor at once rather
                    # than at the next shot.
                    connection.stale = False
                    connection.offer(service.state)
                    logger.info(
                        "overlay_assets_current",
                        extra={
                            "hud_assets_version": loaded,
                            "hud_page_id": connection.page_id or "unreported",
                        },
                    )
            case "client_log":
                logger.warning(
                    "hud_client_log",
                    extra={
                        "hud_client_message": (message.message or "")[
                            :_CLIENT_LOG_LIMIT
                        ]
                    },
                )
            case unsupported:
                connection.reply(
                    ErrorMessage(
                        detail=f"unsupported message type: {unsupported}"
                    ).model_dump_json()
                )


def _request_reload(
    connection: HudConnection, hello: ClientMessage, assets_version: str
) -> None:
    """Send a stale page to the current build, unless it has been sent enough.

    Immediately, not when the floor is next idle. A page only reports another
    build after it has outlived the CaddieJ that served it, so the state it is
    drawing is already a new process's, and it gets all of that back in the
    first snapshot after the reload. Waiting would only mean drawing the
    coming shot with the renderer that cannot show it.
    """

    url = assets_reload_url(assets_version, hello.reload_for, hello.reload_attempt)
    detail = {
        "hud_assets_version": (hello.assets_version or "unreported")[:64],
        "hud_assets_expected": assets_version,
        "hud_assets_reload_attempt": (hello.reload_attempt or "")[:16],
    }
    if url is None:
        logger.error("overlay_assets_reload_exhausted", extra=detail)
        return
    connection.reply(
        ReloadMessage(hud_assets_version=assets_version, url=url).model_dump_json()
    )
    logger.warning("overlay_assets_reload_requested", extra=detail)


HudServerError = AsgiServerError
"""Kept as a name because callers catch it; the behaviour lives in `asgi`."""


class HudServer(LocalAsgiServer):
    """Owns the uvicorn server for the HUD application."""

    def __init__(self, app: FastAPI, host: str, port: int) -> None:
        super().__init__(app, host=host, port=port, name="HUD server")
