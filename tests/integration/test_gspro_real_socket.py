"""The GSPro adapter end to end over real loopback sockets.

The unit tests drive the adapter through in-memory connections, which proves the
logic but not the plumbing. These prove the plumbing: a real listener, a real
connection to a stand-in simulator, and real TCP framing behaviour.

No external software is needed — the simulator here is CaddieJ's own fake — but
these bind ports, so they are gated like the other integration tests.
"""

import asyncio
import os

import pytest

from caddiej.adapters.gspro.adapter import GSProConnectAdapter
from caddiej.adapters.gspro.fakes import (
    FakeGSProServer,
    FakeLaunchMonitor,
    encode,
    heartbeat,
    player_information,
    shot_message,
)
from caddiej.adapters.gspro.interposer import (
    FrameObserver,
    Interposer,
    LinkObserver,
    StreamPair,
    TcpMonitorListener,
    connect_to_simulator,
)
from caddiej.core.events import Event
from caddiej.domain.events import (
    BallDetected,
    ClubSelected,
    LaunchMonitorLinkChanged,
    LinkState,
    ShotIngested,
)
from caddiej.domain.shot import ClubKind
from caddiej.runtime.backoff import Backoff

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("CADDIEJ_RUN_INTEGRATION") != "1",
        reason="set CADDIEJ_RUN_INTEGRATION=1 to bind a real loopback port",
    ),
]


