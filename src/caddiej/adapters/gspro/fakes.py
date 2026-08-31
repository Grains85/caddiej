"""A fake simulator and a fake launch monitor.

These ship inside the adapter rather than in the test suite because they are the
substitute for real hardware during development, not merely test fixtures: the
whole GSPro integration is built and reviewed against them before a real GSPro
or a real ProTee VX is involved.

They are deliberately hostile. A launch monitor on someone's machine will split
messages across packets, repeat itself, send a zero-filled data block on every
heartbeat, and occasionally emit something that is not JSON at all. Anything the
real world does, these can do on demand.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from caddiej.adapters.gspro.framing import JsonFramer
from caddiej.adapters.gspro.interposer import StreamPair

# --- in-memory streams ----------------------------------------------------


class _MemoryWriter:
    """Writes into the reader at the other end of an in-memory connection."""

    def __init__(self, peer: asyncio.StreamReader) -> None:
        self._peer = peer
        self._closed = False
        self.written = bytearray()

    def write(self, data: bytes) -> None:
        if self._closed:
            return
        self.written.extend(data)
        self._peer.feed_data(bytes(data))

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Feeding EOF is what makes the far end's read() return b"", which is
        # how a real socket close is observed.
        with suppress(RuntimeError):
            self._peer.feed_eof()

    async def wait_closed(self) -> None:
        return None

    @property
    def is_closed(self) -> bool:
        return self._closed


@dataclass
class MemoryConnection:
    """Two ends of one in-memory connection, usable as `StreamPair`s."""

    left: StreamPair
    right: StreamPair
    left_writer: _MemoryWriter
    right_writer: _MemoryWriter

    @property
    def sent_by_left(self) -> bytes:
        return bytes(self.left_writer.written)

    @property
    def sent_by_right(self) -> bytes:
        return bytes(self.right_writer.written)


def memory_connection(name: str = "memory") -> MemoryConnection:
    """Create a bidirectional in-memory connection.

    Tests use this instead of sockets so the whole interposer, including its
    lifecycle and reconnect behaviour, is exercised without binding a port.
    """

    left_reader = asyncio.StreamReader()
    right_reader = asyncio.StreamReader()
    left_writer = _MemoryWriter(right_reader)
    right_writer = _MemoryWriter(left_reader)
    return MemoryConnection(
        left=StreamPair(reader=left_reader, writer=left_writer, peer=f"{name}:left"),
        right=StreamPair(
            reader=right_reader, writer=right_writer, peer=f"{name}:right"
        ),
        left_writer=left_writer,
        right_writer=right_writer,
    )


# --- launch monitor payloads ----------------------------------------------


def ball_data(
    speed: float = 148.2,
    hla: float = 1.5,
    vla: float = 13.4,
    total_spin: float | None = 2750.0,
    spin_axis: float | None = -4.2,
    carry: float | None = 268.4,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "Speed": speed,
        "SpinAxis": spin_axis,
        "TotalSpin": total_spin,
        "HLA": hla,
        "VLA": vla,
    }
    if carry is not None:
        payload["CarryDistance"] = carry
    return payload


def club_data(
    speed: float = 104.5,
    angle_of_attack: float = -1.8,
    face_to_target: float = 1.2,
    path: float = -2.4,
) -> dict[str, Any]:
    return {
        "Speed": speed,
        "AngleOfAttack": angle_of_attack,
        "FaceToTarget": face_to_target,
        "Lie": 0.0,
        "Loft": 10.5,
        "Path": path,
        "SpeedAtImpact": speed,
        "VerticalFaceImpact": 2.0,
        "HorizontalFaceImpact": -3.0,
        "ClosureRate": 0.0,
    }


def shot_message(
    shot_number: int,
    *,
    device_id: str = "CaddieJ Fake LM",
    units: str = "Yards",
    with_ball: bool = True,
    with_club: bool = True,
    ball: dict[str, Any] | None = None,
    club: dict[str, Any] | None = None,
    ready: bool = True,
    ball_detected: bool | None = False,
) -> dict[str, Any]:
    """A shot as a launch monitor sends it."""

    options: dict[str, Any] = {
        "ContainsBallData": with_ball,
        "ContainsClubData": with_club,
        "LaunchMonitorIsReady": ready,
        "IsHeartBeat": False,
    }
    if ball_detected is not None:
        options["LaunchMonitorBallDetected"] = ball_detected

    message: dict[str, Any] = {
        "DeviceID": device_id,
        "Units": units,
        "ShotNumber": shot_number,
        "APIversion": "1",
        "ShotDataOptions": options,
    }
    # Real devices include both blocks and use the flags to say which matter,
    # so the fake does too.
    message["BallData"] = ball if ball is not None else ball_data()
    message["ClubData"] = club if club is not None else club_data()
    if not with_ball and ball is None:
        message["BallData"] = ball_data(
            speed=0.0, hla=0.0, vla=0.0, total_spin=0.0, spin_axis=0.0, carry=None
        )
    if not with_club and club is None:
        message["ClubData"] = club_data(
            speed=0.0, angle_of_attack=0.0, face_to_target=0.0, path=0.0
        )
    return message


def heartbeat(
    *,
    device_id: str = "CaddieJ Fake LM",
    ready: bool = True,
    ball_detected: bool | None = None,
    shot_number: int = 0,
) -> dict[str, Any]:
    """A heartbeat, carrying zero-filled data blocks as real devices do."""

    options: dict[str, Any] = {
        "ContainsBallData": False,
        "ContainsClubData": False,
        "LaunchMonitorIsReady": ready,
        "IsHeartBeat": True,
    }
    if ball_detected is not None:
        options["LaunchMonitorBallDetected"] = ball_detected
    return {
        "DeviceID": device_id,
        "Units": "Yards",
        "ShotNumber": shot_number,
        "APIversion": "1",
        "BallData": ball_data(
            speed=0.0, hla=0.0, vla=0.0, total_spin=0.0, spin_axis=0.0, carry=None
        ),
        "ClubData": club_data(
            speed=0.0, angle_of_attack=0.0, face_to_target=0.0, path=0.0
        ),
        "ShotDataOptions": options,
    }


def player_information(handed: str = "RH", club: str = "DR") -> dict[str, Any]:
    """GSPro's 201 response: a Player block, which 200 may also carry."""

    return {
        "Code": 201,
        "Message": "GSPro Player Information",
        "Player": {"Handed": handed, "Club": club},
    }


