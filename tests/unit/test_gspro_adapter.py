"""The GSPro adapter, driven by a fake launch monitor and a fake simulator.

Every scenario a real session goes through is exercised here without a socket,
a simulator, or a launch monitor: connect, disconnect, reconnect, club change,
ball detected and removed, complete and incomplete and duplicate shots, and
payloads that are not valid at all.
"""

import asyncio
from typing import TypeVar

import pytest

from caddiej.adapters.gspro.adapter import GSProConnectAdapter, Publish
from caddiej.adapters.gspro.errors import GSProBindError, GSProConnectionError
from caddiej.adapters.gspro.fakes import (
    JUNK_PAYLOADS,
    MALFORMED_PAYLOADS,
    FailingListener,
    FakeLaunchMonitor,
    ScriptedListener,
    ball_data,
    club_data,
    encode,
    memory_connection,
    player_information,
    shot_acknowledgement,
    shot_message,
    simulator_failure,
)
from caddiej.adapters.gspro.interposer import (
    FrameObserver,
    Interposer,
    LinkObserver,
    StreamPair,
)
from caddiej.core.events import Event
from caddiej.domain.events import (
    BallCleared,
    BallDetected,
    ClubSelected,
    DuplicateShotIgnored,
    HandednessChanged,
    IncompleteShotDiscarded,
    LaunchMonitorLinkChanged,
    LaunchMonitorReadinessChanged,
    LinkState,
    RejectionReason,
    ShotIngested,
    ShotMessageRejected,
    SimulatorAcknowledgedShot,
    SimulatorReportedError,
)
from caddiej.domain.shot import ClubKind, Handedness
from caddiej.runtime.backoff import Backoff

pytestmark = pytest.mark.asyncio

EventType = TypeVar("EventType", bound=Event)


class Simulator:
    """A fake simulator that hands out a connection per dial."""

    def __init__(self) -> None:
        self.links: list = []
        self.reachable = True

    async def connect(self) -> StreamPair:
        if not self.reachable:
            raise GSProConnectionError("the fake simulator is not running")
        link = memory_connection(f"simulator-{len(self.links)}")
        self.links.append(link)
        return link.left

    @property
    def current(self) -> StreamPair:
        return self.links[-1].right

    async def push(self, *messages: dict) -> None:
        self.current.writer.write(encode(*messages))
        await self.current.writer.drain()


