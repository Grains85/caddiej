import asyncio
import json
import logging

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from caddiej.adapters.hud import ConnectionRegistry, create_app
from caddiej.adapters.hud.protocol import PROTOCOL_VERSION
from caddiej.adapters.hud.server import _receive_loop, _send_loop
from caddiej.floor import (
    ClubPath,
    FloorPoint,
    FloorService,
    FloorState,
    MarkerVariant,
    ShotMetrics,
)


class FakeWebSocket:
    """Stands in for a Starlette WebSocket in loop-level tests."""

    def __init__(self, incoming: list[dict] | None = None) -> None:
        self.sent: list[str] = []
        self._incoming = list(incoming or [])

    async def send_text(self, text: str) -> None:
        self.sent.append(text)

    async def receive(self) -> dict:
        if self._incoming:
            return self._incoming.pop(0)
        return {"type": "websocket.disconnect", "code": 1000}

    def messages(self) -> list[dict]:
        return [json.loads(text) for text in self.sent]


def text_frame(payload: str) -> dict:
    return {"type": "websocket.receive", "text": payload}


@pytest.fixture
def service() -> FloorService:
    return FloorService()


@pytest.fixture
def registry() -> ConnectionRegistry:
    return ConnectionRegistry()


@pytest.fixture
def client(service: FloorService, registry: ConnectionRegistry) -> TestClient:
    service.add_listener(registry.broadcast)
    return TestClient(create_app(service, registry))


def test_healthz_reports_state_and_client_count(
    client: TestClient, service: FloorService
) -> None:
    service.set_online("CADDIEJ FLOOR ONLINE")

    body = client.get("/healthz").json()

    assert body == {
        "status": "ok",
        "protocol_version": PROTOCOL_VERSION,
        "state_version": 1,
        "mode": "online",
        "clients": 0,
    }


def test_healthz_reports_on_the_subsystems_it_was_given(
    service: FloorService, registry: ConnectionRegistry
) -> None:
    # A disk that has stopped accepting shots is only visible if something says
    # so, and the health endpoint is the thing anybody already looks at.
    probes = {"history": lambda: {"available": False, "shots_dropped": 3}}
    subject = TestClient(create_app(service, registry, probes))

    body = subject.get("/healthz").json()

    assert body["history"] == {"available": False, "shots_dropped": 3}
    assert body["status"] == "ok"


def test_healthz_reports_nothing_extra_when_given_no_subsystems(
    client: TestClient,
) -> None:
    assert set(client.get("/healthz").json()) == {
        "status",
        "protocol_version",
        "state_version",
        "mode",
        "clients",
    }