def shot_acknowledgement(
    handed: str | None = None, club: str | None = None
) -> dict[str, Any]:
    """GSPro's 200 acknowledgement, optionally carrying a Player block."""

    payload: dict[str, Any] = {
        "Code": 200,
        "Message": "Shot received successfully",
    }
    if handed is not None or club is not None:
        player: dict[str, Any] = {}
        if handed is not None:
            player["Handed"] = handed
        if club is not None:
            player["Club"] = club
        payload["Player"] = player
    return payload


def simulator_failure(
    code: int = 501, message: str = "Failure occurred"
) -> dict[str, Any]:
    return {"Code": code, "Message": message}


def encode(*messages: dict[str, Any]) -> bytes:
    """Serialise messages the way a device does: back to back, no delimiter."""

    return b"".join(json.dumps(message).encode() for message in messages)


# Payloads that frame as a message but cannot be understood, whichever
# direction they travel in. A payload that is valid JSON but wrong for its
# direction — `{"Code": "abc"}` from a device — is not malformed here: it is a
# message that direction's schema rejects.
MALFORMED_PAYLOADS: tuple[bytes, ...] = (
    b"{not json at all}",
    b'{"DeviceID": "x", "ShotNumber": }',
    b'{"BallData": {"Speed": "fast", "HLA": 0.0, "VLA": 1.0}}',
    b'{"DeviceID": \xff\xfe}',  # invalid UTF-8 where a value belongs
)

# Bytes containing no JSON object at all. These never become a message, so they
# are skipped during framing rather than rejected during validation.
JUNK_PAYLOADS: tuple[bytes, ...] = (
    b"\xff\xfe\x00 garbage",
    b"\r\n\r\n",
    b"}}} stray",
)


# --- fake launch monitor --------------------------------------------------


class FakeLaunchMonitor:
    """Sends launch monitor traffic into a connection, badly on request."""

    def __init__(self, stream: StreamPair, device_id: str = "CaddieJ Fake LM") -> None:
        self._stream = stream
        self.device_id = device_id
        self._shot_number = 0

    @property
    def last_shot_number(self) -> int:
        return self._shot_number

    def reset_shot_numbers(self) -> None:
        """Simulate a device restart, which starts counting from one again."""

        self._shot_number = 0

    async def send_raw(self, payload: bytes, chunk_size: int | None = None) -> None:
        """Write bytes, optionally split so no message arrives whole."""

        if chunk_size is None:
            self._stream.writer.write(payload)
            await self._stream.writer.drain()
            return
        for start in range(0, len(payload), chunk_size):
            self._stream.writer.write(payload[start : start + chunk_size])
            await self._stream.writer.drain()
            await asyncio.sleep(0)

    async def send(
        self, *messages: dict[str, Any], chunk_size: int | None = None
    ) -> None:
        await self.send_raw(encode(*messages), chunk_size=chunk_size)

    async def send_heartbeat(
        self, *, ready: bool = True, ball_detected: bool | None = None
    ) -> None:
        await self.send(
            heartbeat(
                device_id=self.device_id, ready=ready, ball_detected=ball_detected
            )
        )

    async def hit_shot(
        self,
        *,
        with_ball: bool = True,
        with_club: bool = True,
        ball: dict[str, Any] | None = None,
        club: dict[str, Any] | None = None,
        units: str = "Yards",
    ) -> int:
        """Send a complete shot and return the shot number used."""

        self._shot_number += 1
        await self.send(
            shot_message(
                self._shot_number,
                device_id=self.device_id,
                units=units,
                with_ball=with_ball,
                with_club=with_club,
                ball=ball,
                club=club,
            )
        )
        return self._shot_number

    async def repeat_last_shot(self) -> None:
        """Retransmit the previous shot, as happens after a device reconnect."""

        await self.send(shot_message(self._shot_number, device_id=self.device_id))

    async def send_malformed(self, payload: bytes | None = None) -> None:
        await self.send_raw(payload if payload is not None else MALFORMED_PAYLOADS[0])

    async def disconnect(self) -> None:
        await self._stream.aclose()