class Harness:
    """An adapter wired to fakes, with helpers for asserting on events."""

    def __init__(
        self,
        assembly_window: float = 0.05,
        ball_present_debounce: float = 0.0,
        ball_absent_debounce: float = 0.0,
        raw_payload_max_bytes: int = 0,
        publish: Publish | None = None,
    ) -> None:
        self.events: list[Event] = []
        self.listener = ScriptedListener()
        self.simulator = Simulator()
        self.adapter = GSProConnectAdapter(
            listener=self.listener,
            interposer_factory=self._interposer,
            publish=publish or self._publish,
            assembly_window_seconds=assembly_window,
            ball_present_debounce_seconds=ball_present_debounce,
            ball_absent_debounce_seconds=ball_absent_debounce,
            raw_payload_max_bytes=raw_payload_max_bytes,
        )
        self._serving: asyncio.Task[None] | None = None
        self._monitor_links: list = []

    def _interposer(self, on_frame: FrameObserver, on_link: LinkObserver) -> Interposer:
        return Interposer(
            connect_simulator=self.simulator.connect,
            on_frame=on_frame,
            on_link=on_link,
            backoff=Backoff(0.01, 0.02, jitter=lambda: 1.0),
        )

    async def _publish(self, event: Event) -> None:
        self.events.append(event)

    async def __aenter__(self) -> "Harness":
        self._serving = asyncio.create_task(self.adapter.serve(), name="gspro-test")
        await asyncio.wait_for(self.listener.serving.wait(), 1.0)
        await self.expect(LaunchMonitorLinkChanged)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._serving is not None:
            self._serving.cancel()
            await asyncio.gather(self._serving, return_exceptions=True)

    async def connect_monitor(
        self, device_id: str = "CaddieJ Fake LM"
    ) -> FakeLaunchMonitor:
        link = memory_connection(f"monitor-{len(self._monitor_links)}")
        self._monitor_links.append(link)
        self.listener.offer(link.right)
        await self.wait_for_link(LinkState.BRIDGED)
        return FakeLaunchMonitor(link.left, device_id=device_id)

    def of_type(self, event_type: type[EventType]) -> list[EventType]:
        return [event for event in self.events if isinstance(event, event_type)]

    async def expect(
        self, event_type: type[EventType], count: int = 1, timeout: float = 2.0
    ) -> EventType:
        """Wait for the nth event of a type, or fail describing what did arrive."""

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            found = self.of_type(event_type)
            if len(found) >= count:
                return found[count - 1]
            if loop.time() >= deadline:
                seen = ", ".join(type(event).__name__ for event in self.events)
                raise AssertionError(
                    f"no {event_type.__name__} #{count} arrived. "
                    f"Saw: {seen or 'nothing'}"
                )
            await asyncio.sleep(0.005)

    async def wait_for_link(self, state: LinkState, timeout: float = 2.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if any(
                event.state is state for event in self.of_type(LaunchMonitorLinkChanged)
            ):
                return
            if loop.time() >= deadline:
                seen = [
                    event.state.value
                    for event in self.of_type(LaunchMonitorLinkChanged)
                ]
                raise AssertionError(f"link never reached {state.value}. Saw: {seen}")
            await asyncio.sleep(0.005)

    async def quiesce(self, rounds: int = 20) -> None:
        """Let the interpreter finish everything it currently has to do."""

        for _ in range(rounds):
            await asyncio.sleep(0.005)

    @property
    def link_states(self) -> list[LinkState]:
        return [event.state for event in self.of_type(LaunchMonitorLinkChanged)]


# --- connect, disconnect, reconnect ---------------------------------------


async def test_the_adapter_reports_that_it_is_listening() -> None:
    async with Harness() as harness:
        assert harness.link_states[0] is LinkState.LISTENING


async def test_a_connecting_device_is_bridged_to_the_simulator() -> None:
    async with Harness() as harness:
        await harness.connect_monitor()

        assert harness.link_states[:3] == [
            LinkState.LISTENING,
            LinkState.MONITOR_CONNECTED,
            LinkState.BRIDGED,
        ]
        assert harness.adapter.link_state is LinkState.BRIDGED


async def test_a_disconnecting_device_returns_the_adapter_to_listening() -> None:
    async with Harness() as harness:
        monitor = await harness.connect_monitor()

        await monitor.disconnect()
        await harness.quiesce()

        assert harness.link_states[-1] is LinkState.LISTENING


async def test_a_device_can_reconnect_and_keep_playing() -> None:
    async with Harness() as harness:
        monitor = await harness.connect_monitor()
        await monitor.hit_shot()
        await harness.expect(ShotIngested)

        await monitor.disconnect()
        await harness.quiesce()

        reconnected = await harness.connect_monitor()
        await reconnected.hit_shot()

        assert len(await _shots(harness, 2)) == 2


async def test_shot_numbers_repeating_after_a_reconnect_are_not_duplicates() -> None:
    # A device that restarts counts from one again. Treating that as a duplicate
    # would silently discard the first shot of every session.
    async with Harness() as harness:
        monitor = await harness.connect_monitor()
        await monitor.hit_shot()
        await harness.expect(ShotIngested)
        await monitor.disconnect()
        await harness.quiesce()

        reconnected = await harness.connect_monitor()
        reconnected.reset_shot_numbers()
        await reconnected.hit_shot()

        await harness.expect(ShotIngested, count=2)
        assert harness.of_type(DuplicateShotIgnored) == []


async def test_the_two_sessions_of_a_reconnect_are_distinguishable() -> None:
    async with Harness() as harness:
        monitor = await harness.connect_monitor()
        await monitor.hit_shot()
        first = await harness.expect(ShotIngested)
        await monitor.disconnect()
        await harness.quiesce()

        reconnected = await harness.connect_monitor()
        reconnected.reset_shot_numbers()
        await reconnected.hit_shot()
        second = await harness.expect(ShotIngested, count=2)

        assert (
            first.shot.source.device_session_id != second.shot.source.device_session_id
        )


async def test_a_shot_still_queued_when_the_next_device_connects_is_not_lost() -> None:
    # A device's last messages can still be waiting to be interpreted after it
    # has gone and its replacement has arrived. Per-connection state therefore
    # cannot be reset the instant a device connects: the messages still queued
    # belong to the previous connection and must be read as such.
    released = asyncio.Event()

    async def publish(event: Event) -> None:
        if isinstance(event, ShotIngested):
            await released.wait()
        harness.events.append(event)

    harness = Harness(publish=publish)
    async with harness:
        monitor = await harness.connect_monitor()
        await monitor.hit_shot()  # the interpreter stalls publishing this one
        await monitor.hit_shot()  # and this one waits in the queue behind it
        await asyncio.sleep(0.05)
        await monitor.disconnect()
        await asyncio.sleep(0.05)

        harness.listener.offer(memory_connection("replacement").right)
        # The link events are stuck behind the stalled publish, so the new
        # session is observed by the simulator being dialled a second time.
        while len(harness.simulator.links) < 2:
            await asyncio.sleep(0.01)

        released.set()

        assert len(await _shots(harness, 2)) == 2


async def test_a_shot_awaiting_its_club_data_survives_the_device_dropping() -> None:
    # Half a shot is still a shot. Waiting for club data that can no longer
    # arrive, and then discarding the ball flight, is the worst of both.
    async with Harness(assembly_window=30.0) as harness:
        monitor = await harness.connect_monitor()
        await monitor.hit_shot(with_club=False)
        await harness.quiesce()

        assert harness.of_type(ShotIngested) == []

        await monitor.disconnect()

        ingested = await harness.expect(ShotIngested)
        assert ingested.shot.has_ball_data
        assert not ingested.shot.has_club_data


async def test_ball_data_arriving_late_for_a_club_only_shot_is_still_a_shot() -> None:
    async with Harness(assembly_window=0.02) as harness:
        monitor = await harness.connect_monitor()

        await monitor.send(shot_message(1, with_ball=False))
        await harness.expect(IncompleteShotDiscarded)

        await monitor.send(shot_message(1, with_club=False))
        ingested = await harness.expect(ShotIngested)

        assert ingested.shot.has_ball_data


async def test_an_unreachable_simulator_does_not_stop_the_adapter_listening() -> None:
    # CaddieJ does not redial on its own: the device controls reconnect timing,
    # and it will try again. What matters is that the adapter is still there to
    # accept it once the simulator is back.
    async with Harness() as harness:
        harness.simulator.reachable = False
        harness.listener.offer(memory_connection("refused").right)
        await harness.wait_for_link(LinkState.SIMULATOR_UNAVAILABLE)

        harness.simulator.reachable = True
        await asyncio.sleep(0.05)  # let the short test cooldown elapse

        await harness.connect_monitor()

        assert harness.adapter.link_state is LinkState.BRIDGED


async def test_an_unreachable_simulator_stays_the_reported_state() -> None:
    # Announcing "listening" straight afterwards would paper over the one fact
    # worth telling someone: GSPro is not running.
    async with Harness() as harness:
        harness.simulator.reachable = False
        harness.listener.offer(memory_connection("refused").right)
        await harness.wait_for_link(LinkState.SIMULATOR_UNAVAILABLE)
        await harness.quiesce()

        assert harness.adapter.link_state is LinkState.SIMULATOR_UNAVAILABLE
        assert harness.link_states[-1] is LinkState.SIMULATOR_UNAVAILABLE


async def test_being_unable_to_listen_is_reported_and_not_fatal() -> None:
    # A port already in use must not stop CaddieJ running: this is an optional
    # source of facts, not a precondition for the process.
    events: list[Event] = []

    async def publish(event: Event) -> None:
        events.append(event)

    adapter = GSProConnectAdapter(
        listener=FailingListener(GSProBindError("port 9210 is already in use")),
        interposer_factory=lambda on_frame, on_link: Interposer(
            connect_simulator=Simulator().connect, on_frame=on_frame, on_link=on_link
        ),
        publish=publish,
    )

    await asyncio.wait_for(adapter.serve(), 2.0)

    assert adapter.link_state is LinkState.BIND_FAILED
    reported = [
        event.state for event in events if isinstance(event, LaunchMonitorLinkChanged)
    ]
    assert reported == [LinkState.LISTENING, LinkState.BIND_FAILED]


# --- shots ----------------------------------------------------------------


async def _shots(harness: Harness, count: int) -> list[ShotIngested]:
    await harness.expect(ShotIngested, count=count)
    return harness.of_type(ShotIngested)


async def test_a_complete_shot_becomes_a_normalized_shot() -> None:
    async with Harness() as harness:
        monitor = await harness.connect_monitor()

        await monitor.hit_shot()
        ingested = await harness.expect(ShotIngested)

        shot = ingested.shot
        assert shot.has_ball_data and shot.has_club_data
        assert shot.ball is not None
        assert shot.ball.speed_mph == 148.2
        assert shot.ball.launch_angle_degrees == 13.4
        assert shot.ball.carry_yards == 268.4
        assert shot.club_delivery is not None
        assert shot.club_delivery.angle_of_attack_degrees == -1.8
        assert shot.club_delivery.face_to_path_degrees == pytest.approx(3.6)


async def test_an_ingested_shot_carries_its_provenance() -> None:
    async with Harness() as harness:
        monitor = await harness.connect_monitor(device_id="ProTee VX")

        await monitor.hit_shot()
        shot = (await harness.expect(ShotIngested)).shot

        assert shot.source.kind == "gspro_connect"
        assert shot.source.device_id == "ProTee VX"
        assert shot.source.device_sequence == 1
        assert shot.source.reported_units == "Yards"
        assert shot.source.observed_at.tzinfo is not None
        assert shot.source.observed_monotonic > 0


async def test_every_shot_gets_its_own_identity() -> None:
    async with Harness() as harness:
        monitor = await harness.connect_monitor()

        await monitor.hit_shot()
        await monitor.hit_shot()
        shots = await _shots(harness, 2)

        assert shots[0].shot.shot_id != shots[1].shot.shot_id
        assert [shot.shot.source.device_sequence for shot in shots] == [1, 2]


async def test_a_metric_device_has_its_carry_normalized_to_yards() -> None:
    async with Harness() as harness:
        monitor = await harness.connect_monitor()

        await monitor.hit_shot(units="Meters")
        shot = (await harness.expect(ShotIngested)).shot

        assert shot.ball is not None
        assert shot.ball.carry_yards == pytest.approx(293.5, abs=0.1)
        assert shot.source.reported_units == "Meters"


async def test_a_shot_split_across_two_messages_is_assembled_into_one() -> None:
    async with Harness() as harness:
        monitor = await harness.connect_monitor()

        await monitor.send(
            shot_message(1, with_ball=True, with_club=False),
            shot_message(1, with_ball=False, with_club=True),
        )
        shot = (await harness.expect(ShotIngested)).shot

        assert shot.has_ball_data and shot.has_club_data
        assert len(harness.of_type(ShotIngested)) == 1


async def test_a_shot_that_never_gets_its_club_data_is_published_anyway() -> None:
    # Ball data is what makes a shot; club data is a bonus not every device has.
    async with Harness(assembly_window=0.02) as harness:
        monitor = await harness.connect_monitor()

        await monitor.send(shot_message(1, with_ball=True, with_club=False))
        shot = (await harness.expect(ShotIngested)).shot

        assert shot.has_ball_data
        assert not shot.has_club_data


async def test_club_data_with_no_ball_data_is_discarded_as_incomplete() -> None:
    # There is no ball flight to show, score, or record.
    async with Harness(assembly_window=0.02) as harness:
        monitor = await harness.connect_monitor()

        await monitor.send(shot_message(1, with_ball=False, with_club=True))
        discarded = await harness.expect(IncompleteShotDiscarded)

        assert discarded.device_sequence == 1
        assert discarded.had_club_data
        assert not discarded.had_ball_data
        assert harness.of_type(ShotIngested) == []


async def test_a_retransmitted_shot_is_ignored_rather_than_counted_twice() -> None:
    async with Harness() as harness:
        monitor = await harness.connect_monitor()

        await monitor.hit_shot()
        await harness.expect(ShotIngested)
        await monitor.repeat_last_shot()
        duplicate = await harness.expect(DuplicateShotIgnored)

        assert duplicate.device_sequence == 1
        assert len(harness.of_type(ShotIngested)) == 1
        assert harness.adapter.health().duplicates_ignored == 1


async def test_the_same_shot_number_with_different_data_is_another_shot() -> None:
    # A device that does not increment its shot number is a device with a bug,
    # not a player who has stopped hitting shots. Refusing these would lose an
    # entire session's worth of golf.
    async with Harness() as harness:
        monitor = await harness.connect_monitor()
        await monitor.hit_shot()
        await harness.expect(ShotIngested)

        await monitor.send(shot_message(1, ball=ball_data(speed=90.0)))
        second = await harness.expect(ShotIngested, count=2)

        assert second.shot.ball is not None
        assert second.shot.ball.speed_mph == 90.0
        assert harness.of_type(ShotMessageRejected) == []
        assert harness.adapter.health().reused_shot_numbers == 1


async def test_a_device_stuck_on_one_shot_number_loses_nothing() -> None:
    async with Harness() as harness:
        monitor = await harness.connect_monitor()

        for speed in (100.0, 110.0, 120.0):
            await monitor.send(shot_message(1, ball=ball_data(speed=speed)))
        await harness.expect(ShotIngested, count=3)

        speeds = [
            event.shot.ball.speed_mph
            for event in harness.of_type(ShotIngested)
            if event.shot.ball is not None
        ]
        assert speeds == [100.0, 110.0, 120.0]


async def test_heartbeats_are_not_shots() -> None:
    # Heartbeats carry zero-filled data blocks; believing them would produce a
    # shot travelling at zero miles per hour every second.
    async with Harness() as harness:
        monitor = await harness.connect_monitor()

        for _ in range(5):
            await monitor.send_heartbeat()
        await harness.quiesce()

        assert harness.of_type(ShotIngested) == []
        assert harness.of_type(ShotMessageRejected) == []


async def test_a_shot_arriving_in_fragments_is_still_understood() -> None:
    async with Harness() as harness:
        monitor = await harness.connect_monitor()

        await monitor.send(shot_message(1), chunk_size=5)
        shot = (await harness.expect(ShotIngested)).shot

        assert shot.ball is not None
        assert shot.ball.speed_mph == 148.2


async def test_a_club_block_of_nothing_but_nulls_is_not_club_data() -> None:
    async with Harness(assembly_window=0.02) as harness:
        monitor = await harness.connect_monitor()

        await monitor.send(
            shot_message(1, club={"Speed": None, "Path": None}),
        )
        shot = (await harness.expect(ShotIngested)).shot

        assert not shot.has_club_data


# --- device status --------------------------------------------------------


async def test_a_ball_being_placed_is_reported() -> None:
    async with Harness() as harness:
        monitor = await harness.connect_monitor()

        await monitor.send_heartbeat(ball_detected=True)

        assert (await harness.expect(BallDetected)).device_id == "CaddieJ Fake LM"


async def test_a_ball_being_removed_is_reported() -> None:
    async with Harness() as harness:
        monitor = await harness.connect_monitor()
        await monitor.send_heartbeat(ball_detected=True)
        await harness.expect(BallDetected)

        await monitor.send_heartbeat(ball_detected=False)

        await harness.expect(BallCleared)


async def test_a_repeating_ball_flag_is_not_an_event_storm() -> None:
    async with Harness() as harness:
        monitor = await harness.connect_monitor()

        for _ in range(10):
            await monitor.send_heartbeat(ball_detected=True)
        await harness.quiesce()

        assert len(harness.of_type(BallDetected)) == 1


async def test_a_device_that_never_reports_detection_produces_no_ball_events() -> None:
    # Silence is not the same as "no ball": the floor must not clear its marker
    # forever because a device does not implement detection.
    async with Harness() as harness:
        monitor = await harness.connect_monitor()

        for _ in range(3):
            await monitor.send_heartbeat(ball_detected=None)
        await harness.quiesce()

        assert harness.of_type(BallDetected) == []
        assert harness.of_type(BallCleared) == []


async def test_a_ball_absent_before_ever_being_present_is_not_a_removal() -> None:
    async with Harness() as harness:
        monitor = await harness.connect_monitor()

        await monitor.send_heartbeat(ball_detected=False)
        await harness.quiesce()

        assert harness.of_type(BallCleared) == []


async def test_a_ball_appearing_is_believed_at_once() -> None:
    # The floor has to stop projecting onto the ball, so this cannot wait.
    async with Harness(ball_present_debounce=0.0, ball_absent_debounce=5.0) as harness:
        monitor = await harness.connect_monitor()

        await monitor.send_heartbeat(ball_detected=True)

        await harness.expect(BallDetected)


async def test_detection_blinking_off_for_one_heartbeat_does_not_clear_the_ball() -> (
    None
):
    # The marker flashing back on under the player's club is worse than a slow
    # clear, so a ball has to be gone for a while before it is believed gone.
    async with Harness(ball_absent_debounce=5.0) as harness:
        monitor = await harness.connect_monitor()
        await monitor.send_heartbeat(ball_detected=True)
        await harness.expect(BallDetected)

        await monitor.send_heartbeat(ball_detected=False)
        await monitor.send_heartbeat(ball_detected=True)
        await harness.quiesce(rounds=10)

        assert harness.of_type(BallCleared) == []
        assert len(harness.of_type(BallDetected)) == 1


async def test_a_ball_that_stays_gone_is_cleared_after_the_delay() -> None:
    async with Harness(ball_absent_debounce=0.05) as harness:
        monitor = await harness.connect_monitor()
        await monitor.send_heartbeat(ball_detected=True)
        await harness.expect(BallDetected)

        await monitor.send_heartbeat(ball_detected=False)

        await harness.expect(BallCleared)


async def test_device_readiness_is_reported_when_it_changes() -> None:
    async with Harness() as harness:
        monitor = await harness.connect_monitor()

        await monitor.send_heartbeat(ready=True)
        ready = await harness.expect(LaunchMonitorReadinessChanged)
        await monitor.send_heartbeat(ready=False)
        not_ready = await harness.expect(LaunchMonitorReadinessChanged, count=2)

        assert ready.ready is True
        assert not_ready.ready is False


async def test_readiness_repeating_is_not_a_change() -> None:
    async with Harness() as harness:
        monitor = await harness.connect_monitor()

        for _ in range(5):
            await monitor.send_heartbeat(ready=True)
        await harness.quiesce()

        assert len(harness.of_type(LaunchMonitorReadinessChanged)) == 1


# --- what the simulator says back -----------------------------------------


async def test_a_club_change_from_the_simulator_is_reported() -> None:
    async with Harness() as harness:
        await harness.connect_monitor()

        await harness.simulator.push(player_information("RH", "DR"))
        first = await harness.expect(ClubSelected)
        await harness.simulator.push(player_information("RH", "PT"))
        second = await harness.expect(ClubSelected, count=2)

        assert first.club.kind is ClubKind.DRIVER
        assert second.club.kind is ClubKind.PUTTER
        assert second.club.vendor_code == "PT"


async def test_player_information_on_a_200_is_applied() -> None:
    async with Harness() as harness:
        await harness.connect_monitor()

        await harness.simulator.push(shot_acknowledgement(handed="RH", club="DR"))
        club = await harness.expect(ClubSelected)
        await harness.expect(SimulatorAcknowledgedShot)

        assert club.club.kind is ClubKind.DRIVER
        assert club.club.vendor_code == "DR"


async def test_a_200_without_a_player_block_is_only_an_acknowledgement() -> None:
    async with Harness() as harness:
        await harness.connect_monitor()

        await harness.simulator.push(shot_acknowledgement())
        await harness.expect(SimulatorAcknowledgedShot)
        await harness.quiesce()

        assert harness.of_type(ClubSelected) == []
        assert harness.of_type(HandednessChanged) == []


async def test_the_same_club_reported_repeatedly_is_one_change() -> None:
    async with Harness() as harness:
        await harness.connect_monitor()

        for _ in range(4):
            await harness.simulator.push(player_information("RH", "I7"))
        await harness.quiesce()

        assert len(harness.of_type(ClubSelected)) == 1


async def test_the_same_club_on_a_200_is_still_one_change() -> None:
    async with Harness() as harness:
        await harness.connect_monitor()

        for _ in range(4):
            await harness.simulator.push(shot_acknowledgement(handed="RH", club="I7"))
        await harness.quiesce()

        assert len(harness.of_type(ClubSelected)) == 1
        assert len(harness.of_type(SimulatorAcknowledgedShot)) == 4


async def test_handedness_is_reported_when_it_changes() -> None:
    async with Harness() as harness:
        await harness.connect_monitor()

        await harness.simulator.push(player_information("LH", "DR"))
        changed = await harness.expect(HandednessChanged)

        assert changed.handedness is Handedness.LEFT


async def test_handedness_on_a_200_is_applied() -> None:
    async with Harness() as harness:
        await harness.connect_monitor()

        await harness.simulator.push(shot_acknowledgement(handed="LH", club="DR"))
        changed = await harness.expect(HandednessChanged)
        await harness.expect(SimulatorAcknowledgedShot)

        assert changed.handedness is Handedness.LEFT


async def test_a_shot_records_the_handedness_in_play_when_it_was_hit() -> None:
    async with Harness() as harness:
        monitor = await harness.connect_monitor()
        await harness.simulator.push(player_information("LH", "DR"))
        await harness.expect(HandednessChanged)

        await monitor.hit_shot()
        shot = (await harness.expect(ShotIngested)).shot

        assert shot.handedness is Handedness.LEFT


async def test_a_shot_acknowledgement_is_reported() -> None:
    async with Harness() as harness:
        await harness.connect_monitor()

        await harness.simulator.push(shot_acknowledgement())

        await harness.expect(SimulatorAcknowledgedShot)


async def test_a_simulator_failure_is_reported() -> None:
    async with Harness() as harness:
        await harness.connect_monitor()

        await harness.simulator.push(simulator_failure(501, "Failure occurred"))
        reported = await harness.expect(SimulatorReportedError)

        assert reported.code == 501
        assert reported.message == "Failure occurred"


# --- bad input ------------------------------------------------------------


@pytest.mark.parametrize("payload", MALFORMED_PAYLOADS)
async def test_a_malformed_payload_is_rejected_with_a_reason(payload: bytes) -> None:
    async with Harness() as harness:
        monitor = await harness.connect_monitor()

        await monitor.send_malformed(payload)
        rejection = await harness.expect(ShotMessageRejected)

        assert rejection.reason in {
            RejectionReason.UNPARSEABLE,
            RejectionReason.SCHEMA_INVALID,
        }
        assert harness.of_type(ShotIngested) == []


async def test_a_malformed_payload_does_not_break_the_session() -> None:
    # A device that hiccups must not cost the player the rest of the round.
    async with Harness() as harness:
        monitor = await harness.connect_monitor()

        await monitor.send_malformed()
        await harness.expect(ShotMessageRejected)
        await monitor.hit_shot()

        await harness.expect(ShotIngested)
        assert harness.adapter.link_state is LinkState.BRIDGED


async def test_malformed_bytes_are_forwarded_to_the_simulator_regardless() -> None:
    # Whether CaddieJ understands a message has no bearing on the player's shot.
    async with Harness() as harness:
        monitor = await harness.connect_monitor()

        await monitor.send_malformed(b"{not json at all}")
        await harness.expect(ShotMessageRejected)

        assert b"{not json at all}" in harness.simulator.links[0].sent_by_left


@pytest.mark.parametrize("payload", JUNK_PAYLOADS)
async def test_junk_containing_no_message_is_skipped_silently(payload: bytes) -> None:
    # Junk never becomes a message, so there is nothing to reject. It is skipped
    # while framing, forwarded regardless, and the session carries on.
    async with Harness() as harness:
        monitor = await harness.connect_monitor()

        await monitor.send_raw(payload)
        await monitor.hit_shot()
        await harness.expect(ShotIngested)

        assert harness.of_type(ShotMessageRejected) == []
        assert payload in harness.simulator.links[0].sent_by_left


async def test_a_shot_declaring_data_it_did_not_send_is_rejected() -> None:
    async with Harness() as harness:
        monitor = await harness.connect_monitor()

        await monitor.send_raw(
            encode(
                {
                    "DeviceID": "LM",
                    "ShotNumber": 1,
                    "ShotDataOptions": {
                        "ContainsBallData": True,
                        "ContainsClubData": True,
                    },
                }
            )
        )
        await harness.quiesce()

        assert harness.of_type(ShotIngested) == []


async def test_an_oversized_message_costs_one_message_not_the_connection() -> None:
    async with Harness() as harness:
        monitor = await harness.connect_monitor()

        await monitor.send_raw(b'{"padding": "' + b"x" * 200000)
        await monitor.hit_shot()

        await harness.expect(ShotIngested)


async def test_a_failing_subscriber_does_not_stop_later_shots_being_read() -> None:
    # Interpretation is a long-lived loop. If one message could kill it, a single
    # bad event handler would silently end shot ingestion for the session.
    harness = Harness()
    failures: list[int] = []
    original = harness._publish

    async def sometimes_broken(event: Event) -> None:
        if isinstance(event, ShotIngested) and not failures:
            failures.append(1)
            raise RuntimeError("a subscriber blew up")
        await original(event)

    harness._publish = sometimes_broken  # type: ignore[method-assign]
    harness.adapter._publish = sometimes_broken  # type: ignore[assignment]

    async with harness:
        monitor = await harness.connect_monitor()

        await monitor.hit_shot()
        await harness.quiesce()
        await monitor.hit_shot()

        ingested = await harness.expect(ShotIngested)
        assert failures == [1]
        assert ingested.shot.source.device_sequence == 2


async def test_a_burst_of_traffic_is_interpreted_without_loss() -> None:
    # Frames are handed off to another task so the byte path is never blocked.
    # Nothing may be dropped on the way.
    async with Harness() as harness:
        monitor = await harness.connect_monitor()

        for _ in range(40):
            await monitor.hit_shot()

        await harness.expect(ShotIngested, count=40, timeout=5.0)
        assert harness.adapter.health().frames_dropped == 0


async def test_a_shot_is_not_dropped_behind_a_backlog_of_heartbeats() -> None:
    # The queue exists so the byte path is never blocked, and it is bounded so a
    # wedged interpreter cannot eat memory. When it does overflow, what goes has
    # to be the oldest heartbeat rather than the shot that just happened.
    blocked = asyncio.Event()

    async def publish(event: Event) -> None:
        if isinstance(event, LaunchMonitorReadinessChanged):
            await blocked.wait()
        harness.events.append(event)

    harness = Harness(publish=publish)
    async with harness:
        monitor = await harness.connect_monitor()
        await monitor.send_heartbeat(ready=True)
        await harness.quiesce()  # the interpreter is now stuck on the heartbeat

        for _ in range(400):
            await monitor.send_heartbeat()
        await monitor.hit_shot()

        blocked.set()

        ingested = await harness.expect(ShotIngested, timeout=5.0)
        assert ingested.shot.has_ball_data
        assert harness.adapter.health().frames_dropped > 0


async def test_a_number_the_domain_refuses_is_a_rejection_not_a_crash() -> None:
    async with Harness() as harness:
        monitor = await harness.connect_monitor()

        await monitor.send(shot_message(1, ball=ball_data(speed=-40.0)))
        rejection = await harness.expect(ShotMessageRejected)

        assert rejection.reason is RejectionReason.IMPLAUSIBLE_VALUE
        assert harness.of_type(ShotIngested) == []
        assert harness.adapter.health().messages_rejected == 1


async def test_a_split_shot_merges_even_if_one_half_omits_the_device_id() -> None:
    async with Harness(assembly_window=30.0) as harness:
        monitor = await harness.connect_monitor()

        await monitor.send(shot_message(1, with_club=False))
        await monitor.send_raw(
            encode(
                {
                    "ShotNumber": 1,
                    "ShotDataOptions": {
                        "ContainsBallData": False,
                        "ContainsClubData": True,
                    },
                    "ClubData": {"Speed": 104.5, "Path": -2.4},
                }
            )
        )

        ingested = await harness.expect(ShotIngested)
        assert ingested.shot.has_ball_data
        assert ingested.shot.has_club_data


async def test_an_undocumented_response_code_is_neither_applied_nor_lost() -> None:
    async with Harness() as harness:
        await harness.connect_monitor()

        await harness.simulator.push({"Code": 302, "Message": "Something new"})
        await harness.quiesce()

        assert harness.of_type(SimulatorReportedError) == []
        assert harness.of_type(SimulatorAcknowledgedShot) == []


# --- keeping what the device actually said --------------------------------


async def test_nothing_is_kept_of_what_a_device_said_by_default() -> None:
    # Holding every message's bytes for the length of an evening is not free,
    # and nothing reads them unless somebody switched capture on.
    async with Harness() as harness:
        monitor = await harness.connect_monitor()

        await monitor.hit_shot()

        ingested = await harness.expect(ShotIngested)
        assert ingested.shot.source.raw_payload is None


async def test_a_shot_can_carry_the_message_it_was_read_from() -> None:
    # For diagnosing a device that reports something unexpected, from a recorded
    # round rather than a live one.
    async with Harness(raw_payload_max_bytes=4096) as harness:
        monitor = await harness.connect_monitor()

        await monitor.hit_shot(ball=ball_data(speed=148.2))

        ingested = await harness.expect(ShotIngested)
        raw = ingested.shot.source.raw_payload
        assert raw is not None
        assert '"BallData"' in raw
        assert "148.2" in raw


async def test_a_split_shot_keeps_both_halves() -> None:
    # A recording with one half of a split shot would be no use for working out
    # why the halves disagreed.
    async with Harness(assembly_window=1.0, raw_payload_max_bytes=4096) as harness:
        monitor = await harness.connect_monitor()

        await monitor.send(shot_message(1, with_ball=True, with_club=False))
        await monitor.send(
            shot_message(1, with_ball=False, club=club_data(speed=104.5))
        )

        ingested = await harness.expect(ShotIngested)
        raw = ingested.shot.source.raw_payload
        assert raw is not None
        assert len(raw.splitlines()) == 2
        assert "104.5" in raw


async def test_an_oversized_capture_is_truncated_and_says_so() -> None:
    # A limit that silently cut a payload in half would look like a device
    # sending malformed JSON.
    async with Harness(raw_payload_max_bytes=40) as harness:
        monitor = await harness.connect_monitor()

        await monitor.hit_shot()

        ingested = await harness.expect(ShotIngested)
        raw = ingested.shot.source.raw_payload
        assert raw is not None
        assert "truncated" in raw


async def test_a_heartbeat_is_not_kept() -> None:
    # Heartbeats arrive about once a second and are not shots, so capturing them
    # would be almost all of the bytes and none of the value.
    async with Harness(raw_payload_max_bytes=4096) as harness:
        monitor = await harness.connect_monitor()

        await monitor.send_heartbeat(ready=True, ball_detected=False)
        await monitor.hit_shot()

        ingested = await harness.expect(ShotIngested)
        raw = ingested.shot.source.raw_payload
        assert raw is not None
        assert len(raw.splitlines()) == 1


async def test_health_counts_what_happened() -> None:
    async with Harness() as harness:
        monitor = await harness.connect_monitor()
        await monitor.hit_shot()
        await harness.expect(ShotIngested)
        await monitor.repeat_last_shot()
        await harness.expect(DuplicateShotIgnored)
        await monitor.send_malformed()
        await harness.expect(ShotMessageRejected)

        health = harness.adapter.health()

        assert health.shots_ingested == 1
        assert health.duplicates_ignored == 1
        assert health.messages_rejected == 1
        assert health.frames_dropped == 0
        assert health.link_state is LinkState.BRIDGED