def test_index_serves_the_overlay(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "CaddieJ Floor" in response.text
    assert "/static/js/client.js" in response.text


def test_static_assets_are_served(client: TestClient) -> None:
    assert client.get("/static/css/floor.css").status_code == 200
    assert client.get("/static/js/client.js").status_code == 200


def test_new_client_receives_hello_then_current_state(
    client: TestClient, service: FloorService
) -> None:
    service.set_online("CADDIEJ FLOOR ONLINE")

    with client.websocket_connect("/ws") as websocket:
        hello = json.loads(websocket.receive_text())
        snapshot = json.loads(websocket.receive_text())

    assert hello["type"] == "hello"
    assert snapshot["type"] == "snapshot"
    assert snapshot["state"]["status_text"] == "CADDIEJ FLOOR ONLINE"
    assert snapshot["state"]["mode"] == "online"


def test_a_reconnecting_client_receives_the_whole_floor(
    client: TestClient, service: FloorService
) -> None:
    # A Browser Source can reload at any time. Whatever is on the floor has to
    # come back from one snapshot, including elements set long before.
    service.set_online("CADDIEJ FLOOR ONLINE")
    service.show_marker(FloorPoint(x=0.2, y=0.85), MarkerVariant.PUTTER)
    service.set_metrics(
        ShotMetrics(ball_speed_mph=93.4, launch_angle_degrees=21.5, carry_yards=142.0)
    )
    service.set_club_path(
        ClubPath(start=FloorPoint(x=0.2, y=0.85), angle_degrees=90.0, length=0.25)
    )

    with client.websocket_connect("/ws") as first:
        first.receive_text()  # hello
        first.receive_text()  # snapshot

    # A second connection, as if the page had reloaded, sees the same floor.
    with client.websocket_connect("/ws") as second:
        second.receive_text()  # hello
        snapshot = json.loads(second.receive_text())

    state = snapshot["state"]
    assert state["version"] == service.state.version
    assert state["status_text"] == "CADDIEJ FLOOR ONLINE"
    assert state["marker"] == {
        "position": {"x": 0.2, "y": 0.85},
        "variant": "putter",
        "visible": True,
    }
    assert state["metrics"]["carry_yards"] == 142.0
    assert state["club_path"]["end"]["x"] == pytest.approx(0.45)


def test_a_reconnecting_client_sees_a_hidden_marker_as_hidden(
    client: TestClient, service: FloorService
) -> None:
    service.set_online("CADDIEJ FLOOR ONLINE")
    service.show_marker(FloorPoint(x=0.5, y=0.5))
    service.hide_marker()

    with client.websocket_connect("/ws") as websocket:
        websocket.receive_text()  # hello
        snapshot = json.loads(websocket.receive_text())

    assert snapshot["state"]["marker"]["visible"] is False


def test_marker_changes_reach_a_connected_client(
    client: TestClient, service: FloorService
) -> None:
    service.set_online("CADDIEJ FLOOR ONLINE")

    with client.websocket_connect("/ws") as websocket:
        websocket.receive_text()  # hello
        websocket.receive_text()  # initial snapshot

        service.show_marker(FloorPoint(x=0.1, y=0.9), MarkerVariant.DRIVER)
        shown = json.loads(websocket.receive_text())
        service.hide_marker()
        hidden = json.loads(websocket.receive_text())

    assert shown["state"]["marker"]["variant"] == "driver"
    assert shown["state"]["marker"]["visible"] is True
    assert hidden["state"]["marker"]["visible"] is False
    assert hidden["seq"] > shown["seq"]


def test_resync_returns_the_latest_state(
    client: TestClient, service: FloorService
) -> None:
    service.set_online("FIRST")

    with client.websocket_connect("/ws") as websocket:
        websocket.receive_text()  # hello
        websocket.receive_text()  # initial snapshot
        websocket.send_text('{"type": "resync"}')
        snapshot = json.loads(websocket.receive_text())

    assert snapshot["type"] == "snapshot"
    assert snapshot["state"]["status_text"] == "FIRST"


def test_ping_is_answered(client: TestClient) -> None:
    with client.websocket_connect("/ws") as websocket:
        websocket.receive_text()
        websocket.receive_text()
        websocket.send_text('{"type": "ping"}')

        assert json.loads(websocket.receive_text())["type"] == "pong"


def test_malformed_message_is_rejected_without_dropping_the_connection(
    client: TestClient,
) -> None:
    with client.websocket_connect("/ws") as websocket:
        websocket.receive_text()
        websocket.receive_text()
        websocket.send_text("this is not json")
        error = json.loads(websocket.receive_text())

        websocket.send_text('{"type": "ping"}')
        assert json.loads(websocket.receive_text())["type"] == "pong"

    assert error["type"] == "error"


def test_unsupported_message_type_is_reported(client: TestClient) -> None:
    with client.websocket_connect("/ws") as websocket:
        websocket.receive_text()
        websocket.receive_text()
        websocket.send_text('{"type": "set_state"}')
        error = json.loads(websocket.receive_text())

    assert error["type"] == "error"
    assert "set_state" in error["detail"]


def test_binary_frame_does_not_kill_the_connection(client: TestClient) -> None:
    with client.websocket_connect("/ws") as websocket:
        websocket.receive_text()
        websocket.receive_text()
        websocket.send_bytes(b"\x00\x01\x02")
        error = json.loads(websocket.receive_text())

        websocket.send_text('{"type": "ping"}')
        assert json.loads(websocket.receive_text())["type"] == "pong"

    assert error["type"] == "error"


def test_cross_origin_handshake_is_rejected(
    client: TestClient, registry: ConnectionRegistry
) -> None:
    # WebSockets are exempt from the same-origin policy, so any page the user
    # visits could otherwise read the feed and write to CaddieJ's logs.
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/ws", headers={"Origin": "https://evil.example.com"}
        ) as websocket:
            websocket.receive_text()

    assert registry.count == 0