class RealBridge:
    """A running adapter with a real listener and a real fake simulator."""

    def __init__(self, upstream_port: int) -> None:
        self.events: list[Event] = []
        self.listener = TcpMonitorListener("127.0.0.1", 0)
        self.adapter = GSProConnectAdapter(
            listener=self.listener,
            interposer_factory=self._interposer(upstream_port),
            publish=self._publish,
            assembly_window_seconds=0.05,
            ball_present_debounce_seconds=0.0,
            ball_absent_debounce_seconds=0.0,
        )
        self._task: asyncio.Task[None] | None = None

    def _interposer(self, upstream_port: int):
        def factory(on_frame: FrameObserver, on_link: LinkObserver) -> Interposer:
            async def connect() -> StreamPair:
                return await connect_to_simulator("127.0.0.1", upstream_port, 2.0)

            return Interposer(
                connect_simulator=connect,
                on_frame=on_frame,
                on_link=on_link,
                backoff=Backoff(0.05, 0.1, jitter=lambda: 1.0),
            )

        return factory

    async def _publish(self, event: Event) -> None:
        self.events.append(event)

    async def __aenter__(self) -> "RealBridge":
        self._task = asyncio.create_task(self.adapter.serve(), name="gspro-integration")
        await asyncio.wait_for(self.listener.serving.wait(), 5.0)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    @property
    def port(self) -> int:
        assert self.listener.bound_port is not None
        return self.listener.bound_port

    async def connect_monitor(self) -> tuple[FakeLaunchMonitor, StreamPair]:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        stream = StreamPair(reader=reader, writer=writer, peer="test-monitor")
        await self.wait_for_link(LinkState.BRIDGED)
        return FakeLaunchMonitor(stream), stream

    def of_type(self, event_type: type) -> list:
        return [event for event in self.events if isinstance(event, event_type)]

    async def expect(self, event_type: type, count: int = 1, timeout: float = 5.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            found = self.of_type(event_type)
            if len(found) >= count:
                return found[count - 1]
            if loop.time() >= deadline:
                seen = ", ".join(type(event).__name__ for event in self.events)
                raise AssertionError(f"no {event_type.__name__}. Saw: {seen}")
            await asyncio.sleep(0.01)

    async def wait_for_link(self, state: LinkState, timeout: float = 5.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if any(
                event.state is state for event in self.of_type(LaunchMonitorLinkChanged)
            ):
                return
            if loop.time() >= deadline:
                raise AssertionError(f"link never reached {state.value}")
            await asyncio.sleep(0.01)


async def test_a_real_device_connection_is_bridged_to_a_real_simulator() -> None:
    async with FakeGSProServer() as simulator:
        async with RealBridge(simulator.port) as bridge:
            monitor, stream = await bridge.connect_monitor()

            await monitor.hit_shot()
            shot = (await bridge.expect(ShotIngested)).shot

            assert shot.ball is not None
            assert shot.ball.speed_mph == 148.2
            # The simulator received the shot, which is the whole point: CaddieJ
            # observes without interrupting the feed GSPro depends on.
            await asyncio.sleep(0.1)
            assert any(message.get("ShotNumber") == 1 for message in simulator.received)
            stream.writer.close()


async def test_traffic_arrives_at_the_simulator_unmodified() -> None:
    async with FakeGSProServer() as simulator:
        async with RealBridge(simulator.port) as bridge:
            monitor, stream = await bridge.connect_monitor()

            payload = encode(heartbeat(), shot_message(1), heartbeat())
            await monitor.send_raw(payload)
            await bridge.expect(ShotIngested)
            await asyncio.sleep(0.1)

            assert bytes(simulator.received_bytes) == payload
            stream.writer.close()


async def test_a_shot_split_across_packets_is_still_understood() -> None:
    # Real TCP will split messages whenever it feels like it.
    async with FakeGSProServer() as simulator:
        async with RealBridge(simulator.port) as bridge:
            monitor, stream = await bridge.connect_monitor()

            await monitor.send(shot_message(1), chunk_size=3)
            shot = (await bridge.expect(ShotIngested)).shot

            assert shot.ball is not None
            stream.writer.close()


async def test_a_club_change_pushed_by_the_simulator_reaches_the_domain() -> None:
    async with FakeGSProServer() as simulator:
        async with RealBridge(simulator.port) as bridge:
            monitor, stream = await bridge.connect_monitor()
            await monitor.send_heartbeat()
            await simulator.wait_for_connection()

            await simulator.push(player_information("RH", "PT"))
            selected = await bridge.expect(ClubSelected)

            assert selected.club.kind is ClubKind.PUTTER
            stream.writer.close()


async def test_a_ball_detection_flag_survives_the_real_wire() -> None:
    async with FakeGSProServer() as simulator:
        async with RealBridge(simulator.port) as bridge:
            monitor, stream = await bridge.connect_monitor()

            await monitor.send_heartbeat(ball_detected=True)

            await bridge.expect(BallDetected)
            stream.writer.close()


async def test_a_device_disconnecting_and_returning_keeps_working() -> None:
    async with FakeGSProServer() as simulator:
        async with RealBridge(simulator.port) as bridge:
            monitor, stream = await bridge.connect_monitor()
            await monitor.hit_shot()
            await bridge.expect(ShotIngested)

            stream.writer.close()
            await asyncio.sleep(0.2)

            reconnected, second = await bridge.connect_monitor()
            reconnected.reset_shot_numbers()
            await reconnected.hit_shot()

            await bridge.expect(ShotIngested, count=2)
            assert simulator.connections == 2
            second.writer.close()


async def test_the_simulator_vanishing_closes_the_device_connection() -> None:
    # The device must see what it would see if CaddieJ were not there at all.
    async with FakeGSProServer() as simulator:
        async with RealBridge(simulator.port) as bridge:
            monitor, stream = await bridge.connect_monitor()
            await monitor.send_heartbeat()
            await simulator.wait_for_connection()

            await simulator.drop_connections()

            assert await asyncio.wait_for(stream.reader.read(1), 5.0) == b""


async def test_a_simulator_that_is_not_running_is_not_concealed() -> None:
    async with FakeGSProServer() as simulator:
        unused_port = simulator.port
    # The server is now stopped, so nothing is listening on that port.

    async with RealBridge(unused_port) as bridge:
        reader, writer = await asyncio.open_connection("127.0.0.1", bridge.port)

        await bridge.wait_for_link(LinkState.SIMULATOR_UNAVAILABLE)

        assert await asyncio.wait_for(reader.read(1), 5.0) == b""
        writer.close()


async def test_a_connected_device_does_not_stop_the_adapter_shutting_down() -> None:
    # A launch monitor left plugged in has no reason to hang up when CaddieJ
    # stops, so shutdown has to end the session rather than wait for it.
    async with FakeGSProServer() as simulator:
        bridge = RealBridge(simulator.port)
        await bridge.__aenter__()
        monitor, stream = await bridge.connect_monitor()
        await monitor.send_heartbeat()

        assert bridge._task is not None
        bridge._task.cancel()
        await asyncio.wait_for(
            asyncio.gather(bridge._task, return_exceptions=True), 5.0
        )

        stream.writer.close()


async def test_the_adapter_survives_a_device_that_vanishes_mid_message() -> None:
    async with FakeGSProServer() as simulator:
        async with RealBridge(simulator.port) as bridge:
            monitor, stream = await bridge.connect_monitor()

            await monitor.send_raw(b'{"DeviceID": "LM", "ShotNum')
            stream.writer.close()
            await asyncio.sleep(0.2)

            reconnected, second = await bridge.connect_monitor()
            await reconnected.hit_shot()

            await bridge.expect(ShotIngested)
            second.writer.close()
