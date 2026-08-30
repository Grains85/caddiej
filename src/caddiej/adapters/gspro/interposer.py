"""The byte path between a launch monitor and the simulator.

GSPro Open Connect is a feed *into* GSPro: the simulator listens, the launch
monitor connects and sends shots, and the only thing coming back is an
acknowledgement or player information. There is no observer interface, so to see
shots at all CaddieJ has to sit in the conversation: it accepts the launch
monitor's connection, dials the simulator itself, and forwards verbatim.

That puts CaddieJ in the path of live play, which sets three hard rules:

- **Bytes are forwarded before they are understood.** Parsing, publishing, and
  every other thing CaddieJ wants to do happens strictly after the data is on
  its way. Nothing CaddieJ does can add latency to a shot.
- **Bytes are never modified, delayed, or synthesised.** CaddieJ is a witness,
  not a participant.
- **A simulator that cannot be reached is not concealed.** The launch monitor's
  connection is closed rather than accepting shots into a void, so the device's
  own reconnect logic sees exactly what it would see if CaddieJ were absent.
"""

import asyncio
import logging
import socket
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from caddiej.adapters.gspro.errors import GSProBindError, GSProConnectionError
from caddiej.adapters.gspro.framing import JsonFramer
from caddiej.domain.events import LinkState
from caddiej.runtime.backoff import Backoff

logger = logging.getLogger(__name__)

_READ_SIZE = 65536


class Direction(StrEnum):
    TO_SIMULATOR = "to_simulator"
    FROM_SIMULATOR = "from_simulator"


class ByteReader(Protocol):
    async def read(self, size: int) -> bytes: ...


class ByteWriter(Protocol):
    def write(self, data: bytes) -> None: ...

    async def drain(self) -> None: ...

    def close(self) -> None: ...

    async def wait_closed(self) -> None: ...


@dataclass(frozen=True, slots=True)
class StreamPair:
    """One connection, as the interposer needs to see it."""

    reader: ByteReader
    writer: ByteWriter
    peer: str = "unknown"

    async def aclose(self) -> None:
        self.writer.close()
        # A peer that has already vanished makes closing raise; the connection
        # is going away either way.
        with suppress(Exception):
            await self.writer.wait_closed()


FrameObserver = Callable[[Direction, bytes], None]
LinkObserver = Callable[[LinkState, str | None], None]
ConnectSimulator = Callable[[], Awaitable[StreamPair]]


class MonitorListener(Protocol):
    """Accepts launch monitor connections and hands them to a handler."""

    async def serve(self, handle: Callable[[StreamPair], Awaitable[None]]) -> None: ...


