"""A set of Hue lamps, behind the port the lighting service talks to.

This turns Hue's answers into CaddieJ's vocabulary and back: identifiers, an
on-off flag and a brightness fraction, out of a resource document with a dozen
fields nothing here reads.

**Lights are chosen by name, not only by identifier.** A Hue light's id is a UUID
nobody has typed on purpose, so `LIGHTING_LIGHTS=Sitting room lamp` has to work.
Matching is case-insensitive and accepts either form, and an unmatched name is
reported by name — because "which of the four things I listed did you not find"
is the only question somebody has when this goes wrong.

**Naming nothing means every light in the house.** Convenient, and a little
dangerous, so it is the sort of thing worth doing loudly: the count is logged at
start-up, so somebody who did not mean the whole house finds out immediately
rather than the first time a coaching line dims the kitchen.
"""

import logging
from collections.abc import Mapping
from typing import Any

from caddiej.adapters.hue.bridge import (
    OUTPUT_NAME,
    HueBridge,
    to_fraction,
    to_percent,
)
from caddiej.lighting.port import LightingUnavailableError, LightState

logger = logging.getLogger(__name__)

RESOURCE = "light"


class HueLights:
    """The lamps CaddieJ is allowed to touch, and nothing else in the house."""

    def __init__(
        self,
        host: str,
        application_key: str,
        lights: tuple[str, ...] = (),
        timeout_seconds: float = 3.0,
        bridge: HueBridge | None = None,
    ) -> None:
        self._bridge = bridge or HueBridge(
            host=host,
            application_key=application_key,
            timeout_seconds=timeout_seconds,
        )
        self._wanted = lights
        self._ids: tuple[str, ...] = ()
        self._names: dict[str, str] = {}

    @property
    def name(self) -> str:
        return OUTPUT_NAME

    @property
    def is_configured(self) -> bool:
        return self._bridge.is_configured

    @property
    def light_ids(self) -> tuple[str, ...]:
        return self._ids

    async def open(self) -> None:
        """Find the lamps, and refuse to start if none of them are there.

        Resolved once. A bridge that has to be asked which lights exist before
        every command would spend half its budget on questions, and lamps do not
        appear and disappear during a round.
        """

        await self._bridge.open()
        resources = await self._bridge.get(RESOURCE)
        if not resources:
            raise LightingUnavailableError("the bridge reports no lights at all")

        chosen = self._choose(resources)
        if not chosen:
            raise LightingUnavailableError(
                f"none of the lights named were found: {', '.join(self._wanted)}"
            )

        self._ids = tuple(chosen)
        self._names = chosen
        logger.info(
            "lighting_bridge_ready",
            extra={
                "lighting_output": OUTPUT_NAME,
                "lighting_bridge": self._bridge.host,
                "lighting_lights": len(self._ids),
                "lighting_named": len(self._wanted),
            },
        )

    def _choose(self, resources: list[dict[str, Any]]) -> dict[str, str]:
        """Which of the bridge's lights were asked for, by id or by name."""

        available = {
            str(item.get("id")): _name_of(item) for item in resources if item.get("id")
        }
        if not self._wanted:
            return available

        wanted = {name.casefold() for name in self._wanted}
        chosen = {
            light_id: name
            for light_id, name in available.items()
            if light_id.casefold() in wanted or name.casefold() in wanted
        }
        missing = wanted - {
            key
            for light_id, name in chosen.items()
            for key in (light_id.casefold(), name.casefold())
        }
        if missing:
            logger.warning(
                "lighting_lights_not_found",
                extra={"lighting_missing": ", ".join(sorted(missing))},
            )
        return chosen

    async def snapshot(self) -> tuple[LightState, ...]:
        """What the chosen lamps are doing right now."""

        resources = await self._bridge.get(RESOURCE)
        wanted = set(self._ids)
        return tuple(
            _state_of(item, self._names.get(str(item.get("id")), ""))
            for item in resources
            if str(item.get("id")) in wanted
        )

    async def apply(
        self, brightness: Mapping[str, float], transition_seconds: float
    ) -> None:
        """Set these lamps to these brightnesses, over that long.

        Brightness only. Not `on`, deliberately: a modulation command that also
        asserted a lamp was on would turn one back on the moment somebody reached
        for a switch mid-sentence, and arguing with a person about their own
        lights is exactly what this must never do.
        """

        await self._bridge.put_all(
            RESOURCE,
            {
                light_id: {
                    "dimming": {"brightness": to_percent(value)},
                    "dynamics": {"duration": _milliseconds(transition_seconds)},
                }
                for light_id, value in brightness.items()
                if light_id in self._names
            },
        )

    async def restore(self, state: tuple[LightState, ...]) -> None:
        """Put the lamps back exactly as they were found.

        `on` is included here where `apply` omits it, because this is the call
        that has to undo everything — including the case where CaddieJ dimmed a
        lamp so far that the bridge switched it out.

        The transition is short but not instant. Snapping back is what makes a
        subtle effect look like a glitch at the end.
        """

        bodies: dict[str, dict[str, Any]] = {}
        for light in state:
            if light.light_id not in self._names:
                continue
            body: dict[str, Any] = {
                "on": {"on": light.on},
                "dynamics": {"duration": _milliseconds(0.4)},
            }
            if light.brightness is not None:
                body["dimming"] = {"brightness": to_percent(light.brightness)}
            bodies[light.light_id] = body
        await self._bridge.put_all(RESOURCE, bodies)

    async def aclose(self) -> None:
        await self._bridge.aclose()


def _name_of(resource: dict[str, Any]) -> str:
    metadata = resource.get("metadata")
    if isinstance(metadata, dict):
        return str(metadata.get("name", ""))
    return ""


def _state_of(resource: dict[str, Any], name: str) -> LightState:
    """One light resource as the service's idea of a light.

    A lamp that is off reports the brightness it will return to, which is
    genuinely useful — it is what restore has to put back — so it is kept rather
    than discarded. Whether the service is allowed to dim it is a separate
    question, answered by `on`.
    """

    on = resource.get("on")
    dimming = resource.get("dimming")
    brightness = None
    if isinstance(dimming, dict):
        value = dimming.get("brightness")
        if isinstance(value, int | float):
            brightness = to_fraction(float(value))
    return LightState(
        light_id=str(resource.get("id", "")),
        on=bool(on.get("on")) if isinstance(on, dict) else False,
        brightness=brightness,
        name=name or _name_of(resource),
    )


def _milliseconds(seconds: float) -> int:
    """A transition as Hue wants it. Never negative, never absurd."""

    return max(0, min(60_000, round(seconds * 1000)))