def test_same_origin_handshake_is_accepted(client: TestClient) -> None:
    with client.websocket_connect("/ws", headers={"Origin": "http://testserver"}) as ws:
        assert json.loads(ws.receive_text())["type"] == "hello"


def test_client_without_an_origin_is_accepted(client: TestClient) -> None:
    # Non-browser clients, including CaddieJ's own integration tests, send none.
    with client.websocket_connect("/ws") as websocket:
        assert json.loads(websocket.receive_text())["type"] == "hello"


def test_disconnect_unregisters_the_client(
    client: TestClient, registry: ConnectionRegistry
) -> None:
    with client.websocket_connect("/ws") as websocket:
        websocket.receive_text()
        websocket.receive_text()
        assert registry.count == 1

    assert registry.count == 0


async def test_send_loop_pushes_each_new_state() -> None:
    websocket = FakeWebSocket()
    registry = ConnectionRegistry()
    connection = registry.register()
    service = FloorService()
    service.add_listener(registry.broadcast)

    task = asyncio.create_task(_send_loop(websocket, connection))  # type: ignore[arg-type]
    service.set_online("ONLINE")
    await asyncio.sleep(0)
    service.set_status_text("UPDATED")
    await asyncio.sleep(0)
    connection.close()
    await asyncio.wait_for(task, 1)

    texts = [message["state"]["status_text"] for message in websocket.messages()]
    assert texts == ["ONLINE", "UPDATED"]


async def test_send_loop_skips_superseded_states() -> None:
    websocket = FakeWebSocket()
    connection = ConnectionRegistry().register()
    first = FloorState().evolve(status_text="FIRST")
    connection.offer(first)
    connection.offer(first.evolve(status_text="SECOND"))

    task = asyncio.create_task(_send_loop(websocket, connection))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    connection.close()
    await asyncio.wait_for(task, 1)

    assert [m["state"]["status_text"] for m in websocket.messages()] == ["SECOND"]
    assert connection.dropped == 1


async def test_receive_loop_cannot_change_state() -> None:
    service = FloorService()
    service.set_online("SERVER OWNED")
    websocket = FakeWebSocket(
        [
            text_frame('{"type": "set_status_text", "message": "CLIENT OWNED"}'),
            text_frame('{"type": "hello", "surface": "floor"}'),
        ]
    )

    with pytest.raises(WebSocketDisconnect):
        await _receive_loop(websocket, service)  # type: ignore[arg-type]

    assert service.state.status_text == "SERVER OWNED"
    assert service.state.version == 1


async def test_non_text_frame_is_rejected_rather_than_crashing() -> None:
    # A binary frame previously raised KeyError out of the handler, because
    # receive_text assumes the frame carries text.
    service = FloorService()
    websocket = FakeWebSocket(
        [
            {"type": "websocket.receive", "bytes": b"\x00\x01\x02"},
            text_frame('{"type": "ping"}'),
        ]
    )

    with pytest.raises(WebSocketDisconnect):
        await _receive_loop(websocket, service)  # type: ignore[arg-type]

    types = [message["type"] for message in websocket.messages()]
    assert types == ["error", "pong"]


async def test_oversized_client_log_is_truncated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Our own client truncates, but a hostile or buggy one will not.
    service = FloorService()
    payload = json.dumps({"type": "client_log", "message": "X" * 5000})
    websocket = FakeWebSocket([text_frame(payload)])

    with caplog.at_level(logging.WARNING, logger="caddiej.adapters.hud.server"):
        with pytest.raises(WebSocketDisconnect):
            await _receive_loop(websocket, service)  # type: ignore[arg-type]

    logged = [r for r in caplog.records if r.getMessage() == "hud_client_log"]
    assert len(logged) == 1
    assert len(logged[0].hud_client_message) == 500
