"""The byte path: what CaddieJ does to traffic it sits in the middle of.

The invariants here are the ones that protect the player. CaddieJ is in the path
of live play, so it must forward everything, unmodified, before it thinks about
any of it, and it must not pretend to be a simulator it cannot reach.
"""

import asyncio

import pytest

from caddiej.adapters.gspro.errors import GSProConnectionError
from caddiej.adapters.gspro.fakes import (
    MALFORMED_PAYLOADS,
    encode,
    heartbeat,
    memory_connection,
    player_information,
    shot_message,
)
from caddiej.adapters.gspro.interposer import Direction, Interposer, StreamPair
from caddiej.domain.events import LinkState
from caddiej.runtime.backoff import Backoff

pytestmark = pytest.mark.asyncio


class Recorder:
    """Collects what the interposer reports, in order."""

    def __init__(self) -> None:
        self.frames: list[tuple[Direction, bytes]] = []
        self.links: list[tuple[LinkState, str | None]] = []

    def on_frame(self, direction: Direction, payload: bytes) -> None:
        self.frames.append((direction, payload))

    def on_link(self, state: LinkState, detail: str | None) -> None:
        self.links.append((state, detail))

    @property
    def states(self) -> list[LinkState]:
        return [state for state, _ in self.links]

    def frames_towards(self, direction: Direction) -> list[bytes]:
        return [payload for observed, payload in self.frames if observed is direction]


class Simulator:
    """Hands out a fresh in-memory connection per dial, or refuses to answer."""

    def __init__(self) -> None:
        self.links: list = []
        self.reachable = True
        self.dials = 0

    async def connect(self) -> StreamPair:
        self.dials += 1
        if not self.reachable:
            raise GSProConnectionError("the fake simulator is not running")
        link = memory_connection(f"simulator-{self.dials}")
        self.links.append(link)
        return link.left

    @property
    def current(self):
        return self.links[-1].right


def build(
    simulator: Simulator,
    recorder: Recorder,
    backoff: Backoff | None = None,
    idle_timeout: float = 0.0,
) -> Interposer:
    return Interposer(
        connect_simulator=simulator.connect,
        on_frame=recorder.on_frame,
        on_link=recorder.on_link,
        backoff=backoff or Backoff(0.01, 0.02, jitter=lambda: 1.0),
        idle_timeout=idle_timeout,
    )


async def settle(times: int = 6) -> None:
    """Let queued stream work run without depending on wall-clock timing."""

    for _ in range(times):
        await asyncio.sleep(0)


async def test_a_bridged_session_reports_connected_then_bridged() -> None:
    recorder = Recorder()
    simulator = Simulator()
    monitor = memory_connection("monitor")
    session = asyncio.create_task(build(simulator, recorder).bridge(monitor.right))

    await settle()

    assert recorder.states[:2] == [LinkState.MONITOR_CONNECTED, LinkState.BRIDGED]

    session.cancel()
    await asyncio.gather(session, return_exceptions=True)


async def test_device_traffic_reaches_the_simulator_byte_for_byte() -> None:
    # CaddieJ is a witness, not a participant: it must not reshape the stream.
    recorder = Recorder()
    simulator = Simulator()
    monitor = memory_connection("monitor")
    session = asyncio.create_task(build(simulator, recorder).bridge(monitor.right))
    await settle()

    payload = encode(heartbeat(), shot_message(1), heartbeat())
    monitor.left.writer.write(payload)
    await settle()

    assert simulator.links[0].sent_by_left == payload

    session.cancel()
    await asyncio.gather(session, return_exceptions=True)


async def test_simulator_replies_reach_the_device_byte_for_byte() -> None:
    recorder = Recorder()
    simulator = Simulator()
    monitor = memory_connection("monitor")
    session = asyncio.create_task(build(simulator, recorder).bridge(monitor.right))
    await settle()

    payload = encode(player_information("RH", "DR"))
    simulator.current.writer.write(payload)
    await settle()

    assert monitor.right_writer.written == payload
    assert recorder.frames_towards(Direction.FROM_SIMULATOR) == [payload]

    session.cancel()
    await asyncio.gather(session, return_exceptions=True)


