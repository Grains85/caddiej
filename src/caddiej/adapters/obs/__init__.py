"""OBS Studio adapter."""

from caddiej.adapters.obs.client import (
    CAPTURE_SCENE,
    SCREENSHOT_FORMAT,
    CaptureSourceState,
    ObsAuthenticationError,
    ObsClient,
    ObsConnectionError,
    ObsIdentificationError,
    ObsRequestError,
    ObsResourceNotFoundError,
    ObsTransportConnectionError,
    ObsVersionInfo,
)
from caddiej.adapters.obs.floor_reveals import FLOOR_REVEALS, FloorReveals

__all__ = [
    "CAPTURE_SCENE",
    "FLOOR_REVEALS",
    "SCREENSHOT_FORMAT",
    "CaptureSourceState",
    "FloorReveals",
    "ObsAuthenticationError",
    "ObsClient",
    "ObsConnectionError",
    "ObsIdentificationError",
    "ObsRequestError",
    "ObsResourceNotFoundError",
    "ObsTransportConnectionError",
    "ObsVersionInfo",
]
