"""Choosing a lighting system from configuration, and nothing else.

One switch, in one file, so that adding a second system is a case here and a
module beside `adapters/hue`. The service never learns which one it got.

The vendor import is inside its own branch on purpose: selecting `none`, or
leaving lighting switched off, must not require an HTTP client to be installed.
"""

import logging

from caddiej.lighting.port import LightingPort, NullLights

logger = logging.getLogger(__name__)


def build_lights(
    kind: str,
    bridge_host: str = "",
    application_key: str = "",
    lights: str = "",
    timeout_seconds: float = 3.0,
) -> LightingPort | None:
    """The lighting system named, or None to leave the room alone.

    None is a supported answer rather than a failure: lighting is off by default
    and a house with no bridge in it is a house CaddieJ runs in.
    """

    if kind == "none":
        return NullLights()

    if kind == "hue":
        from caddiej.adapters.hue import HueLights

        return HueLights(
            host=bridge_host,
            application_key=application_key,
            lights=_named(lights),
            timeout_seconds=timeout_seconds,
        )

    logger.error("lighting_output_unknown", extra={"lighting_output": kind})
    return None


def _named(lights: str) -> tuple[str, ...]:
    """The lights somebody listed, or nothing to mean "whichever are there".

    A comma-separated list because it comes from an environment variable, and
    names as well as identifiers because a Hue identifier is a UUID nobody has
    ever typed on purpose.
    """

    return tuple(name.strip() for name in lights.split(",") if name.strip())
