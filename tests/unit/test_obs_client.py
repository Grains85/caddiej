import asyncio
import base64
import logging
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest

from caddiej.adapters.obs import (
    FloorReveals,
    ObsAuthenticationError,
    ObsClient,
    ObsConnectionError,
    ObsRequestError,
    ObsResourceNotFoundError,
    ObsTransportConnectionError,
)
from caddiej.adapters.obs import client as obs_client_module
from caddiej.adapters.obs.screenshot import UnusableScreenshot
from caddiej.domain.events import ShotDetected
from caddiej.obs_reveal import RevealFeature, TimedObsSource
from caddiej.video.jpeg_size import jpeg_size

# Mirrors the defaults reported by OBS 32.2.2 / obs-websocket 5.7.4.
BROWSER_SOURCE_DEFAULTS = {
    "css": "body { background-color: rgba(0, 0, 0, 0); }",
    "fps": 30,
    "fps_custom": False,
    "height": 600,
    "reroute_audio": False,
    "restart_when_active": False,
    "shutdown": False,
    "url": "https://obsproject.com/browser-source",
    "webpage_control_level": 1,
    "width": 800,
}


class FakeTransport:
    def __init__(
        self,
        version_data: Mapping[str, Any] | None = None,
        *,
        connect_delay: float = 0,
    ) -> None:
        self.version_data = version_data or {}
        self.connect_delay = connect_delay
        self.connected = False
        self.disconnected = False
        self.request_timeout: float | None = None
        self.connect_timeout: float | None = None
        self.text_updates: list[tuple[str, str, float]] = []
        self.scene_item_lookups: list[tuple[str, str, float]] = []
        self.visibility_updates: list[tuple[str, int, bool, float]] = []
        self.input_settings_updates: list[tuple[str, Mapping[str, Any]]] = []
        self.created_inputs: list[tuple[str, str, str, Mapping[str, Any]]] = []
        self.input_defaults: Mapping[str, Any] = BROWSER_SOURCE_DEFAULTS
        self.scene_item_missing = False

    async def connect(self, timeout: float) -> None:
        self.connect_timeout = timeout
        async with asyncio.timeout(timeout):
            await asyncio.sleep(self.connect_delay)
        self.connected = True

    async def get_version(self, timeout: float) -> Mapping[str, Any]:
        self.request_timeout = timeout
        return self.version_data

    async def set_text(self, source_name: str, text: str, timeout: float) -> None:
        self.text_updates.append((source_name, text, timeout))

    async def set_input_settings(
        self,
        source_name: str,
        settings: Mapping[str, Any],
        timeout: float,
    ) -> None:
        self.input_settings_updates.append((source_name, dict(settings)))

    async def get_input_default_settings(
        self,
        input_kind: str,
        timeout: float,
    ) -> Mapping[str, Any]:
        return self.input_defaults

    async def create_input(
        self,
        scene_name: str,
        source_name: str,
        input_kind: str,
        settings: Mapping[str, Any],
        timeout: float,
    ) -> None:
        self.created_inputs.append(
            (scene_name, source_name, input_kind, dict(settings))
        )

    async def get_scene_item_id(
        self,
        scene_name: str,
        source_name: str,
        timeout: float,
    ) -> int:
        self.scene_item_lookups.append((scene_name, source_name, timeout))
        if self.scene_item_missing:
            raise ObsResourceNotFoundError(
                f"OBS source '{source_name}' was not found in scene '{scene_name}'"
            )
        return 42

    async def set_scene_item_enabled(
        self,
        scene_name: str,
        scene_item_id: int,
        visible: bool,
        timeout: float,
    ) -> None:
        self.visibility_updates.append((scene_name, scene_item_id, visible, timeout))

    async def disconnect(self) -> None:
        self.disconnected = True
        self.connected = False


