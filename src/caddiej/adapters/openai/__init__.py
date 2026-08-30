"""OpenAI, behind the coaching subsystem's provider port.

An adapter in the strict sense: it holds a vendor SDK, translates a
vendor-shaped call and vendor exceptions, and knows nothing about golf. Nothing
outside this package imports `openai`, and the coaching subsystem does not know
which provider it is talking to.

The dependency is optional. Absent, `is_configured` answers no, coaching runs
from its templates, and CaddieJ starts normally.
"""

from caddiej.adapters.openai.errors import classify, retry_after_seconds
from caddiej.adapters.openai.provider import (
    IMAGE_DETAIL,
    PROVIDER_NAME,
    OpenAIAdviceProvider,
    sdk,
)

__all__ = [
    "IMAGE_DETAIL",
    "PROVIDER_NAME",
    "OpenAIAdviceProvider",
    "classify",
    "retry_after_seconds",
    "sdk",
]