# --- fake simulator -------------------------------------------------------

Responder = Callable[[dict[str, Any]], Sequence[dict[str, Any]]]


def acknowledge_shots(message: dict[str, Any]) -> Sequence[dict[str, Any]]:
    """Default behaviour: acknowledge anything that is not a heartbeat."""

    options = message.get("ShotDataOptions") or {}
    if options.get("IsHeartBeat"):
        return ()
    if options.get("ContainsBallData") or options.get("ContainsClubData"):
        return (shot_acknowledgement(),)
    return ()


@dataclass
class FakeGSProServer:
    """A stand-in for GSPro's Open Connect listener, over a real socket.

    It can behave, misbehave, or refuse to exist, which is what makes the
    adapter's lifecycle testable without installing a simulator.
    """

    host: str = "127.0.0.1"
    port: int = 0
    responder: Responder = acknowledge_shots
    received: list[dict[str, Any]] = field(default_factory=list)
    received_bytes: bytearray = field(default_factory=bytearray)
    connections: int = 0
    _server: asyncio.Server | None = None
    _clients: list[asyncio.StreamWriter] = field(default_factory=list)
    _connected: asyncio.Event = field(default_factory=asyncio.Event)

    async def start(self) -> "FakeGSProServer":
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        sockets = self._server.sockets or ()
        if sockets:
            self.port = sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        for writer in tuple(self._clients):
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
        self._clients.clear()
        if self._server is not None:
            self._server.close()
            with suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    async def __aenter__(self) -> "FakeGSProServer":
        return await self.start()

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop()

    async def wait_for_connection(self, timeout: float = 5.0) -> None:
        await asyncio.wait_for(self._connected.wait(), timeout)

    async def push(self, *messages: dict[str, Any]) -> None:
        """Send unprompted messages, which is how club changes actually arrive."""

        payload = encode(*messages)
        for writer in tuple(self._clients):
            writer.write(payload)
            with suppress(Exception):
                await writer.drain()

    async def drop_connections(self) -> None:
        """Simulate the simulator vanishing mid-session."""

        for writer in tuple(self._clients):
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
        self._clients.clear()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connections += 1
        self._clients.append(writer)
        self._connected.set()
        framer = JsonFramer()
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    return
                self.received_bytes.extend(chunk)
                for frame in framer.feed(chunk):
                    try:
                        message = json.loads(frame.decode("utf-8", errors="replace"))
                    except ValueError:
                        continue
                    if not isinstance(message, dict):
                        continue
                    self.received.append(message)
                    for response in self.responder(message):
                        writer.write(encode(response))
                        await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            return
        finally:
            if writer in self._clients:
                self._clients.remove(writer)
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()


# --- listeners for tests --------------------------------------------------


class ScriptedListener:
    """Hands prepared connections to the adapter instead of accepting sockets."""

    def __init__(self, connections: Iterable[StreamPair] = ()) -> None:
        self._incoming: asyncio.Queue[StreamPair] = asyncio.Queue()
        for connection in connections:
            self._incoming.put_nowait(connection)
        self.handled = 0
        self.serving = asyncio.Event()

    def offer(self, connection: StreamPair) -> None:
        self._incoming.put_nowait(connection)

    async def serve(self, handle: Callable[[StreamPair], Awaitable[None]]) -> None:
        self.serving.set()
        # A task per connection, as a real listener does. Handling them in
        # sequence would hide the case of a second device connecting while one
        # is already bridged.
        async with asyncio.TaskGroup() as group:
            while True:
                connection = await self._incoming.get()
                self.handled += 1

                async def run(held: StreamPair = connection) -> None:
                    await handle(held)

                group.create_task(run())


class FailingListener:
    """A listener that cannot bind, which is what a busy port looks like."""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def serve(self, handle: Callable[[StreamPair], Awaitable[None]]) -> None:
        raise self._error
