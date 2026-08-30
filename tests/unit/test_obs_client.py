import asyncio
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest

from caddiej.adapters.obs import (
    ObsAuthenticationError,
    ObsClient,
    ObsConnectionError,
    ObsRequestError,
    ObsResourceNotFoundError,
    ObsTransportConnectionError,
)
from caddiej.adapters.obs import client as obs_client_module

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
