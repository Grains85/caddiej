"""The only place Hue's own shape is asserted.

Everything here is about translation: a resource document with a dozen fields
becomes an identifier, a flag and a fraction; a fraction becomes a percentage and
a millisecond duration. A stub stands in for the HTTP client, so none of this
needs a bridge, a network, or the client library installed.
"""

from typing import Any

import pytest

from caddiej.adapters.hue import (
    BRIGHTNESS_MAX,
    BRIGHTNESS_MIN,
    KEY_HEADER,
    HueBridge,
    HueLights,
    to_fraction,
    to_percent,
)
from caddiej.lighting.port import (
    LightingCommandError,
    LightingUnavailableError,
    LightState,
)


def a_light(
    identifier: str = "abc",
    name: str = "Sitting room",
    on: bool = True,
    brightness: float | None = 80.0,
) -> dict[str, Any]:
    """One light resource, shaped as a bridge reports it."""

    resource: dict[str, Any] = {
        "id": identifier,
        "id_v1": "/lights/3",
        "type": "light",
        "metadata": {"name": name, "archetype": "table_shade"},
        "on": {"on": on},
    }
    if brightness is not None:
        resource["dimming"] = {"brightness": brightness, "min_dim_level": 0.2}
    return resource


class StubResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class StubClient:
    """Stands in for the HTTP client, and records everything asked of it."""

    def __init__(
        self,
        payload: Any = None,
        status_code: int = 200,
        error: BaseException | None = None,
    ) -> None:
        self.payload = payload if payload is not None else {"data": []}
        self.status_code = status_code
        self.error = error
        self.requests: list[tuple[str, str, Any]] = []
        self.closed = 0

    async def request(self, method: str, path: str, json: Any = None) -> StubResponse:
        self.requests.append((method, path, json))
        if self.error is not None:
            raise self.error
        payload = self.payload(path) if callable(self.payload) else self.payload
        return StubResponse(payload, self.status_code)

    async def aclose(self) -> None:
        self.closed += 1


def a_bridge(client: StubClient) -> HueBridge:
    return HueBridge(host="10.0.0.5", application_key="secret", client=client)


class TestTheBrightnessScale:
    def test_a_fraction_becomes_hue_s_percentage(self) -> None:
        assert to_percent(1.0) == BRIGHTNESS_MAX
        assert to_percent(0.5) == 50.0

    def test_zero_becomes_the_dimmest_legal_value_rather_than_zero(self) -> None:
        """Hue refuses a brightness of zero. The service floors its fractions
        well above this, so the conversion never has to invent a value — but a
        conversion that could emit an illegal one is a conversion that will."""

        assert to_percent(0.0) == BRIGHTNESS_MIN
        assert to_percent(-1.0) == BRIGHTNESS_MIN

    def test_more_than_full_brightness_is_clamped(self) -> None:
        assert to_percent(2.0) == BRIGHTNESS_MAX

    def test_a_percentage_comes_back_as_a_fraction(self) -> None:
        assert to_fraction(100.0) == 1.0
        assert to_fraction(50.0) == 0.5
        assert to_fraction(0.0) == 0.0

    def test_the_round_trip_holds_within_the_scale(self) -> None:
        for fraction in (0.01, 0.25, 0.5, 0.99, 1.0):
            assert to_fraction(to_percent(fraction)) == pytest.approx(
                fraction, abs=0.001
            )