class TcpMonitorListener:
    """Listens on a real TCP socket for the launch monitor connector."""

    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._bound_port: int | None = None
        self.serving = asyncio.Event()

    @property
    def bound_port(self) -> int | None:
        return self._bound_port

    async def serve(self, handle: Callable[[StreamPair], Awaitable[None]]) -> None:
        # Each connection is served on a task this listener owns, rather than in
        # asyncio's own connection callback. Closing a server waits for those
        # callbacks to return, and a callback bridging a launch monitor returns
        # when the device hangs up — which during shutdown it has no reason to
        # do. Owning the task means shutdown can end the session instead of
        # waiting for the player to unplug something.
        sessions: set[asyncio.Task[None]] = set()

        async def on_connection(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            _disable_nagle(writer)
            peer = _describe_peer(writer)

            async def run_session() -> None:
                await handle(StreamPair(reader=reader, writer=writer, peer=peer))

            session: asyncio.Task[None] = asyncio.create_task(
                run_session(),
                name="gspro-monitor-session",
            )
            sessions.add(session)
            session.add_done_callback(sessions.discard)

        try:
            server = await asyncio.start_server(on_connection, self._host, self._port)
        except OSError as error:
            raise GSProBindError(
                f"Could not listen on {self._host}:{self._port}: {error}"
            ) from error

        sockets = server.sockets or ()
        if sockets:
            self._bound_port = sockets[0].getsockname()[1]
        self.serving.set()
        try:
            # Deliberately not `server.serve_forever()`. Cancelling that closes
            # the server and then waits for every open connection to end before
            # it returns, and ending those connections is precisely what this
            # listener has to do first. Waiting to be cancelled instead keeps
            # the shutdown order under our control. The server is already
            # accepting; `serve_forever` adds nothing but that trap.
            await asyncio.Event().wait()
        finally:
            self.serving.clear()
            await _close_server(server, sessions)


async def _close_server(
    server: asyncio.Server, sessions: set[asyncio.Task[None]]
) -> None:
    """Stop listening and end every connection being served."""

    server.close()
    pending = tuple(sessions)
    for session in pending:
        session.cancel()
    # Sessions are gathered rather than abandoned so their own cleanup — closing
    # the two sockets a bridge holds — has run before shutdown continues.
    await asyncio.gather(*pending, return_exceptions=True)
    with suppress(Exception):
        await server.wait_closed()


async def connect_to_simulator(host: str, port: int, timeout: float) -> StreamPair:
    """Open a connection to the simulator's Open Connect port."""

    try:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_connection(host, port)
    except TimeoutError as error:
        raise GSProConnectionError(
            f"Timed out connecting to the simulator at {host}:{port}"
        ) from error
    except OSError as error:
        raise GSProConnectionError(
            f"Could not connect to the simulator at {host}:{port}: {error}"
        ) from error

    _disable_nagle(writer)
    return StreamPair(reader=reader, writer=writer, peer=f"{host}:{port}")


def _disable_nagle(writer: asyncio.StreamWriter) -> None:
    # Nagle would hold a small shot message waiting for more data to batch. On
    # a path a player is standing in front of, that delay is unacceptable.
    raw = writer.get_extra_info("socket")
    if raw is not None:
        with suppress(OSError):
            raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)


def _describe_peer(writer: asyncio.StreamWriter) -> str:
    peer = writer.get_extra_info("peername")
    if isinstance(peer, tuple) and len(peer) >= 2:
        return f"{peer[0]}:{peer[1]}"
    return "unknown"


class _Activity:
    """When either direction of a bridged session last carried anything."""

    __slots__ = ("at",)

    def __init__(self, at: float) -> None:
        self.at = at


