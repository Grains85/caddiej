"""The model-provider seam used by Ideal Swing coaching.

Golf advice lives in the swing reference and in ``config/ideal_swing.json``.
This package only chooses a provider and describes the request it accepts.
"""

from caddiej.coaching.provider import (
    AdviceProvider,
    AdviceRequest,
    ImageInput,
    ProviderError,
    ProviderRateLimitedError,
    ProviderRefusedError,
    ProviderReply,
    ProviderReplyError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

__all__ = [
    "AdviceProvider",
    "AdviceRequest",
    "ImageInput",
    "ProviderError",
    "ProviderRateLimitedError",
    "ProviderRefusedError",
    "ProviderReply",
    "ProviderReplyError",
    "ProviderTimeoutError",
    "ProviderUnavailableError",
]
