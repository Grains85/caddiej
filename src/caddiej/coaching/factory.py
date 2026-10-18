"""Choosing a provider from configuration, and nothing else.

One switch, in one file, so that adding a provider is a case here and a module
beside `adapters/openai`. The service never learns which one it got.

The import of a vendor adapter is inside its branch on purpose: selecting
`scripted`, or `none`, must not require the OpenAI SDK to be installed.
"""

import logging

from caddiej.coaching.provider import AdviceProvider

logger = logging.getLogger(__name__)


def build_provider(
    kind: str,
    model: str,
    api_key: str = "",
    api_base: str = "",
) -> AdviceProvider | None:
    """The provider named, or None when no model should be called.

    None is a supported answer rather than a failure: it is the default, and it
    is what CaddieJ runs as with no credentials anywhere on the machine.
    """

    if kind == "none":
        return None

    if kind == "openai":
        from caddiej.adapters.openai import OpenAIAdviceProvider

        return OpenAIAdviceProvider(
            api_key=api_key,
            model=model,
            base_url=api_base or None,
        )

    if kind == "scripted":
        from caddiej.coaching.fakes import DemoProvider

        logger.warning("coaching_scripted_provider")
        return DemoProvider()

    logger.error("coaching_provider_unknown", extra={"coaching_provider": kind})
    return None
