"""HUD transport: static overlay files plus the floor state feed.

FastAPI and uvicorn are used purely as transport here. Routes hold no logic;
they delegate to `FloorService` and the connection registry, and no framework
type escapes this module.
"""

import asyncio
import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from caddiej.adapters.asgi import AsgiServerError, LocalAsgiServer
from caddiej.adapters.hud.connections import ConnectionRegistry, HudConnection
from caddiej.adapters.hud.protocol import (
    PROTOCOL_VERSION,
    ClientMessage,
    ErrorMessage,
    PongMessage,
    hello_message,
    snapshot_message,
)
from caddiej.floor.service import FloorService

logger = logging.getLogger(__name__)

STATIC_ROOT = Path(__file__).parent / "static"

# How long to let a queued snapshot finish sending after the client disconnects.
_SEND_DRAIN_TIMEOUT = 5.0

# Client-supplied log text is untrusted and unbounded; only a useful prefix is
# worth putting in CaddieJ's logs.
_CLIENT_LOG_LIMIT = 500


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


def create_app(
    service: FloorService,
    registry: ConnectionRegistry,
    subsystems: Mapping[str, HealthProbe] | None = None,
) -> FastAPI:
    """Build the ASGI application for the floor overlay."""

    app = FastAPI(title="CaddieJ HUD", docs_url=None, redoc_url=None, openapi_url=None)
    probes = dict(subsystems or {})

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        state = service.state
        report: dict[str, object] = {
            "status": "ok",
            "protocol_version": PROTOCOL_VERSION,
            "state_version": state.version,
            "mode": state.mode.value,
            "clients": registry.count,
        }
        for name, probe in probes.items():
            report[name] = dict(probe())
        return report

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_ROOT / "index.html")

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
        logger.info("hud_client_connected", extra={"hud_clients": registry.count})
        try:
            await websocket.send_text(hello_message().model_dump_json())
            await websocket.send_text(snapshot_message(service.state).model_dump_json())
            await _serve_connection(websocket, connection, service)
        except WebSocketDisconnect:
            pass
        finally:
            registry.unregister(connection)
            logger.info(
                "hud_client_disconnected",
                extra={
                    "hud_clients": registry.count,
                    "hud_dropped": connection.dropped,
                },
            )

    app.mount("/static", StaticFiles(directory=STATIC_ROOT), name="static")
    return app


async def _serve_connection(
    websocket: WebSocket,
    connection: HudConnection,
    service: FloorService,
) -> None:
    """Send state and handle client control messages until the client goes away.

    The receive loop detects the disconnect; closing the connection then lets the
    send loop finish by itself. Nothing is cancelled, which keeps the ASGI task's
    completion clean.
    """

    sender = asyncio.create_task(_send_loop(websocket, connection), name="hud-send")
    try:
        await _receive_loop(websocket, service)
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
    while True:
        state = await connection.next_state()
        if state is None:
            return
        await websocket.send_text(snapshot_message(state).model_dump_json())


async def _receive_loop(websocket: WebSocket, service: FloorService) -> None:
    while True:
        # `receive` rather than `receive_text`, because the latter assumes the
        # frame carries text and raises KeyError on a binary one. Any local page
        # can open this socket, so the frame type is not ours to assume.
        frame = await websocket.receive()
        if frame["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(frame.get("code", 1005), frame.get("reason"))

        raw = frame.get("text")
        if raw is None:
            await websocket.send_text(
                ErrorMessage(detail="only text frames are accepted").model_dump_json()
            )
            continue

        try:
            message = ClientMessage.model_validate_json(raw)
        except ValidationError:
            await websocket.send_text(
                ErrorMessage(detail="malformed message").model_dump_json()
            )
            continue

        match message.type:
            case "resync":
                await websocket.send_text(
                    snapshot_message(service.state).model_dump_json()
                )
            case "ping":
                await websocket.send_text(PongMessage().model_dump_json())
            case "hello":
                logger.info(
                    "hud_client_hello",
                    extra={"hud_surface": message.surface or "unknown"},
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
                await websocket.send_text(
                    ErrorMessage(
                        detail=f"unsupported message type: {unsupported}"
                    ).model_dump_json()
                )


HudServerError = AsgiServerError
"""Kept as a name because callers catch it; the behaviour lives in `asgi`."""


class HudServer(LocalAsgiServer):
    """Owns the uvicorn server for the HUD application."""

    def __init__(self, app: FastAPI, host: str, port: int) -> None:
        super().__init__(app, host=host, port=port, name="HUD server")
