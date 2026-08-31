"""End-to-end HUD tests over a real loopback socket.

These need no external software. They bind a loopback port and speak real
HTTP and WebSocket, and they run in ordinary CI.
"""

import asyncio
import json
import os
import signal
import sys

import pytest
import websockets

from caddiej.adapters.hud import ConnectionRegistry, HudServer, create_app
from caddiej.floor import (
    ClubPath,
    FloorPoint,
    FloorService,
    MarkerVariant,
    ShotMetrics,
)

pytestmark = pytest.mark.loopback


class RunningHud:
    def __init__(
        self, service: FloorService, server: HudServer, task: asyncio.Task
    ) -> None:
        self.service = service
        self.server = server
        self.task = task

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.server.port}/ws"

    @property
    def http_url(self) -> str:
        return f"http://127.0.0.1:{self.server.port}"


@pytest.fixture
async def hud():
    service = FloorService()
    registry = ConnectionRegistry()
    service.add_listener(registry.broadcast)
    # Port 0 lets the OS pick a free port, so tests never collide with a real run.
    server = HudServer(create_app(service, registry), host="127.0.0.1", port=0)
    task = asyncio.create_task(server.serve(), name="hud-server-test")
    await server.wait_until_serving(10)

    yield RunningHud(service, server, task)

    await server.stop()
    await asyncio.wait_for(task, 10)


async def test_a_real_client_reconnecting_recovers_the_whole_floor(
    hud: RunningHud,
) -> None:
    # The behaviour an OBS Browser Source refresh depends on: one snapshot has
    # to restore everything, over a real socket rather than a test harness.
    hud.service.set_online("CADDIEJ FLOOR ONLINE")
    hud.service.show_marker(FloorPoint(x=0.18, y=0.82), MarkerVariant.DRIVER)
    hud.service.set_metrics(
        ShotMetrics(ball_speed_mph=151.3, launch_angle_degrees=11.2, carry_yards=274.6)
    )
    hud.service.set_club_path(
        ClubPath(start=FloorPoint(x=0.18, y=0.82), angle_degrees=-30.0, length=0.5)
    )
    expected_version = hud.service.state.version

    async with websockets.connect(hud.ws_url) as first:
        await asyncio.wait_for(first.recv(), 5)  # hello
        await asyncio.wait_for(first.recv(), 5)  # snapshot

    # The page reloaded: a new socket, no memory of the previous one.
    async with websockets.connect(hud.ws_url) as second:
        await asyncio.wait_for(second.recv(), 5)  # hello
        snapshot = json.loads(await asyncio.wait_for(second.recv(), 5))

    state = snapshot["state"]
    assert snapshot["seq"] == expected_version
    assert state["marker"]["variant"] == "driver"
    assert state["marker"]["visible"] is True
    assert state["marker"]["position"] == {"x": 0.18, "y": 0.82}
    assert state["metrics"]["ball_speed_mph"] == 151.3
    assert state["club_path"]["end"]["x"] == pytest.approx(-0.07)
    assert state["club_path"]["end"]["y"] == pytest.approx(0.387, abs=1e-3)


async def test_real_client_receives_state_and_live_updates(hud: RunningHud) -> None:
    hud.service.set_online("CADDIEJ FLOOR ONLINE")

    async with websockets.connect(hud.ws_url) as socket:
        hello = json.loads(await asyncio.wait_for(socket.recv(), 5))
        snapshot = json.loads(await asyncio.wait_for(socket.recv(), 5))

        assert hello["type"] == "hello"
        assert snapshot["state"]["status_text"] == "CADDIEJ FLOOR ONLINE"

        hud.service.set_status_text("CADDIEJ FLOOR UPDATED")
        update = json.loads(await asyncio.wait_for(socket.recv(), 5))

    assert update["type"] == "snapshot"
    assert update["state"]["status_text"] == "CADDIEJ FLOOR UPDATED"
    assert update["seq"] > snapshot["seq"]


async def test_reconnect_restores_the_latest_state(hud: RunningHud) -> None:
    hud.service.set_online("FIRST")

    async with websockets.connect(hud.ws_url) as socket:
        await asyncio.wait_for(socket.recv(), 5)
        await asyncio.wait_for(socket.recv(), 5)

    # State advances while nothing is connected.
    hud.service.set_status_text("CHANGED WHILE DISCONNECTED")

    async with websockets.connect(hud.ws_url) as socket:
        await asyncio.wait_for(socket.recv(), 5)
        snapshot = json.loads(await asyncio.wait_for(socket.recv(), 5))

    assert snapshot["state"]["status_text"] == "CHANGED WHILE DISCONNECTED"


async def test_health_endpoint_serves_over_http(hud: RunningHud) -> None:
    import httpx

    async with httpx.AsyncClient() as client:
        response = await client.get(f"{hud.http_url}/healthz")
        overlay = await client.get(f"{hud.http_url}/")

    assert response.json()["status"] == "ok"
    assert overlay.status_code == 200
    assert "CaddieJ Floor" in overlay.text


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="asyncio.loop.add_signal_handler is not implemented on Windows; "
    "the runtime fallback is covered in tests/unit/test_runtime_lifecycle.py",
)
async def test_the_running_server_leaves_signal_handlers_alone(hud: RunningHud) -> None:
    # uvicorn installs its own SIGINT and SIGTERM handlers inside serve(), which
    # would shut the server down without the rest of CaddieJ noticing. This runs
    # against the real server so it fails if that hook is ever renamed or moved.
    loop = asyncio.get_running_loop()
    fired = asyncio.Event()
    loop.add_signal_handler(signal.SIGTERM, fired.set)
    try:
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(fired.wait(), timeout=2)

        # The server must still be serving: uvicorn did not see the signal.
        async with websockets.connect(hud.ws_url) as socket:
            assert (
                json.loads(await asyncio.wait_for(socket.recv(), 5))["type"] == "hello"
            )
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


async def test_server_stops_cleanly_with_a_client_attached(hud: RunningHud) -> None:
    async with websockets.connect(hud.ws_url) as socket:
        await asyncio.wait_for(socket.recv(), 5)
        await asyncio.wait_for(socket.recv(), 5)

        await hud.server.stop()
        await asyncio.wait_for(hud.task, 10)

    assert hud.task.done()
