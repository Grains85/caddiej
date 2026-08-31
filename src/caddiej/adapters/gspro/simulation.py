"""A round of golf with no golf in it.

This runs the real adapter against a fake GSPro and a fake launch monitor, inside
one process. Bytes are framed, parsed, assembled, translated and published by
exactly the code a real device would drive, so what appears on the floor is what
would appear during a round.

That is the point: a simulated *device*, not a simulated pipeline. A mode that
called `FloorService` directly would look identical on the projector and prove
nothing, because the parts most likely to be wrong would not have run.

Development only. It binds two loopback ports and never listens on a network
interface.
"""

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from caddiej.adapters.gspro.adapter import GSProConnectAdapter, Publish
from caddiej.adapters.gspro.factory import build_adapter
from caddiej.adapters.gspro.fakes import (
    FakeGSProServer,
    FakeLaunchMonitor,
    ball_data,
    club_data,
    player_information,
)
from caddiej.adapters.gspro.interposer import (
    TcpMonitorListener,
    connect_to_simulator,
)
from caddiej.config import Settings
from caddiej.domain.events import LinkState

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ScriptedShot:
    """One club, and the shot hit with it."""

    club_code: str
    ball_speed_mph: float
    launch_angle_degrees: float
    launch_direction_degrees: float
    club_path_degrees: float
    carry_yards: float | None


def scripted_round() -> Sequence[ScriptedShot]:
    """A driver, an iron, a wedge and a putt, with plausible numbers.

    The putt has no carry, which is both true of a putt and the case that proves
    the floor leaves a number out rather than printing a zero.
    """

    return (
        ScriptedShot("DR", 148.2, 12.6, 1.5, -2.4, 268.4),
        ScriptedShot("I7", 118.4, 18.9, -1.1, 3.2, 172.0),
        ScriptedShot("PW", 92.6, 27.4, 0.4, 1.0, 118.5),
        ScriptedShot("PT", 6.2, 1.1, 0.2, 0.5, None),
    )


async def _play_shot(
    monitor: FakeLaunchMonitor,
    simulator: FakeGSProServer,
    shot: ScriptedShot,
    step: float,
) -> None:
    """Walk one shot through the sequence a real one goes through."""

    # GSPro announces the club, unprompted, exactly as it does in play.
    await simulator.push(player_information(club=shot.club_code))
    await monitor.send_heartbeat(ready=True, ball_detected=False)
    await asyncio.sleep(step)

    await monitor.send_heartbeat(ready=True, ball_detected=True)
    await asyncio.sleep(step)

    await monitor.hit_shot(
        ball=ball_data(
            speed=shot.ball_speed_mph,
            hla=shot.launch_direction_degrees,
            vla=shot.launch_angle_degrees,
            carry=shot.carry_yards,
        ),
        club=club_data(path=shot.club_path_degrees),
    )
    await monitor.send_heartbeat(ready=False, ball_detected=False)
    await asyncio.sleep(step)


async def drive_device(
    monitor: FakeLaunchMonitor,
    simulator: FakeGSProServer,
    step_seconds: float,
    shots: Sequence[ScriptedShot] | None = None,
) -> None:
    """Hit the scripted round on a loop until cancelled."""

    script = scripted_round() if shots is None else shots
    logger.info("gspro_simulation_started", extra={"floor_demo_steps": len(script)})
    try:
        while True:
            for shot in script:
                logger.info(
                    "gspro_simulation_shot", extra={"gspro_club_code": shot.club_code}
                )
                await _play_shot(monitor, simulator, shot, step_seconds)
    except asyncio.CancelledError:
        logger.info("gspro_simulation_stopped")
        raise


AdapterReady = Callable[[GSProConnectAdapter], None]


async def run_simulated_round(
    settings: Settings,
    publish: Publish,
    on_adapter: AdapterReady | None = None,
) -> None:
    """Run the adapter, a fake GSPro and a fake device until cancelled."""

    async with FakeGSProServer() as simulator:
        listener = TcpMonitorListener(settings.gspro_bind_host, 0)
        adapter = build_adapter(
            # The fake GSPro takes an ephemeral port, and the launch monitor
            # side does too: a simulated round must never collide with a real
            # GSPro or a real device that happens to be running.
            settings.model_copy(update={"gspro_upstream_port": simulator.port}),
            publish,
            listener=listener,
        )
        if on_adapter is not None:
            on_adapter(adapter)
        async with asyncio.TaskGroup() as group:
            group.create_task(adapter.serve(), name="gspro-simulated-adapter")
            monitor = await _connect_fake_device(listener, settings)
            # Nothing GSPro says before the bridge exists can be heard, and the
            # first thing the script says is which club is in play. Without this
            # the round opens with a shot credited to no club.
            await _wait_until_bridged(adapter, settings.gspro_connect_timeout)
            step = settings.gspro_simulation_step_seconds
            group.create_task(
                drive_device(monitor, simulator, step),
                name="gspro-simulated-device",
            )


async def _wait_until_bridged(adapter: GSProConnectAdapter, timeout: float) -> None:
    async with asyncio.timeout(timeout):
        while adapter.link_state is not LinkState.BRIDGED:
            await asyncio.sleep(0.01)


async def _connect_fake_device(
    listener: TcpMonitorListener, settings: Settings
) -> FakeLaunchMonitor:
    await asyncio.wait_for(listener.serving.wait(), settings.gspro_connect_timeout)
    port = listener.bound_port
    if port is None:  # pragma: no cover - serving implies a bound socket
        raise RuntimeError("The simulated launch monitor has nowhere to connect")
    stream = await connect_to_simulator(
        settings.gspro_bind_host, port, settings.gspro_connect_timeout
    )
    logger.info("gspro_simulated_device_connected", extra={"gspro_bind": str(port)})
    return FakeLaunchMonitor(stream, device_id="CaddieJ Simulated LM")