async def test_malformed_bytes_are_still_forwarded() -> None:
    # Whether CaddieJ can parse something has no bearing on the player's shot.
    recorder = Recorder()
    simulator = Simulator()
    monitor = memory_connection("monitor")
    session = asyncio.create_task(build(simulator, recorder).bridge(monitor.right))
    await settle()

    for payload in MALFORMED_PAYLOADS:
        monitor.left.writer.write(payload)
    await settle()

    forwarded = simulator.links[0].sent_by_left
    for payload in MALFORMED_PAYLOADS:
        assert payload in forwarded

    session.cancel()
    await asyncio.gather(session, return_exceptions=True)


async def test_messages_split_across_writes_are_reassembled_for_observation() -> None:
    recorder = Recorder()
    simulator = Simulator()
    monitor = memory_connection("monitor")
    session = asyncio.create_task(build(simulator, recorder).bridge(monitor.right))
    await settle()

    payload = encode(shot_message(4))
    for index in range(0, len(payload), 7):
        monitor.left.writer.write(payload[index : index + 7])
        await settle(2)

    assert recorder.frames_towards(Direction.TO_SIMULATOR) == [payload]
    assert simulator.links[0].sent_by_left == payload

    session.cancel()
    await asyncio.gather(session, return_exceptions=True)


async def test_the_device_disconnecting_ends_the_session() -> None:
    recorder = Recorder()
    simulator = Simulator()
    monitor = memory_connection("monitor")
    interposer = build(simulator, recorder)
    session = asyncio.create_task(interposer.bridge(monitor.right))
    await settle()

    await monitor.left.aclose()
    await asyncio.wait_for(session, 1.0)

    assert recorder.states[-1] is LinkState.LISTENING
    assert not interposer.is_bridging


async def test_the_simulator_vanishing_closes_the_device_connection() -> None:
    # Accepting shots the simulator will never see would let a player finish a
    # hole that never happened.
    recorder = Recorder()
    simulator = Simulator()
    monitor = memory_connection("monitor")
    session = asyncio.create_task(build(simulator, recorder).bridge(monitor.right))
    await settle()

    await simulator.current.aclose()
    await asyncio.wait_for(session, 1.0)

    assert monitor.right_writer.is_closed


async def test_an_unreachable_simulator_is_not_concealed_from_the_device() -> None:
    recorder = Recorder()
    simulator = Simulator()
    simulator.reachable = False
    monitor = memory_connection("monitor")

    await asyncio.wait_for(build(simulator, recorder).bridge(monitor.right), 1.0)

    assert LinkState.SIMULATOR_UNAVAILABLE in recorder.states
    assert LinkState.BRIDGED not in recorder.states
    assert monitor.right_writer.is_closed


async def test_a_recently_failed_simulator_is_not_dialled_again_immediately() -> None:
    # The device controls reconnect timing, so without a cooldown a device
    # retrying twice a second would have CaddieJ hammering a dead simulator.
    recorder = Recorder()
    simulator = Simulator()
    simulator.reachable = False
    interposer = build(
        simulator, recorder, backoff=Backoff(5.0, 5.0, jitter=lambda: 1.0)
    )

    await asyncio.wait_for(interposer.bridge(memory_connection("a").right), 1.0)
    assert simulator.dials == 1

    await asyncio.wait_for(interposer.bridge(memory_connection("b").right), 1.0)

    assert simulator.dials == 1
    assert recorder.states.count(LinkState.SIMULATOR_UNAVAILABLE) == 2


async def test_the_cooldown_expires_and_the_simulator_is_tried_again() -> None:
    recorder = Recorder()
    simulator = Simulator()
    simulator.reachable = False
    clock = 1000.0
    interposer = Interposer(
        connect_simulator=simulator.connect,
        on_frame=recorder.on_frame,
        on_link=recorder.on_link,
        backoff=Backoff(1.0, 1.0, jitter=lambda: 1.0),
        monotonic=lambda: clock,
    )

    await asyncio.wait_for(interposer.bridge(memory_connection("a").right), 1.0)

    clock += 2.0
    simulator.reachable = True
    monitor = memory_connection("b")
    session = asyncio.create_task(interposer.bridge(monitor.right))
    await settle()

    assert simulator.dials == 2
    assert LinkState.BRIDGED in recorder.states

    session.cancel()
    await asyncio.gather(session, return_exceptions=True)