class TestTalkingToABridge:
    def test_an_address_is_accepted_with_or_without_a_scheme(self) -> None:
        for host in ("10.0.0.5", "https://10.0.0.5", "https://10.0.0.5/"):
            assert HueBridge(host=host, application_key="k").host == "10.0.0.5"

    def test_a_bridge_with_no_key_is_not_configured(self) -> None:
        assert HueBridge(host="10.0.0.5", application_key="").is_configured is False

    def test_a_bridge_with_no_address_is_not_configured(self) -> None:
        assert HueBridge(host="", application_key="k").is_configured is False

    async def test_the_key_travels_as_a_header_and_never_in_a_path(self) -> None:
        """Where it is a credential rather than part of an address, which is what
        keeps it out of any log that records a URL."""

        module = StubModule()
        bridge = HueBridge(host="10.0.0.5", application_key="the-secret", module=module)
        await bridge.open()

        assert module.headers == {KEY_HEADER: "the-secret"}
        assert "the-secret" not in module.base_url

    async def test_certificate_verification_is_off_because_a_bridge_is_self_signed(
        self,
    ) -> None:
        module = StubModule()
        await HueBridge(host="10.0.0.5", application_key="k", module=module).open()

        assert module.verify is False

    async def test_lights_are_read_from_the_clip_v2_resource(self) -> None:
        client = StubClient({"data": [a_light()], "errors": []})
        bridge = a_bridge(client)

        assert len(await bridge.get("light")) == 1
        assert client.requests == [("GET", "/clip/v2/resource/light", None)]

    async def test_a_refusal_in_the_body_of_a_success_is_still_a_failure(self) -> None:
        """Hue reports refusals inside a 200, so the status code alone is not
        enough to know a command was carried out."""

        client = StubClient(
            {"data": [], "errors": [{"description": "device is not responding"}]}
        )

        with pytest.raises(LightingCommandError, match="not responding"):
            await a_bridge(client).get("light")

    async def test_a_rejected_key_is_terminal_rather_than_worth_retrying(self) -> None:
        for status in (401, 403):
            client = StubClient({"data": []}, status_code=status)
            with pytest.raises(LightingUnavailableError, match="application key"):
                await a_bridge(client).get("light")

    async def test_any_other_bad_status_is_worth_retrying(self) -> None:
        client = StubClient({"data": []}, status_code=503)

        with pytest.raises(LightingCommandError):
            await a_bridge(client).get("light")

    async def test_an_unreachable_bridge_is_a_command_failure(self) -> None:
        client = StubClient(error=OSError("no route to host"))

        with pytest.raises(LightingCommandError, match="no route"):
            await a_bridge(client).get("light")

    async def test_an_unreadable_answer_is_a_command_failure(self) -> None:
        client = StubClient(payload=ValueError("not json"))

        with pytest.raises(LightingCommandError):
            await a_bridge(client).get("light")

    async def test_a_bridge_that_was_never_opened_says_so(self) -> None:
        bridge = HueBridge(host="10.0.0.5", application_key="k", module=None)

        with pytest.raises(LightingUnavailableError):
            await bridge.get("light")

    async def test_several_lights_are_written_at_once(self) -> None:
        """Four lamps in series at thirty milliseconds each would put the last a
        tenth of a second behind the first, which is visible as a ripple."""

        client = StubClient({"data": [], "errors": []})
        await a_bridge(client).put_all(
            "light", {"a": {"dimming": {"brightness": 50.0}}, "b": {}}
        )

        assert {path for _, path, _ in client.requests} == {
            "/clip/v2/resource/light/a",
            "/clip/v2/resource/light/b",
        }

    async def test_one_light_refusing_does_not_stop_the_others(self) -> None:
        """A partial update is the better outcome: the lamps that could be
        reached stay in step with the voice."""

        def payload(path: str) -> dict[str, Any]:
            if path.endswith("/bad"):
                return {"errors": [{"description": "unreachable"}]}
            return {"data": [], "errors": []}

        client = StubClient(payload=payload)
        with pytest.raises(LightingCommandError, match="1 of 3"):
            await a_bridge(client).put_all("light", {"a": {}, "bad": {}, "c": {}})

        assert len(client.requests) == 3

    async def test_closing_releases_the_client(self) -> None:
        client = StubClient()
        bridge = a_bridge(client)
        await bridge.aclose()

        assert client.closed == 1


class StubModule:
    """Stands in for the HTTP library, recording how the client was built."""

    def __init__(self) -> None:
        self.base_url = ""
        self.headers: dict[str, str] = {}
        self.verify: object = None
        self.timeout: float = 0.0

    def AsyncClient(  # noqa: N802 - mirrors the library's own name
        self,
        base_url: str,
        headers: dict[str, str],
        timeout: float,
        verify: bool,
    ) -> StubClient:
        self.base_url = base_url
        self.headers = headers
        self.timeout = timeout
        self.verify = verify
        return StubClient()


class TestChoosingLights:
    async def test_naming_nothing_takes_every_light_in_the_house(self) -> None:
        client = StubClient({"data": [a_light("a"), a_light("b", name="Kitchen")]})
        lights = HueLights(host="", application_key="", bridge=a_bridge(client))
        await lights.open()

        assert set(lights.light_ids) == {"a", "b"}

    async def test_a_light_can_be_chosen_by_name(self) -> None:
        """A Hue identifier is a UUID nobody has typed on purpose."""

        client = StubClient(
            {"data": [a_light("a", name="Sitting room"), a_light("b", name="Kitchen")]}
        )
        lights = HueLights(
            host="", application_key="", lights=("kitchen",), bridge=a_bridge(client)
        )
        await lights.open()

        assert lights.light_ids == ("b",)

    async def test_a_light_can_be_chosen_by_identifier(self) -> None:
        client = StubClient({"data": [a_light("a"), a_light("b", name="Kitchen")]})
        lights = HueLights(
            host="", application_key="", lights=("b",), bridge=a_bridge(client)
        )
        await lights.open()

        assert lights.light_ids == ("b",)

    async def test_a_name_that_matches_nothing_is_refused_at_startup(self) -> None:
        client = StubClient({"data": [a_light("a", name="Sitting room")]})
        lights = HueLights(
            host="", application_key="", lights=("Shed",), bridge=a_bridge(client)
        )

        with pytest.raises(LightingUnavailableError, match="Shed"):
            await lights.open()

    async def test_a_bridge_with_no_lights_at_all_is_refused(self) -> None:
        lights = HueLights(
            host="", application_key="", bridge=a_bridge(StubClient({"data": []}))
        )

        with pytest.raises(LightingUnavailableError, match="no lights"):
            await lights.open()


