import asyncio
import json
import logging
import re
from pathlib import Path

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

from caddiej.adapters.hud import ConnectionRegistry, HudConnection, create_app
from caddiej.adapters.hud.protocol import PROTOCOL_VERSION
from caddiej.adapters.hud.server import (
    STATIC_ROOT,
    _receive_loop,
    _send_loop,
    overlay_assets_version,
)
from caddiej.floor import (
    ClubPath,
    FloorAdvice,
    FloorPoint,
    FloorService,
    FloorState,
    MarkerVariant,
    RoundResultView,
    ShotMetrics,
)

ASSETS_VERSION_IS_HASH = re.compile(r'const ASSETS_VERSION = "[0-9a-f]{12}";')


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
        # Every socket the session has opened, not just the live ones. A floor
        # reconnecting in a loop leaves `clients` at 1 the whole time; one
        # recorded session did it 8,141 times and nothing here showed it.
        "client_connects": 0,
        "client_silent_seconds": None,
        "hud_static_root": str(STATIC_ROOT.resolve()),
        "hud_assets_version": overlay_assets_version(),
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
        "client_connects",
        "client_silent_seconds",
        "hud_static_root",
        "hud_assets_version",
    }


def test_index_serves_the_overlay(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "CaddieJ Floor" in response.text
    version = overlay_assets_version()
    assert f"/static/hud/{version}/client.js" in response.text
    assert f"/static/hud/{version}/floor.css" in response.text
    assert "/static/js/client.js" not in response.text
    assert response.headers["cache-control"].startswith("no-store")
    assert version in response.text


def test_static_assets_are_served(client: TestClient) -> None:
    css = client.get("/static/css/floor.css")
    js = client.get("/static/js/client.js")
    assert css.status_code == 200
    assert js.status_code == 200
    assert css.headers["cache-control"].startswith("no-store")
    assert js.headers["cache-control"].startswith("no-store")


def test_changing_the_overlay_changes_the_path_the_browser_asks_for(
    tmp_path: Path,
) -> None:
    """The mechanism OBS's CEF cache is defeated by, checked rather than assumed.

    The hash covers the overlay HTML/JS/CSS the Browser Source executes, so any
    edit to the markup, its renderer or its stylesheet produces a different
    directory name and a refresh cannot keep running the previous one. A query
    string would not: CEF treats `state.js?v=a` and `state.js?v=b` as the same
    module.
    """

    for relative in ("js/client.js", "js/state.js", "css/floor.css", "index.html"):
        copy = tmp_path / relative
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_bytes((STATIC_ROOT / relative).read_bytes())

    before = overlay_assets_version(tmp_path)
    assert before == overlay_assets_version(), "same bytes, same version"

    css = tmp_path / "css/floor.css"
    css.write_text(css.read_text(encoding="utf-8") + "\n/* edit */\n", encoding="utf-8")
    after_css = overlay_assets_version(tmp_path)
    assert after_css != before

    html = tmp_path / "index.html"
    html.write_text(
        html.read_text(encoding="utf-8") + "\n<!-- edit -->\n", encoding="utf-8"
    )
    assert overlay_assets_version(tmp_path) != after_css


def test_overlay_js_renders_display_strings_not_raw_round_numbers(
    client: TestClient,
) -> None:
    version = overlay_assets_version()
    served = client.get(f"/static/hud/{version}/state.js")
    source = served.text
    imported = client.get(f"/static/hud/{version}/client.js").text

    assert served.status_code == 200
    assert served.headers["x-caddiej-assets-version"] == version
    # Version is the directory. A query string here is what CEF ignores.
    assert 'from "./state.js"' in imported
    assert "state.js?v=" not in imported
    assert "__HUD_ASSETS_VERSION__" not in imported
    assert ASSETS_VERSION_IS_HASH.search(imported)
    assert "ball_speed_display" in source
    assert "shot_distance_display" in source
    assert "ball_speed_raw" not in source
    assert "total_distance_raw" not in source
    assert "formatRaw(result.ball_speed" not in source
    assert "formatRaw(result.total_distance" not in source
    assert "address.distance_display" in source
    assert "address.distance_raw" not in source


def test_the_served_html_imports_only_path_versioned_modules(
    client: TestClient,
) -> None:
    """OBS CEF caches `/static/js/state.js` even when `?v=` changes.

    The graph the Browser Source actually loads must put the hash in the
    pathname of every file, and `client.js` must then import `state.js`
    relatively so that request is the same versioned directory.
    """

    version = overlay_assets_version()
    html = client.get("/").text
    client_url = f"/static/hud/{version}/client.js"
    css_url = f"/static/hud/{version}/floor.css"
    assert client_url in html
    assert css_url in html

    imported = client.get(client_url).text
    state = client.get(f"/static/hud/{version}/state.js")
    css = client.get(css_url)

    assert 'from "./state.js"' in imported
    assert 'from "./state.js?v=' not in imported
    assert state.status_code == 200
    assert css.status_code == 200
    assert "ball_speed_raw" not in state.text
    assert client.get("/static/hud/not-a-file/nope.js").status_code == 404


def test_static_root_is_this_checkout() -> None:
    checkout = Path(__file__).resolve().parents[2] / "src/caddiej/adapters/hud/static"
    assert STATIC_ROOT.resolve() == checkout


def test_snapshot_http_matches_websocket_and_hides_raw_round_floats(
    client: TestClient, service: FloorService
) -> None:
    raw_speed = 32.83863067626953
    raw_distance = 28.0288715
    service.set_round_result(
        RoundResultView(
            shot_id="D",
            ball_speed_raw=raw_speed,
            total_distance_raw=raw_distance,
        ),
        None,
    )

    http_body = client.get("/snapshot").json()
    with client.websocket_connect("/ws") as websocket:
        websocket.receive_text()  # hello
        ws_body = json.loads(websocket.receive_text())

    assert http_body == ws_body
    panel = http_body["state"]["round_result"]
    wire = json.dumps(panel)
    assert panel["ball_speed_display"] == "118 km/h"
    assert panel["shot_distance_display"] == "28 m"
    assert "ball_speed_raw" not in panel
    assert "total_distance_raw" not in panel
    assert "32.83863067626953" not in wire
    assert "28.0288715" not in wire
    assert service.state.round_result is not None
    assert service.state.round_result.ball_speed_raw == raw_speed
    assert service.state.round_result.total_distance_raw == raw_distance


def test_new_client_receives_hello_then_current_state(
    client: TestClient, service: FloorService
) -> None:
    service.set_online("CADDIEJ FLOOR ONLINE")

    with client.websocket_connect("/ws") as websocket:
        hello = json.loads(websocket.receive_text())
        snapshot = json.loads(websocket.receive_text())

    assert hello["type"] == "hello"
    assert hello["hud_assets_version"] == overlay_assets_version()
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


async def drain_received(
    frames: list[dict], service: FloorService | None = None
) -> tuple[HudConnection, list[dict]]:
    """Run the receive loop to disconnect and read back what it queued.

    The receive loop does not write to the socket. It hands replies to the
    connection and the one send task writes them, because two tasks writing
    one Starlette socket interleave into a corrupt frame.
    """

    connection = HudConnection()
    websocket = FakeWebSocket(frames)
    with pytest.raises(WebSocketDisconnect):
        await _receive_loop(
            connection,
            websocket,  # type: ignore[arg-type]
            service or FloorService(),
            overlay_assets_version(),
        )

    queued: list[dict] = []
    while True:
        try:
            text = await asyncio.wait_for(connection.next_outbound(), 0.05)
        except TimeoutError:
            return connection, queued
        if text is None:
            return connection, queued
        queued.append(json.loads(text))


async def test_receive_loop_cannot_change_state() -> None:
    service = FloorService()
    service.set_online("SERVER OWNED")

    await drain_received(
        [
            text_frame('{"type": "set_status_text", "message": "CLIENT OWNED"}'),
            text_frame('{"type": "hello", "surface": "floor"}'),
        ],
        service,
    )

    assert service.state.status_text == "SERVER OWNED"
    assert service.state.version == 1


async def test_non_text_frame_is_rejected_rather_than_crashing() -> None:
    # A binary frame previously raised KeyError out of the handler, because
    # receive_text assumes the frame carries text.
    _connection, queued = await drain_received(
        [
            {"type": "websocket.receive", "bytes": b"\x00\x01\x02"},
            text_frame('{"type": "ping"}'),
        ]
    )

    assert [message["type"] for message in queued] == ["error", "pong"]


async def test_the_receive_loop_never_writes_to_the_socket_itself() -> None:
    """The single-writer rule, asserted rather than assumed.

    Starlette does not serialise concurrent calls to its `send` callable, so a
    reply written from here while the send task is writing a snapshot is a
    torn frame — which the bay sees as a projector that reconnects for no
    reason anybody can point at.
    """

    connection = HudConnection()
    websocket = FakeWebSocket([text_frame('{"type": "ping"}')])

    with pytest.raises(WebSocketDisconnect):
        await _receive_loop(
            connection,
            websocket,  # type: ignore[arg-type]
            FloorService(),
            overlay_assets_version(),
        )

    assert websocket.messages() == []
    assert await connection.next_outbound() is not None


async def test_a_resync_is_answered_with_state_not_a_second_writer() -> None:
    service = FloorService()
    service.set_online("RESYNCED")

    _connection, queued = await drain_received(
        [text_frame('{"type": "resync"}')], service
    )

    assert [message["state"]["status_text"] for message in queued] == ["RESYNCED"]


async def test_oversized_client_log_is_truncated(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Our own client truncates, but a hostile or buggy one will not.
    payload = json.dumps({"type": "client_log", "message": "X" * 5000})

    with caplog.at_level(logging.WARNING, logger="caddiej.adapters.hud.server"):
        await drain_received([text_frame(payload)])

    logged = [r for r in caplog.records if r.getMessage() == "hud_client_log"]
    assert len(logged) == 1
    assert len(logged[0].hud_client_message) == 500


async def test_a_hello_reports_which_overlay_build_the_browser_loaded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    frame = text_frame(
        '{"type": "hello", "surface": "floor", "assets_version": "abc123abc123"}'
    )

    with caplog.at_level(logging.INFO, logger="caddiej.adapters.hud.server"):
        await drain_received([frame])

    hello = [r for r in caplog.records if r.getMessage() == "hud_client_hello"]
    stale = [r for r in caplog.records if r.getMessage() == "overlay_assets_stale"]
    assert len(hello) == 1
    assert hello[0].hud_assets_version == "abc123abc123"
    assert len(stale) == 1
    assert stale[0].hud_assets_expected == overlay_assets_version()


async def test_an_overlay_too_old_to_report_its_build_is_still_caught(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The case the staleness check was built for and could not see.

    A cached overlay from before `assets_version` existed reports nothing, and
    the detector used to run only when the field was present — so the one page
    guaranteed to be stale was the one page that went unreported.
    """

    with caplog.at_level(logging.INFO, logger="caddiej.adapters.hud.server"):
        await drain_received([text_frame('{"type": "hello", "surface": "floor"}')])

    stale = [r for r in caplog.records if r.getMessage() == "overlay_assets_stale"]
    assert len(stale) == 1
    assert stale[0].hud_assets_version == "unreported"


async def test_a_hello_from_the_current_build_is_not_reported_stale(
    caplog: pytest.LogCaptureFixture,
) -> None:
    frame = text_frame(
        json.dumps(
            {
                "type": "hello",
                "surface": "floor",
                "assets_version": overlay_assets_version(),
            }
        )
    )

    with caplog.at_level(logging.INFO, logger="caddiej.adapters.hud.server"):
        await drain_received([frame])

    assert not [r for r in caplog.records if r.getMessage() == "overlay_assets_stale"]


class TestAStaleOverlayIsNotTrustedWithTheCoaching:
    """What happens to a page that cannot be brought up to the served build.

    The first hardware test had two overlays connected. One reconnected onto
    the current assets; the other reported a different version, answered the
    reload request with `unknown message type: reload` -- it predates the
    handler -- and carried on drawing the coaching with a layout whose faults
    had already been found. Nothing was ever going to reload it, so the
    screenshot taken off the projector was of a renderer nobody was looking at
    the source of.

    There is no message that fixes such a page. There is one that stops it
    being wrong: `snapshot`, which every build has always understood. A client
    on the wrong build gets its snapshots with the lesson taken out, so the
    worst it can show is nothing.
    """

    @staticmethod
    def snapshots(queued: list[dict]) -> list[dict]:
        """Only the state frames; a stale client is also sent a reload reply."""

        return [m["state"] for m in queued if m["type"] == "snapshot"]

    @staticmethod
    def a_coached_floor() -> FloorService:
        service = FloorService()
        service.set_advice(
            FloorAdvice(
                text="DIP TRAIL SHOULDER",
                observation="OUT-TO-IN PATH",
                swing_score=76,
            )
        )
        return service

    async def test_a_client_on_another_build_is_sent_no_coaching(self) -> None:
        service = self.a_coached_floor()
        frame = text_frame(
            '{"type": "hello", "surface": "floor", "assets_version": "old"}'
        )

        connection, queued = await drain_received([frame], service)

        assert connection.stale
        assert [state["advice"] for state in self.snapshots(queued)] == [None]

    async def test_the_shot_still_reaches_a_client_on_another_build(self) -> None:
        """Only the lesson is withheld, not the floor.

        A stale overlay draws the marker and the ball status correctly -- that
        geometry barely changes between builds -- and blanking it would take
        the bay off the air to fix a layout bug.
        """

        service = self.a_coached_floor()
        service.set_online("CADDIEJ FLOOR ONLINE")
        frame = text_frame(
            '{"type": "hello", "surface": "floor", "assets_version": "old"}'
        )

        _connection, queued = await drain_received([frame], service)

        assert [state["status_text"] for state in self.snapshots(queued)] == [
            "CADDIEJ FLOOR ONLINE"
        ]

    async def test_a_client_that_reloads_onto_the_served_build_is_trusted_again(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A page that does understand `reload` recovers inside one handshake.

        Same connection, because the overlay reconnects its socket rather than
        opening a second one, and the lesson comes back with the hello rather
        than with the next shot: the floor has a result on it and there is no
        reason for the player to wait for another swing to see it.
        """

        service = self.a_coached_floor()
        frames = [
            text_frame(
                '{"type": "hello", "surface": "floor", "assets_version": "old"}'
            ),
            text_frame(
                json.dumps(
                    {
                        "type": "hello",
                        "surface": "floor",
                        "assets_version": overlay_assets_version(),
                    }
                )
            ),
        ]

        with caplog.at_level(logging.INFO, logger="caddiej.adapters.hud.server"):
            connection, queued = await drain_received(frames, service)

        # One snapshot, not two: state is newest-wins, so the blanked one is
        # superseded by the restored one before the send task ever sees it.
        # Which is the point -- the client is never left holding the blank.
        assert not connection.stale
        assert [state["advice"] is not None for state in self.snapshots(queued)] == [
            True
        ]
        assert [r for r in caplog.records if r.getMessage() == "overlay_assets_current"]

    async def test_the_log_says_which_page_was_refused(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The field that was missing when two clients had to be told apart."""

        frame = text_frame(
            json.dumps(
                {
                    "type": "hello",
                    "surface": "floor",
                    "assets_version": "old",
                    "page_id": "k3f9a1b2",
                    "href": "http://127.0.0.1:8777/?hud_assets=old",
                }
            )
        )

        with caplog.at_level(logging.INFO, logger="caddiej.adapters.hud.server"):
            await drain_received([frame])

        hello = [r for r in caplog.records if r.getMessage() == "hud_client_hello"]
        stale = [r for r in caplog.records if r.getMessage() == "overlay_assets_stale"]
        assert hello[0].hud_page_id == "k3f9a1b2"
        assert hello[0].hud_client_href == "http://127.0.0.1:8777/?hud_assets=old"
        assert stale[0].hud_page_id == "k3f9a1b2"

    async def test_an_overlong_page_id_is_truncated(self) -> None:
        """Our own page sends eight characters; a buggy one need not."""

        frame = text_frame(
            json.dumps(
                {
                    "type": "hello",
                    "page_id": "P" * 500,
                    "href": "H" * 5000,
                    "assets_version": overlay_assets_version(),
                }
            )
        )

        connection, _queued = await drain_received([frame])

        assert len(connection.page_id) == 64
        assert len(connection.href) == 200
