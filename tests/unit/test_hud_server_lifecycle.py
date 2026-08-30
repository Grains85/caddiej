import asyncio
import signal

import pytest

from caddiej.adapters.hud import ConnectionRegistry, HudServer, create_app
from caddiej.adapters.hud.server import HudServerError
from caddiej.floor import FloorService


def build_server(port: int = 0) -> HudServer:
    service = FloorService()
    registry = ConnectionRegistry()
    service.add_listener(registry.broadcast)
    return HudServer(create_app(service, registry), host="127.0.0.1", port=port)


async def test_bind_failure_is_reported_as_itself_not_as_a_timeout() -> None:
    server = build_server()

    async def failing_serve() -> None:
        raise OSError(48, "address already in use")

    server._server.serve = failing_serve  # type: ignore[method-assign]
    task = asyncio.create_task(server.serve())

    with pytest.raises(HudServerError) as failure:
        await server.wait_until_serving(5)

    assert isinstance(failure.value.__cause__, OSError)
    await asyncio.gather(task, return_exceptions=True)


async def test_a_uvicorn_exit_becomes_an_error_rather_than_a_process_exit() -> None:
    """How uvicorn actually reports a port it could not bind.

    It calls `sys.exit`, and `SystemExit` is a BaseException: left alone it
    walks through every `except Exception` between here and the event loop.
    Both of CaddieJ's local servers depend on this being translated, the
    assistant API most of all, since its whole promise is that it cannot take
    the simulator down with it.
    """

    server = build_server()

    async def exiting_serve() -> None:
        raise SystemExit(3)

    server._server.serve = exiting_serve  # type: ignore[method-assign]
    task = asyncio.create_task(server.serve())

    with pytest.raises(HudServerError, match="could not bind"):
        await task

    with pytest.raises(HudServerError):
        await server.wait_until_serving(5)


async def test_startup_timeout_is_reported_when_the_server_never_binds() -> None:
    server = build_server()

    async def hanging_serve() -> None:
        await asyncio.Event().wait()

    server._server.serve = hanging_serve  # type: ignore[method-assign]
    task = asyncio.create_task(server.serve())

    with pytest.raises(HudServerError, match="did not start"):
        await server.wait_until_serving(0.05)

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_port_is_unavailable_before_the_socket_is_bound() -> None:
    with pytest.raises(RuntimeError):
        build_server().port


async def test_serving_does_not_replace_the_runtime_signal_handlers() -> None:
    # uvicorn would otherwise install its own SIGINT and SIGTERM handlers and
    # shut down without the rest of CaddieJ noticing.
    server = build_server()
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    with server._server.capture_signals():
        during = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    assert during == before