class TestReadingAndWritingLights:
    async def opened(self, client: StubClient, **options: Any) -> HueLights:
        lights = HueLights(
            host="", application_key="", bridge=a_bridge(client), **options
        )
        await lights.open()
        client.requests.clear()
        return lights

    async def test_a_resource_becomes_a_state_the_service_understands(self) -> None:
        client = StubClient({"data": [a_light("a", name="Lamp", brightness=62.5)]})
        lights = await self.opened(client)

        assert await lights.snapshot() == (
            LightState(light_id="a", on=True, brightness=0.625, name="Lamp"),
        )

    async def test_a_light_that_is_off_still_reports_what_it_will_return_to(
        self,
    ) -> None:
        """Which is what a restore has to put back."""

        client = StubClient({"data": [a_light("a", on=False, brightness=40.0)]})
        lights = await self.opened(client)

        state = (await lights.snapshot())[0]
        assert state.on is False
        assert state.brightness == 0.4

    async def test_a_light_with_no_dimming_reports_no_brightness(self) -> None:
        client = StubClient({"data": [a_light("a", brightness=None)]})
        lights = await self.opened(client)

        assert (await lights.snapshot())[0].brightness is None

    async def test_applying_sends_a_brightness_and_a_transition(self) -> None:
        client = StubClient({"data": [a_light("a")]})
        lights = await self.opened(client)
        await lights.apply({"a": 0.5}, transition_seconds=0.2)

        assert client.requests == [
            (
                "PUT",
                "/clip/v2/resource/light/a",
                {"dimming": {"brightness": 50.0}, "dynamics": {"duration": 200}},
            )
        ]

    async def test_applying_never_asserts_a_light_is_on(self) -> None:
        """Arguing with a person about their own lights is what this must never
        do: a modulation command that also asserted `on` would turn a lamp back
        on the moment somebody reached for a switch mid-sentence."""

        client = StubClient({"data": [a_light("a")]})
        lights = await self.opened(client)
        await lights.apply({"a": 0.5}, transition_seconds=0.2)

        assert "on" not in client.requests[0][2]

    async def test_a_light_that_was_not_chosen_is_never_written_to(self) -> None:
        client = StubClient({"data": [a_light("a", name="Lamp")]})
        lights = await self.opened(client, lights=("Lamp",))
        await lights.apply({"a": 0.5, "somebody-elses-lamp": 0.1}, 0.2)

        assert [path for _, path, _ in client.requests] == ["/clip/v2/resource/light/a"]

    async def test_restoring_puts_back_both_brightness_and_whether_it_was_on(
        self,
    ) -> None:
        client = StubClient({"data": [a_light("a")]})
        lights = await self.opened(client)
        await lights.restore((LightState(light_id="a", on=True, brightness=0.8),))

        body = client.requests[0][2]
        assert body["on"] == {"on": True}
        assert body["dimming"] == {"brightness": 80.0}
        assert body["dynamics"]["duration"] > 0, "snapping back looks like a glitch"

    async def test_restoring_a_light_with_no_brightness_sets_only_its_switch(
        self,
    ) -> None:
        client = StubClient({"data": [a_light("a")]})
        lights = await self.opened(client)
        await lights.restore((LightState(light_id="a", on=False, brightness=None),))

        body = client.requests[0][2]
        assert body["on"] == {"on": False}
        assert "dimming" not in body

    async def test_an_absurd_transition_is_clamped_rather_than_sent(self) -> None:
        client = StubClient({"data": [a_light("a")]})
        lights = await self.opened(client)
        await lights.apply({"a": 0.5}, transition_seconds=-5.0)
        await lights.apply({"a": 0.5}, transition_seconds=10_000.0)

        durations = [body["dynamics"]["duration"] for _, _, body in client.requests]
        assert durations == [0, 60_000]

    async def test_closing_releases_the_bridge(self) -> None:
        client = StubClient({"data": [a_light("a")]})
        lights = await self.opened(client)
        await lights.aclose()

        assert client.closed == 1