class StubResponse:
    def __init__(
        self,
        *,
        succeeded: bool = True,
        data: Mapping[str, Any] | None = None,
        code: int = 100,
        comment: str | None = None,
    ) -> None:
        self.responseData = data
        self.requestStatus = SimpleNamespace(code=code, comment=comment)
        self._succeeded = succeeded

    def ok(self) -> bool:
        return self._succeeded


class RecordingWebSocketClient:
    def __init__(self, responses: list[StubResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[object, float]] = []

    async def call(self, request: object, timeout: float) -> StubResponse:
        self.calls.append((request, timeout))
        return self.responses.pop(0)


def make_recording_transport(
    monkeypatch,
    *responses: StubResponse,
) -> tuple[obs_client_module._SimpleObsWsTransport, RecordingWebSocketClient]:
    websocket = RecordingWebSocketClient(list(responses))
    monkeypatch.setattr(
        obs_client_module.simpleobsws,
        "WebSocketClient",
        lambda **_kwargs: websocket,
    )
    transport = obs_client_module._SimpleObsWsTransport(
        "ws://127.0.0.1:4455",
        "secret",
    )
    return transport, websocket


async def test_obs_client_connects_and_maps_version_information() -> None:
    transport = FakeTransport(
        {
            "obsVersion": "31.0.0",
            "obsWebSocketVersion": "5.6.0",
            "rpcVersion": 1,
        }
    )
    captured: dict[str, str] = {}

    def factory(url: str, password: str) -> FakeTransport:
        captured.update(url=url, password=password)
        return transport

    client = ObsClient(
        host="obs.local",
        port=4455,
        password="secret",
        connect_timeout=3.0,
        transport_factory=factory,
    )

    await client.connect()
    version = await client.get_version()
    await client.set_text("Status", "READY")
    await client.set_scene_item_visibility("Scene", "Status", visible=False)
    await client.disconnect()

    assert captured == {"url": "ws://obs.local:4455", "password": "secret"}
    assert version.obs_version == "31.0.0"
    assert version.websocket_version == "5.6.0"
    assert version.rpc_version == 1
    assert transport.connect_timeout == 3.0
    assert transport.request_timeout == 3.0
    assert transport.text_updates == [("Status", "READY", 3.0)]
    assert transport.scene_item_lookups == [("Scene", "Status", 3.0)]
    assert transport.visibility_updates == [("Scene", 42, False, 3.0)]
    assert transport.disconnected


async def test_text_update_maps_to_obs_input_settings_request(monkeypatch) -> None:
    transport, websocket = make_recording_transport(
        monkeypatch,
        StubResponse(),
    )

    await transport.set_text("CaddieJ Status", "CADDIEJ READY", timeout=2.5)

    request, timeout = websocket.calls[0]
    assert request.requestType == "SetInputSettings"
    assert request.requestData == {
        "inputName": "CaddieJ Status",
        "inputSettings": {"text": "CADDIEJ READY"},
        "overlay": True,
    }
    assert timeout == 2.5


async def test_visibility_resolves_id_and_maps_enabled_request(monkeypatch) -> None:
    transport, websocket = make_recording_transport(
        monkeypatch,
        StubResponse(data={"sceneItemId": 17}),
        StubResponse(),
    )

    scene_item_id = await transport.get_scene_item_id(
        "CaddieJ Test",
        "CaddieJ Status",
        timeout=3,
    )
    await transport.set_scene_item_enabled(
        "CaddieJ Test",
        scene_item_id,
        False,
        timeout=3,
    )

    lookup, visibility = [call[0] for call in websocket.calls]
    assert lookup.requestType == "GetSceneItemId"
    assert lookup.requestData == {
        "sceneName": "CaddieJ Test",
        "sourceName": "CaddieJ Status",
    }
    assert visibility.requestType == "SetSceneItemEnabled"
    assert visibility.requestData == {
        "sceneName": "CaddieJ Test",
        "sceneItemId": 17,
        "sceneItemEnabled": False,
    }


async def test_missing_text_source_raises_caddiej_error(monkeypatch) -> None:
    transport, _ = make_recording_transport(
        monkeypatch,
        StubResponse(
            succeeded=False,
            code=600,
            comment="No source was found",
        ),
    )

    with pytest.raises(
        ObsResourceNotFoundError,
        match="Missing Status",
    ):
        await transport.set_text("Missing Status", "READY", timeout=1)


async def test_missing_scene_item_raises_caddiej_error(monkeypatch) -> None:
    transport, _ = make_recording_transport(
        monkeypatch,
        StubResponse(
            succeeded=False,
            code=600,
            comment="No scene item was found",
        ),
    )

    with pytest.raises(
        ObsResourceNotFoundError,
        match="Missing Scene",
    ):
        await transport.get_scene_item_id(
            "Missing Scene",
            "CaddieJ Status",
            timeout=1,
        )


async def _connected_client(transport: FakeTransport) -> ObsClient:
    client = ObsClient(
        host="localhost",
        port=4455,
        password="",
        connect_timeout=2.0,
        transport_factory=lambda _url, _password: transport,
    )
    await client.connect()
    return client


async def test_ensure_browser_source_creates_it_when_missing() -> None:
    transport = FakeTransport()
    transport.scene_item_missing = True
    client = await _connected_client(transport)

    created = await client.ensure_browser_source(
        "CaddieJ Test",
        "CaddieJ Floor",
        url="http://127.0.0.1:8730/",
        width=1920,
        height=1080,
    )

    assert created is True
    assert transport.created_inputs == [
        (
            "CaddieJ Test",
            "CaddieJ Floor",
            "browser_source",
            {
                "url": "http://127.0.0.1:8730/",
                "width": 1920,
                "height": 1080,
                "shutdown": False,
                "restart_when_active": False,
            },
        )
    ]
    assert transport.input_settings_updates == []


async def test_ensure_browser_source_reconciles_an_existing_one() -> None:
    transport = FakeTransport()
    client = await _connected_client(transport)

    created = await client.ensure_browser_source(
        "CaddieJ Test",
        "CaddieJ Floor",
        url="http://127.0.0.1:8730/",
        width=1280,
        height=720,
    )

    assert created is False
    assert transport.created_inputs == []
    assert transport.input_settings_updates == [
        (
            "CaddieJ Floor",
            {
                "url": "http://127.0.0.1:8730/",
                "width": 1280,
                "height": 720,
                "shutdown": False,
                "restart_when_active": False,
            },
        )
    ]


async def test_ensure_browser_source_rejects_settings_obs_does_not_know() -> None:
    transport = FakeTransport()
    transport.input_defaults = {"url": "", "width": 800, "height": 600}
    client = await _connected_client(transport)

    with pytest.raises(ObsRequestError, match="restart_when_active, shutdown"):
        await client.ensure_browser_source(
            "CaddieJ Test",
            "CaddieJ Floor",
            url="http://127.0.0.1:8730/",
            width=1920,
            height=1080,
        )

    assert transport.created_inputs == []
    assert transport.input_settings_updates == []


async def test_ensure_browser_source_requires_a_connection() -> None:
    transport = FakeTransport()
    client = ObsClient(
        host="localhost",
        port=4455,
        password="",
        connect_timeout=1,
        transport_factory=lambda _url, _password: transport,
    )

    with pytest.raises(ObsConnectionError, match="not connected"):
        await client.ensure_browser_source(
            "CaddieJ Test",
            "CaddieJ Floor",
            url="http://127.0.0.1:8730/",
            width=1920,
            height=1080,
        )


async def test_obs_client_rejects_requests_before_connect() -> None:
    transport = FakeTransport()
    client = ObsClient(
        host="localhost",
        port=4455,
        password="",
        connect_timeout=1,
        transport_factory=lambda _url, _password: transport,
    )

    with pytest.raises(ObsConnectionError, match="not connected"):
        await client.get_version()


async def test_obs_client_times_out_and_cleans_up() -> None:
    transport = FakeTransport(connect_delay=0.05)
    client = ObsClient(
        host="localhost",
        port=4455,
        password="",
        connect_timeout=0.001,
        transport_factory=lambda _url, _password: transport,
    )

    with pytest.raises(ObsTransportConnectionError):
        await client.connect()

    assert transport.disconnected


def test_simpleobsws_transport_receives_plain_password(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class StubWebSocketClient:
        def __init__(
            self,
            *,
            url: str,
            password: str,
            identification_parameters: object,
        ) -> None:
            captured.update(
                url=url,
                password=password,
                identification_parameters=identification_parameters,
            )

    monkeypatch.setattr(
        obs_client_module.simpleobsws,
        "WebSocketClient",
        StubWebSocketClient,
    )

    obs_client_module._SimpleObsWsTransport(
        "ws://127.0.0.1:4455",
        "actual-secret",
    )

    assert captured["url"] == "ws://127.0.0.1:4455"
    assert captured["password"] == "actual-secret"
    assert isinstance(captured["password"], str)
    assert isinstance(
        captured["identification_parameters"],
        obs_client_module.simpleobsws.IdentificationParameters,
    )


async def test_authentication_failure_is_distinct_and_does_not_expose_password(
    monkeypatch,
) -> None:
    class StubWebSocketClient:
        def __init__(self, **_kwargs: object) -> None:
            self.ws = self
            self.close_code = 4009
            self.close_reason = "Authentication Failed"

        async def connect(self) -> None:
            return None

        async def wait_until_identified(self, timeout: float) -> bool:
            return False

        async def disconnect(self) -> None:
            return None

    monkeypatch.setattr(
        obs_client_module.simpleobsws,
        "WebSocketClient",
        StubWebSocketClient,
    )
    transport = obs_client_module._SimpleObsWsTransport(
        "ws://127.0.0.1:4455",
        "never-log-this",
    )

    with pytest.raises(
        ObsAuthenticationError,
        match="authentication failed",
    ) as error:
        await transport.connect(timeout=1)

    assert "never-log-this" not in str(error.value)


def _jpeg(width: int, height: int) -> bytes:
    frame = (
        b"\xff\xc0"
        + (17).to_bytes(2, "big")
        + b"\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + bytes(10)
    )
    return b"\xff\xd8" + frame + b"\xff\xd9"


def _data_uri(data: bytes) -> str:
    return "data:image/jpg;base64," + base64.b64encode(data).decode("ascii")


FLOOR = "CaddieJ Test"
CAPTURE = "CaddieJ Capture"
SOURCES = ("Swing Behind", "Swing Side", "Impact", "All Values")


class _Bay:
    """OBS as the bay has it, with OBS's own rule for what refreshes.

    The floor scene is Program and holds each coach source as a hidden item.
    The capture scene holds them enabled and is nested, enabled and fully
    transparent, in the floor scene. A window capture ticks, and so gives a
    new picture, only while an enabled item reaches it from Program.
    Everything that could put a source on the floor is recorded.
    """

    def __init__(self, *, nested: bool = True, capture_scene: bool = True) -> None:
        self.program = FLOOR
        self.nested = nested
        self.scenes: dict[str, dict[str, dict[str, Any]]] = {
            FLOOR: {
                name: {"sceneItemId": 10 + index, "sceneItemEnabled": False}
                for index, name in enumerate(SOURCES)
            },
        }
        if capture_scene:
            self.scenes[CAPTURE] = {
                name: {"sceneItemId": 20 + index, "sceneItemEnabled": True}
                for index, name in enumerate(SOURCES)
            }
        self.priority = {name: 1 for name in SOURCES}
        self.ticks = dict.fromkeys(SOURCES, 0)
        self.toggles: list[tuple[str, int, bool]] = []
        self.program_switches: list[str] = []
        self.connected = False

    def showing(self, source: str) -> bool:
        floor = self.scenes[self.program].get(source, {})
        if floor.get("sceneItemEnabled"):
            return True
        capture = self.scenes.get(CAPTURE, {}).get(source, {})
        rendered = self.nested or self.program == CAPTURE
        return bool(capture.get("sceneItemEnabled")) and rendered

    def on_floor(self, source: str) -> bool:
        """Drawn on the projector. The nested capture scene draws nothing."""

        return bool(self.scenes[self.program].get(source, {}).get("sceneItemEnabled"))

    async def connect(self, timeout: float) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def get_current_program_scene(self, timeout: float) -> str:
        return self.program

    async def set_current_program_scene(self, scene: str, timeout: float) -> None:
        self.program_switches.append(scene)
        self.program = scene

    async def get_scene_item_id(
        self, scene_name: str, source_name: str, timeout: float
    ) -> int:
        return int(self.scenes[scene_name][source_name]["sceneItemId"])

    async def list_scene_items(self, scene_name: str, timeout: float):
        if scene_name not in self.scenes:
            raise ObsResourceNotFoundError(f"OBS scene '{scene_name}' was not found")
        return tuple(
            {"sourceName": name, **item}
            for name, item in self.scenes[scene_name].items()
        )

    async def set_scene_item_enabled(
        self, scene_name: str, scene_item_id: int, visible: bool, timeout: float
    ) -> None:
        self.toggles.append((scene_name, scene_item_id, visible))
        for item in self.scenes[scene_name].values():
            if item["sceneItemId"] == scene_item_id:
                item["sceneItemEnabled"] = visible

    async def get_source_active(self, source_name: str, timeout: float):
        showing = self.showing(source_name)
        return {"videoActive": showing, "videoShowing": showing}

    async def get_input_settings(self, source_name: str, timeout: float):
        return {
            "inputKind": "window_capture",
            "inputSettings": {"priority": self.priority[source_name]},
        }

    async def set_input_settings(
        self, source_name: str, settings: Mapping[str, Any], timeout: float
    ) -> None:
        self.priority[source_name] = int(settings["priority"])

    async def get_scene_item_transform(
        self, scene_name: str, scene_item_id: int, timeout: float
    ):
        name = next(
            name
            for name, item in self.scenes[scene_name].items()
            if item["sceneItemId"] == scene_item_id
        )
        if self.showing(name) and self.priority[name] == 1:
            return {"sourceWidth": 1792.0, "sourceHeight": 1690.0}
        return {"sourceWidth": 1.0, "sourceHeight": 1.0}

    async def get_source_screenshot(
        self,
        source_name: str,
        image_format: str,
        width: int | None,
        height: int | None,
        quality: int | None,
        timeout: float,
    ) -> str:
        if self.showing(source_name):
            self.ticks[source_name] += 1
        tick = self.ticks[source_name]
        marker = bytes([SOURCES.index(source_name), tick % 251])
        return _data_uri(_jpeg(1792, 1690) + marker * 400)


def _client(transport: object, reveals: FloorReveals | None = None) -> ObsClient:
    return ObsClient(
        "127.0.0.1",
        4455,
        "secret",
        1.0,
        transport_factory=lambda _url, _password: transport,  # type: ignore[arg-type,return-value]
        floor_reveals=reveals if reveals is not None else FloorReveals(),
    )


def _floor_untouched(bay: _Bay) -> None:
    assert bay.toggles == [], "no scene item is ever shown or hidden"
    assert bay.program_switches == [], "the capture scene is never put on Program"
    assert bay.program == FLOOR
    assert not any(bay.on_floor(name) for name in SOURCES)


async def test_a_one_pixel_screenshot_is_rejected() -> None:
    class OnePixel(_Bay):
        async def get_source_screenshot(self, *args: Any, **kwargs: Any) -> str:
            return _data_uri(_jpeg(1, 1))

    client = _client(OnePixel())
    await client.connect()

    with pytest.raises(UnusableScreenshot, match="invalid_capture_dimensions"):
        await client.source_screenshot("Swing Behind")


class TestTheCaptureSceneKeepsSourcesLiveOffTheFloor:
    async def test_every_source_refreshes_and_nothing_is_shown_on_the_floor(
        self,
    ) -> None:
        bay = _Bay()
        client = _client(bay)
        await client.connect()

        async with client.hold_sources(SOURCES, capture_scene=CAPTURE) as states:
            pictures = {
                name: [await client.source_screenshot(name) for _ in range(3)]
                for name in SOURCES
            }
            assert not any(bay.on_floor(name) for name in SOURCES)

        for name, frames in pictures.items():
            assert len(set(frames)) == 3, f"{name} must be a live, changing picture"
        assert all(state.ready for state in states)
        assert [state.rendered for state in states] == [True] * 4
        assert [state.floor_item_enabled for state in states] == [False] * 4
        _floor_untouched(bay)

    async def test_the_old_hidden_floor_item_is_never_made_visible(self) -> None:
        bay = _Bay()
        client = _client(bay)
        await client.connect()

        async with client.hold_sources(SOURCES, capture_scene=CAPTURE):
            for name in SOURCES:
                await client.source_screenshot(name)

        assert not [toggle for toggle in bay.toggles if toggle[0] == FLOOR]
        _floor_untouched(bay)

    async def test_without_the_capture_scene_it_reports_rather_than_exposes(
        self, caplog
    ) -> None:
        caplog.set_level("INFO")
        bay = _Bay(capture_scene=False)
        client = _client(bay)
        await client.connect()

        async with client.hold_sources(SOURCES, capture_scene=CAPTURE) as states:
            first = await client.source_screenshot("Swing Behind")
            second = await client.source_screenshot("Swing Behind")

        assert first == second, "a source nothing shows is frozen, and that is reported"
        assert not any(state.ready for state in states)
        messages = [record.getMessage() for record in caplog.records]
        assert "obs_capture_scene_missing" in messages
        assert "obs_capture_source_not_rendering" in messages
        _floor_untouched(bay)

    async def test_a_capture_scene_that_is_not_nested_is_reported_not_rendering(
        self,
    ) -> None:
        bay = _Bay(nested=False)
        client = _client(bay)
        await client.connect()

        async with client.hold_sources(SOURCES, capture_scene=CAPTURE) as states:
            pass

        assert [state.rendered for state in states] == [False] * 4
        assert "rendered=false" in states[0].describe()
        _floor_untouched(bay)

    async def test_a_capture_scene_that_is_program_is_refused(self, caplog) -> None:
        bay = _Bay()
        client = _client(bay)
        await client.connect()

        async with client.hold_sources(SOURCES, capture_scene=FLOOR) as states:
            pass

        assert "obs_capture_scene_is_program" in [
            r.getMessage() for r in caplog.records
        ]
        assert not any(state.ready for state in states)
        _floor_untouched(bay)

    async def test_diagnostics_say_rendered_and_floor_unchanged(self, caplog) -> None:
        caplog.set_level("INFO")
        bay = _Bay()
        client = _client(bay)
        await client.connect()

        async with client.hold_sources(("Swing Behind",), capture_scene=CAPTURE):
            pass

        state = next(
            r for r in caplog.records if r.getMessage() == "obs_capture_source_state"
        )
        assert state.obs_scene == CAPTURE
        assert "rendered=true" in state.swing_capture_detail
        assert "floor_item=hidden" in state.swing_capture_detail
        assert "obs_capture_floor_unchanged" in [r.getMessage() for r in caplog.records]


class TestNothingCanExposeACaptureSource:
    async def test_an_error_mid_burst_leaves_the_floor_as_it_was(self) -> None:
        bay = _Bay()
        client = _client(bay)
        await client.connect()

        with pytest.raises(RuntimeError):
            async with client.hold_sources(SOURCES, capture_scene=CAPTURE):
                await client.source_screenshot("Impact")
                raise RuntimeError("burst failed")

        _floor_untouched(bay)

    async def test_cancellation_mid_burst_leaves_the_floor_as_it_was(self) -> None:
        bay = _Bay()
        client = _client(bay)
        await client.connect()
        started = asyncio.Event()

        async def burst() -> None:
            async with client.hold_sources(SOURCES, capture_scene=CAPTURE):
                started.set()
                await asyncio.sleep(10)

        task = asyncio.create_task(burst())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        _floor_untouched(bay)

    async def test_a_stale_window_match_is_retargeted_in_the_capture_scene_only(
        self,
    ) -> None:
        bay = _Bay()
        bay.priority["Swing Behind"] = 0
        client = _client(bay)
        await client.connect()

        async with client.hold_sources(("Swing Behind",), capture_scene=CAPTURE):
            data = await client.source_screenshot("Swing Behind")
            assert bay.priority["Swing Behind"] == 1

        assert jpeg_size(data) == (1792, 1690)
        assert bay.priority["Swing Behind"] == 0, "match priority is put back"
        _floor_untouched(bay)

    async def test_a_cancelled_burst_still_restores_the_window_match(self) -> None:
        bay = _Bay()
        bay.priority["Swing Side"] = 0
        client = _client(bay)
        await client.connect()
        started = asyncio.Event()

        async def burst() -> None:
            async with client.hold_sources(("Swing Side",), capture_scene=CAPTURE):
                started.set()
                await asyncio.sleep(10)

        task = asyncio.create_task(burst())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert bay.priority["Swing Side"] == 0
        _floor_untouched(bay)


EVIDENCE = ("Swing Behind", "Swing Side", "All Values")


class _HeldOpen:
    """A reveal's delay passes at once; its duration lasts until released."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.shown = asyncio.Event()
        self._calls = 0

    async def sleep(self, _seconds: float) -> None:
        self._calls += 1
        if self._calls == 1:
            return
        self.shown.set()
        await self.release.wait()


class _ScheduledReveal:
    """A real Top Impact reveal on the floor of the bay capture is reading."""

    def __init__(self, bay: _Bay, reveals: FloorReveals, scene: str = FLOOR) -> None:
        self.clock = _HeldOpen()
        self.reveal = TimedObsSource(
            lambda: _client(bay, reveals),
            scene,
            "Impact",
            feature=RevealFeature.TOP_IMPACT,
            enabled=True,
            sleep=self.clock.sleep,
            capture_scene=CAPTURE,
            floor_reveals=reveals,
        )

    async def show(self) -> None:
        await self.reveal.on_shot_detected(
            ShotDetected(trigger_id="70", source="protee_log")
        )
        await self.clock.shown.wait()

    async def finish(self) -> None:
        self.clock.release.set()
        await self.reveal.aclose()


def _floor_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.getMessage().startswith("obs_capture_floor_")
    ]


class TestTheFloorCheckKnowsWhatWasScheduled:
    """Capture warns when a source it reads changes on the floor mid-burst.

    Top Impact is revealed on that floor a moment after the strike, on purpose,
    and is often the very source capture reads. The reveal is excused because
    it declared the change, not because of its name, so an evidence source —
    or an Impact nobody scheduled — still warns.
    """

    async def test_a_scheduled_impact_reveal_during_capture_does_not_warn(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level("INFO")
        bay = _Bay()
        reveals = FloorReveals()
        reveal = _ScheduledReveal(bay, reveals)
        client = _client(bay, reveals)
        await client.connect()

        async with client.hold_sources(SOURCES, capture_scene=CAPTURE):
            await reveal.show()
            assert bay.on_floor("Impact"), "the reveal still reaches the floor"
        await reveal.finish()

        [record] = _floor_records(caplog)
        assert record.levelno == logging.INFO
        assert record.getMessage() == "obs_capture_floor_revealed"
        assert record.swing_capture_sources == "Impact"
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    @pytest.mark.parametrize("source", EVIDENCE)
    async def test_an_evidence_source_reaching_the_floor_still_warns(
        self, caplog: pytest.LogCaptureFixture, source: str
    ) -> None:
        bay = _Bay()
        reveals = FloorReveals()
        reveal = _ScheduledReveal(bay, reveals)
        client = _client(bay, reveals)
        await client.connect()

        async with client.hold_sources(SOURCES, capture_scene=CAPTURE):
            await reveal.show()
            bay.scenes[FLOOR][source]["sceneItemEnabled"] = True
        await reveal.finish()

        [record] = _floor_records(caplog)
        assert record.levelno == logging.WARNING
        assert record.getMessage() == "obs_capture_floor_changed"
        assert record.swing_capture_sources == source
        assert "also revealed as scheduled: Impact" in record.swing_capture_detail

    @pytest.mark.parametrize("source", EVIDENCE)
    async def test_an_evidence_source_alone_on_the_floor_warns(
        self, caplog: pytest.LogCaptureFixture, source: str
    ) -> None:
        bay = _Bay()
        client = _client(bay)
        await client.connect()

        async with client.hold_sources(SOURCES, capture_scene=CAPTURE):
            bay.scenes[FLOOR][source]["sceneItemEnabled"] = True

        [record] = _floor_records(caplog)
        assert record.getMessage() == "obs_capture_floor_changed"
        assert record.swing_capture_sources == source

    async def test_impact_changing_with_no_reveal_scheduled_still_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        bay = _Bay()
        client = _client(bay)
        await client.connect()

        async with client.hold_sources(SOURCES, capture_scene=CAPTURE):
            bay.scenes[FLOOR]["Impact"]["sceneItemEnabled"] = True

        [record] = _floor_records(caplog)
        assert record.getMessage() == "obs_capture_floor_changed"
        assert record.swing_capture_sources == "Impact"

    async def test_an_unrelated_captured_source_changing_still_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        bay = _Bay()
        bay.scenes[FLOOR]["Delivery"] = {"sceneItemId": 30, "sceneItemEnabled": True}
        reveals = FloorReveals()
        reveal = _ScheduledReveal(bay, reveals)
        client = _client(bay, reveals)
        await client.connect()

        async with client.hold_sources(SOURCES + ("Delivery",), capture_scene=CAPTURE):
            await reveal.show()
            bay.scenes[FLOOR]["Delivery"]["sceneItemEnabled"] = False
        await reveal.finish()

        [record] = _floor_records(caplog)
        assert record.getMessage() == "obs_capture_floor_changed"
        assert record.swing_capture_sources == "Delivery"

    async def test_a_reveal_in_another_scene_does_not_excuse_the_floor(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        bay = _Bay()
        bay.scenes["Elsewhere"] = {
            "Impact": {"sceneItemId": 40, "sceneItemEnabled": False}
        }
        reveals = FloorReveals()
        reveal = _ScheduledReveal(bay, reveals, scene="Elsewhere")
        client = _client(bay, reveals)
        await client.connect()

        async with client.hold_sources(SOURCES, capture_scene=CAPTURE):
            await reveal.show()
            bay.scenes[FLOOR]["Impact"]["sceneItemEnabled"] = True
        await reveal.finish()

        [record] = _floor_records(caplog)
        assert record.getMessage() == "obs_capture_floor_changed"
        assert record.swing_capture_sources == "Impact"

    async def test_a_reveal_finished_before_capture_is_not_carried_into_it(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        bay = _Bay()
        reveals = FloorReveals()
        reveal = _ScheduledReveal(bay, reveals)
        await reveal.show()
        await reveal.finish()
        client = _client(bay, reveals)
        await client.connect()

        async with client.hold_sources(SOURCES, capture_scene=CAPTURE):
            bay.scenes[FLOOR]["Impact"]["sceneItemEnabled"] = True

        [record] = _floor_records(caplog)
        assert record.getMessage() == "obs_capture_floor_changed"

    async def test_nothing_moving_still_reports_unchanged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level("INFO")
        bay = _Bay()
        client = _client(bay)
        await client.connect()

        async with client.hold_sources(SOURCES, capture_scene=CAPTURE):
            pass

        [record] = _floor_records(caplog)
        assert record.levelno == logging.INFO
        assert record.getMessage() == "obs_capture_floor_unchanged"
