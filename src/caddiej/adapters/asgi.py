"""One loopback ASGI server, shared by the surfaces that need one.

CaddieJ serves two local HTTP surfaces — the floor overlay and the assistant
API — and they need identical treatment in one respect that is easy to get
wrong: uvicorn installs its own SIGINT and SIGTERM handlers inside `serve()`,
which would replace the runtime's and shut a server down without the rest of
CaddieJ learning about it. That is the kind of subtlety that must exist once. Two
copies means one of them gets fixed and the other quietly does not, and the
symptom is a shutdown that skips half of itself.

So this module owns starting, waiting for a bound socket, and stopping. It owns
no routes and knows nothing about either surface.
"""

import asyncio
import contextlib
import logging
from collections.abc import Generator

import uvicorn
from fastapi import FastAPI

logger = logging.getLogger(__name__)

# Detect a peer that has stopped acknowledging without closing, such as a
# suspended Browser Source or a machine that went away. Set explicitly rather
# than inherited, because this is the only thing reaping half-open clients.
_WS_PING_INTERVAL = 20.0
_WS_PING_TIMEOUT = 20.0


class AsgiServerError(RuntimeError):
    """Raised when a local server cannot start."""


class _RuntimeOwnedSignalsServer(uvicorn.Server):
    """A uvicorn server that leaves signal handling to CaddieJ's runtime."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


class LocalAsgiServer:
    """Owns the uvicorn server for one local surface."""

    def __init__(
        self,
        app: FastAPI,
        host: str,
        port: int,
        name: str = "server",
    ) -> None:
        config = uvicorn.Config(
            app=app,
            host=host,
            port=port,
            log_config=None,
            access_log=False,
            # Lifespan stays on so that tests, which always run it, exercise the
            # same startup path as the real server.
            lifespan="on",
            ws_ping_interval=_WS_PING_INTERVAL,
            ws_ping_timeout=_WS_PING_TIMEOUT,
        )
        self._name = name
        self._address = f"{host}:{port}"
        self._server = _RuntimeOwnedSignalsServer(config)
        self._error: BaseException | None = None

    @property
    def port(self) -> int:
        """The bound port, which differs from the configured one when it was 0."""

        for server in getattr(self._server, "servers", ()):
            for socket in server.sockets:
                return int(socket.getsockname()[1])
        raise RuntimeError(f"{self._name} is not listening")

    async def serve(self) -> None:
        try:
            await self._server.serve()
        except SystemExit as error:
            # uvicorn reports a port it could not bind by calling `sys.exit`,
            # and `SystemExit` is a BaseException. Left alone it walks through
            # every `except Exception` between here and the event loop, so a
            # port already in use takes the whole of CaddieJ down — including
            # for the assistant API, whose entire promise is that it cannot.
            # Translated rather than re-raised, because it is a start-up
            # failure and saying so is this object's job.
            self._error = AsgiServerError(
                f"{self._name} could not bind {self._address}"
            )
            raise self._error from error
        except Exception as error:
            # Deliberately not BaseException: a cancellation is not a start-up
            # failure and must not be reported as one.
            self._error = error
            raise

    async def wait_until_serving(self, timeout: float) -> None:
        """Block until the socket is accepting connections.

        A failure to bind is reported as itself rather than as a timeout, so a
        port already in use does not look like a hung startup.
        """

        try:
            async with asyncio.timeout(timeout):
                while not self._server.started:
                    if self._error is not None:
                        raise AsgiServerError(
                            f"{self._name} failed to start"
                        ) from self._error
                    await asyncio.sleep(0.01)
        except TimeoutError as error:
            raise AsgiServerError(
                f"{self._name} did not start within {timeout} seconds"
            ) from error

    async def stop(self) -> None:
        """Ask uvicorn to shut down gracefully; the caller awaits `serve()`."""

        self._server.should_exit = True