async def test_a_successful_connection_clears_the_cooldown() -> None:
    recorder = Recorder()
    simulator = Simulator()
    interposer = build(simulator, recorder)

    monitor = memory_connection("a")
    session = asyncio.create_task(interposer.bridge(monitor.right))
    await settle()
    await monitor.left.aclose()
    await asyncio.wait_for(session, 1.0)

    second = memory_connection("b")
    session = asyncio.create_task(interposer.bridge(second.right))
    await settle()

    assert simulator.dials == 2
    assert recorder.states.count(LinkState.BRIDGED) == 2

    session.cancel()
    await asyncio.gather(session, return_exceptions=True)


async def test_a_second_device_is_refused_while_one_is_bridged() -> None:
    # Open Connect assumes a single device; interleaving two shot streams over
    # one simulator connection would corrupt both.
    recorder = Recorder()
    simulator = Simulator()
    interposer = build(simulator, recorder)
    first = memory_connection("first")
    session = asyncio.create_task(interposer.bridge(first.right))
    await settle()

    second = memory_connection("second")
    await asyncio.wait_for(interposer.bridge(second.right), 1.0)

    assert second.right_writer.is_closed
    assert not first.right_writer.is_closed
    assert simulator.dials == 1

    session.cancel()
    await asyncio.gather(session, return_exceptions=True)


async def test_a_link_that_goes_quiet_is_let_go_of() -> None:
    """A connection whose peer disappeared without closing its socket — a laptop
    lid, a dropped wireless link — reads as open from this side forever. CaddieJ
    bridges one device at a time, so a single one of those refuses every later
    connection until the process is restarted."""

    recorder = Recorder()
    simulator = Simulator()
    interposer = build(simulator, recorder, idle_timeout=0.05)
    monitor = memory_connection("monitor")

    await asyncio.wait_for(interposer.bridge(monitor.right), 2.0)

    assert recorder.states[-1] is LinkState.LISTENING
    assert monitor.right_writer.is_closed
    assert simulator.links[0].left_writer.is_closed


async def test_a_link_that_went_quiet_does_not_refuse_the_next_device() -> None:
    recorder = Recorder()
    simulator = Simulator()
    interposer = build(simulator, recorder, idle_timeout=0.05)

    await asyncio.wait_for(interposer.bridge(memory_connection("gone").right), 2.0)
    assert interposer.is_bridging is False

    second = memory_connection("returned")
    session = asyncio.create_task(interposer.bridge(second.right))
    await settle()

    assert recorder.states.count(LinkState.BRIDGED) == 2
    assert not second.right_writer.is_closed

    session.cancel()
    await asyncio.gather(session, return_exceptions=True)


async def test_traffic_keeps_a_link_alive() -> None:
    """The timeout is about silence, not about duration: a device that is
    heartbeating must be able to stay connected all evening."""

    recorder = Recorder()
    simulator = Simulator()
    interposer = build(simulator, recorder, idle_timeout=0.2)
    monitor = memory_connection("monitor")
    session = asyncio.create_task(interposer.bridge(monitor.right))
    await settle()

    for _ in range(6):
        monitor.left.writer.write(encode(heartbeat()))
        await asyncio.sleep(0.05)

    assert not session.done(), "a talking device was dropped"
    assert recorder.states.count(LinkState.LISTENING) == 0

    session.cancel()
    await asyncio.gather(session, return_exceptions=True)


async def test_the_check_can_be_switched_off() -> None:
    recorder = Recorder()
    simulator = Simulator()
    monitor = memory_connection("monitor")
    session = asyncio.create_task(
        build(simulator, recorder, idle_timeout=0.0).bridge(monitor.right)
    )

    await settle()
    await asyncio.sleep(0.1)

    assert not session.done()

    session.cancel()
    await asyncio.gather(session, return_exceptions=True)


async def test_cancelling_a_session_closes_both_connections() -> None:
    recorder = Recorder()
    simulator = Simulator()
    monitor = memory_connection("monitor")
    session = asyncio.create_task(build(simulator, recorder).bridge(monitor.right))
    await settle()

    session.cancel()
    await asyncio.gather(session, return_exceptions=True)

    assert monitor.right_writer.is_closed
    assert simulator.links[0].left_writer.is_closed
