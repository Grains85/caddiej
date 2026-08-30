"""Philips Hue, confined to this package.

Nothing outside imports an HTTP client or knows what a bridge is, and nothing
inside CaddieJ depends on this package being installed: the lighting subsystem
talks to a `LightingPort`, and this is one implementation of it.
"""

from caddiej.adapters.hue.bridge import (
    API_ROOT,
    BRIGHTNESS_MAX,
    BRIGHTNESS_MIN,
    KEY_HEADER,
    OUTPUT_NAME,
    HueBridge,
    sdk,
    to_fraction,
    to_percent,
)
from caddiej.adapters.hue.lights import RESOURCE, HueLights

__all__ = [
    "API_ROOT",
    "BRIGHTNESS_MAX",
    "BRIGHTNESS_MIN",
    "KEY_HEADER",
    "OUTPUT_NAME",
    "RESOURCE",
    "HueBridge",
    "HueLights",
    "sdk",
    "to_fraction",
    "to_percent",
]
