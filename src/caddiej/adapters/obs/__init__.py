"""OBS Studio adapter."""

from caddiej.adapters.obs.client import (
    ObsAuthenticationError,
    ObsClient,
    ObsConnectionError,
    ObsIdentificationError,
    ObsRequestError,
    ObsResourceNotFoundError,
    ObsTransportConnectionError,
    ObsVersionInfo,
)

__all__ = [
    "ObsAuthenticationError",
    "ObsClient",
    "ObsConnectionError",
    "ObsIdentificationError",
    "ObsRequestError",
    "ObsResourceNotFoundError",
    "ObsTransportConnectionError",
    "ObsVersionInfo",
]