class Interposer:
    """Bridges one launch monitor connection to the simulator at a time."""

    def __init__(
        self,
        connect_simulator: ConnectSimulator,
        on_frame: FrameObserver,
        on_link: LinkObserver,
        max_message_bytes: int = 65536,
        backoff: Backoff | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        idle_timeout: float = 180.0,
    ) -> None:
        self._connect_simulator = connect_simulator
        self._on_frame = on_frame
        self._on_link = on_link
        self._max_message_bytes = max_message_bytes
        self._backoff = backoff or Backoff()
        self._monotonic = monotonic
        self._idle_timeout = idle_timeout or None
        self._busy = False
        self._cooldown_until = 0.0

    @property
    def is_bridging(self) -> bool:
        return self._busy

    async def bridge(self, downstream: StreamPair) -> None:
        """Serve one launch monitor connection until either side closes."""

        if self._busy:
            # Open Connect assumes a single device. Interleaving two shot
            # streams over one simulator connection would corrupt both.
            logger.warning(
                "gspro_monitor_rejected_already_bridging",
                extra={"gspro_peer": downstream.peer},
            )
            await downstream.aclose()
            return

        self._busy = True
        bridged = False
        try:
            self._on_link(LinkState.MONITOR_CONNECTED, downstream.peer)
            upstream = await self._open_simulator(downstream)
            if upstream is None:
                return
            try:
                bridged = True
                self._on_link(LinkState.BRIDGED, downstream.peer)
                await self._pump_both_ways(downstream, upstream)
            finally:
                await upstream.aclose()
        finally:
            self._busy = False
            await downstream.aclose()
            if bridged:
                # Only a session that actually bridged returns to listening.
                # Announcing it after a failed connection would immediately
                # paper over the fact that the simulator is unreachable, which
                # is the one thing worth telling someone about.
                self._on_link(LinkState.LISTENING, None)

    async def _open_simulator(self, downstream: StreamPair) -> StreamPair | None:
        """Dial the simulator, honouring a cooldown after a recent failure.

        The cooldown exists because the launch monitor controls reconnect
        timing: a device retrying twice a second would otherwise have CaddieJ
        hammering a simulator that is not running.
        """

        remaining = self._cooldown_until - self._monotonic()
        if remaining > 0:
            self._on_link(
                LinkState.SIMULATOR_UNAVAILABLE,
                f"waiting {remaining:.1f}s before retrying the simulator",
            )
            logger.info(
                "gspro_simulator_cooldown",
                extra={"gspro_backoff_seconds": round(remaining, 3)},
            )
            return None

        try:
            upstream = await self._connect_simulator()
        except GSProConnectionError as error:
            delay = self._backoff.next_delay()
            self._cooldown_until = self._monotonic() + delay
            self._on_link(LinkState.SIMULATOR_UNAVAILABLE, str(error))
            logger.warning(
                "gspro_simulator_unreachable",
                extra={"gspro_backoff_seconds": round(delay, 3)},
            )
            return None

        self._backoff.reset()
        self._cooldown_until = 0.0
        return upstream

    async def _pump_both_ways(
        self, downstream: StreamPair, upstream: StreamPair
    ) -> None:
        # Shared, because liveness belongs to the session rather than to a
        # direction: a simulator only speaks when it is spoken to, so silence
        # from it while the device is heartbeating is a working bridge.
        activity = _Activity(self._monotonic())
        pumps = (
            asyncio.create_task(
                self._pump(downstream, upstream, Direction.TO_SIMULATOR, activity),
                name="gspro-monitor-to-simulator",
            ),
            asyncio.create_task(
                self._pump(upstream, downstream, Direction.FROM_SIMULATOR, activity),
                name="gspro-simulator-to-monitor",
            ),
        )
        try:
            # Either side closing ends the session: a half-open bridge would
            # silently swallow shots.
            done, pending = await asyncio.wait(
                pumps, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                error = task.exception()
                if error is not None:
                    logger.warning(
                        "gspro_pump_failed",
                        extra={"task_name": task.get_name()},
                        exc_info=error,
                    )
        except asyncio.CancelledError:
            for task in pumps:
                task.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            raise

    async def _pump(
        self,
        source: StreamPair,
        sink: StreamPair,
        direction: Direction,
        activity: "_Activity",
    ) -> None:
        """Forward one direction of a bridged session until the session dies.

        Both awaits are bounded. A TCP connection whose peer disappeared without
        closing — a laptop lid, a dropped wireless link, a killed process on
        another machine — stays readable forever from this side, and CaddieJ
        bridges one device at a time, so a single one of those would refuse every
        later connection for the rest of the evening. Returning ends the session,
        which closes both legs and goes back to listening, and a device that is
        actually still there simply reconnects.

        What is bounded is the session going quiet, not this direction going
        quiet. Detection therefore takes up to twice the timeout, which is the
        right trade: the cost of noticing late is a slow reconnect, and the cost
        of noticing wrongly is hanging up on somebody mid-round.
        """

        framer = JsonFramer(self._max_message_bytes)
        while True:
            try:
                async with asyncio.timeout(self._idle_timeout):
                    chunk = await source.reader.read(_READ_SIZE)
            except TimeoutError:
                if self._monotonic() - activity.at < (self._idle_timeout or 0.0):
                    # The other direction is carrying traffic, so the link is
                    # there and this side simply has nothing to say.
                    continue
                logger.warning(
                    "gspro_link_idle",
                    extra={
                        "gspro_direction": direction.value,
                        "gspro_peer": source.peer,
                    },
                )
                return
            if not chunk:
                return
            activity.at = self._monotonic()

            # Forward first, always. Everything below this line is CaddieJ's
            # own business and must never delay the simulator or the device.
            sink.writer.write(chunk)
            try:
                async with asyncio.timeout(self._idle_timeout):
                    await sink.writer.drain()
            except TimeoutError:
                # The other side has stopped reading. Nothing CaddieJ can do
                # will make it start, and holding the bridge open would keep the
                # device that is still working from ever reconnecting.
                logger.warning(
                    "gspro_forward_stalled",
                    extra={
                        "gspro_direction": direction.value,
                        "gspro_peer": sink.peer,
                    },
                )
                return

            for frame in framer.feed(chunk):
                self._on_frame(direction, frame)
