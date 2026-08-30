"""Letting the room react, without ever being in the way.

A voice starts, its measured shape arrives on the event, and the lights follow it
until it stops — then the room is put back exactly as it was found. That last
part is the reason most of this package exists.

Nothing outside imports a bridge, and nothing here knows any golf. A `Flourish`
is a depth and a duration; deciding that a birdie is worth one belongs to
`caddiej.effects`, which decides what the floor and the voice do about the same
birdie in the same table.
"""

from caddiej.lighting.factory import build_lights
from caddiej.lighting.modulation import (
    Curve,
    Flourish,
    Modulation,
    pulse_curve,
    speaking_curve,
)
from caddiej.lighting.port import (
    LightingCommandError,
    LightingError,
    LightingPort,
    LightingUnavailableError,
    LightState,
    NullLights,
)
from caddiej.lighting.service import (
    LightingHealth,
    LightingService,
    build_lighting_service,
)

__all__ = [
    "Curve",
    "Flourish",
    "LightState",
    "LightingCommandError",
    "LightingError",
    "LightingHealth",
    "LightingPort",
    "LightingService",
    "LightingUnavailableError",
    "Modulation",
    "NullLights",
    "build_lighting_service",
    "build_lights",
    "pulse_curve",
    "speaking_curve",
]
