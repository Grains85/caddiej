"""Passive observers driving the HUD through the real overlay server."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path

import pytest
import websockets

from caddiej.adapters.gspro.round_file import GSProRoundFileAdapter
from caddiej.adapters.hud import ConnectionRegistry, HudServer, create_app
from caddiej.adapters.protee.log_adapter import ProTeeLogAdapter
from caddiej.core.event_bus import AsyncEventBus
from caddiej.domain.event_policy import delivery_for, queue_size_for
from caddiej.domain.play import PlayFlow
from caddiej.floor import FloorPresenter, FloorService, PhysicalPoint

pytestmark = pytest.mark.loopback

DAY = date(2026, 9, 14)


def a_shot(shot_id: str) -> dict[str, object]:
    return {
        "ShotID": shot_id,
        "PlayerName": "Johan",
        "Hole": 3,
        "HoleShot": 2,
        "BallSpeed": 32.84,
        "TotalDistance": 248.5,
        "StartingSurface": 1,
        "EndingSurface": 4,
        "activeShot": {
            "sd": {
                "BallStartPos": {"x": 10.0, "y": 1.0, "z": 20.0},
                "path": [
                    {"time": 0.0, "pos": {"x": 10.0, "y": 1.0, "z": 20.0}},
                    {"time": 0.5, "pos": {"x": 14.0, "y": 3.0, "z": 28.0}},
                ],
            }
        },
    }


async def snapshot_after(ws, previous_seq: int) -> dict:
    while True:
        raw = await asyncio.wait_for(ws.recv(), 5)
        message = json.loads(raw)
        if message.get("type") == "snapshot" and message["seq"] > previous_seq:
            return message


async def wait_for_state(ws, *, previous_seq: int, check) -> dict:
    seq = previous_seq
    while True:
        message = await snapshot_after(ws, seq)
        seq = message["seq"]
        if check(message["state"]):
            return message


@pytest.fixture
async def hud_stack(tmp_path: Path):
    protee_root = tmp_path / "ProTeeUnited"
    logs = protee_root / "Logs"
    logs.mkdir(parents=True)
    log_path = logs / f"LogFile-{DAY.isoformat()}.txt"
    log_path.write_text("historical READY FOR SHOT\n", encoding="utf-8")

    round_path = tmp_path / "currentRound.dat"
    round_path.write_text(json.dumps([a_shot("A"), a_shot("B")]), encoding="utf-8")

    bus = AsyncEventBus(policy=delivery_for, queue_size=queue_size_for)
    floor = FloorService()
    registry = ConnectionRegistry()
    floor.add_listener(registry.broadcast)
    play = PlayFlow(
        floor=FloorPresenter(floor, tee=PhysicalPoint(x_m=0.5, y_m=0.72)),
        publish=bus.publish,
    )
    play.subscribe(bus)
    play.start()

    protee = ProTeeLogAdapter(
        protee_root,
        bus.publish,
        poll_interval=0.02,
        today=lambda: DAY,
    )
    gspro = GSProRoundFileAdapter(round_path, bus.publish, poll_interval=0.02)
    await protee.poll()
    await gspro.poll()

    server = HudServer(
        create_app(floor, registry),
        host="127.0.0.1",
        port=0,
    )
    await bus.start()
    hud_task = asyncio.create_task(server.serve(), name="hud")
    protee_task = asyncio.create_task(protee.serve(), name="protee")
    gspro_task = asyncio.create_task(gspro.serve(), name="gspro-round")
    await server.wait_until_serving(10)

    yield {
        "url": f"ws://127.0.0.1:{server.port}/ws",
        "http": f"http://127.0.0.1:{server.port}",
        "log": log_path,
        "round": round_path,
        "floor": floor,
        "protee": protee,
        "gspro": gspro,
        "play": play,
        "bus": bus,
    }

    for task in (protee_task, gspro_task):
        task.cancel()
    await asyncio.gather(protee_task, gspro_task, return_exceptions=True)
    await play.aclose()
    await bus.stop(1.0)
    await server.stop()
    await asyncio.wait_for(hud_task, 5)


async def test_passive_observers_update_the_overlay(hud_stack: dict) -> None:
    log_path: Path = hud_stack["log"]
    round_path: Path = hud_stack["round"]
    floor: FloorService = hud_stack["floor"]

    async with websockets.connect(hud_stack["url"]) as ws:
        await asyncio.wait_for(ws.recv(), 5)  # hello
        first = json.loads(await asyncio.wait_for(ws.recv(), 5))
        assert first["type"] == "snapshot"
        seq = first["seq"]

        with log_path.open("a", encoding="utf-8") as handle:
            handle.write("VX status changed: READY FOR SHOT\n")
            handle.write("Detected a right handed Driver\n")

        ready = await wait_for_state(
            ws,
            previous_seq=seq,
            check=lambda state: (
                state.get("play_status") == "READY"
                and state.get("club_label") == "DRIVER"
            ),
        )
        seq = ready["seq"]
        state = ready["state"]
        assert state["marker"]["visible"] is True
        assert floor.state.play_status == "READY"

        shots = [a_shot("A"), a_shot("B"), a_shot("D")]
        round_path.write_text(json.dumps(shots), encoding="utf-8")
        result = await wait_for_state(
            ws,
            previous_seq=seq,
            check=lambda state: (state.get("round_result") or {}).get("shot_id") == "D",
        )
        panel = result["state"]["round_result"]
        assert panel["shot_id"] == "D"
        assert panel["ball_speed_raw"] == 32.84
        assert panel["total_distance_raw"] == 248.5
        assert panel["ball_speed_display"] == "118 km/h"
        assert panel["total_distance_display"] == "249 m"
        assert panel["hole"] == 3
        assert panel["hole_shot"] == 2
        assert panel["starting_surface_raw"] == 1
        assert panel["ending_surface_raw"] == 4
        assert panel["player_name"] == "Johan"
        flight = result["state"]["ball_flight"]
        assert flight is not None
        assert len(flight) == 2

    health = hud_stack["gspro"].health()
    assert health.shots_observed == 1
    assert health.last_shot_id == "D"
    assert hud_stack["protee"].health().last_event_at is not None
