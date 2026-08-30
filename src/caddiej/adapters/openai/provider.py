"""The only file in CaddieJ that knows what the OpenAI SDK looks like.

Everything vendor-shaped is here: the Responses API call, the structured-output
parameter, how an image is attached, where the token counts live, which
exception means what. Swapping providers, or adding a second, is a new file
beside this one and a line in the factory.

Three decisions worth stating.

**The SDK's own retries are switched off.** It would retry inside a single
`complete`, which the service cannot see, cannot count against its budget, and
cannot abandon when a newer shot arrives. Retrying is the service's business
because only the service knows whether the answer is still wanted.

**Images go as data URLs at low detail.** Low detail is a fixed, small number of
tokens per image, and what a coach reads from a swing frame — where the hands
are, whether the hips have moved — survives it. High detail would multiply the
cost of every shot for resolution nobody is using.

**The reply is parsed and handed on unvalidated.** Whether a coaching line is
acceptable is a golf question, answered in `coaching/validate.py`. A provider
that could reject one would be a provider that changed the coaching.
"""

import base64
import json
import logging
import time
from typing import Any

from caddiej.adapters.openai.errors import classify
from caddiej.coaching.provider import (
    AdviceRequest,
    ImageInput,
    ProviderReply,
    ProviderReplyError,
)

logger = logging.getLogger(__name__)

PROVIDER_NAME = "openai"
# Fixed and small per image. See the module docstring.
IMAGE_DETAIL = "low"


def sdk() -> Any | None:
    """The SDK module, or None if it was never installed.

    Optional on purpose: CaddieJ runs, coaches from templates and films swings
    with no AI dependency present at all, so its absence is a supported state
    rather than an import error at start-up.
    """

    try:
        import openai
    except ModuleNotFoundError:
        return None
    return openai


class OpenAIAdviceProvider:
    """Asks an OpenAI model to phrase one coaching line."""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str | None = None,
        detail: str = IMAGE_DETAIL,
        module: Any | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url or None
        self._detail = detail
        self._module = module if module is not None else sdk()
        self._client: Any | None = None

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @property
    def model(self) -> str:
        return self._model

    @property
    def is_configured(self) -> bool:
        """Whether a call could succeed: a key, and a package to make it with."""

        if self._module is None:
            logger.warning("coaching_openai_sdk_missing")
            return False
        return bool(self._api_key)

    def _connect(self) -> Any:
        """The client, built once and kept.

        Lazily, so that constructing the provider costs nothing and a run that
        never coaches never opens a connection pool.
        """

        if self._client is None:
            assert self._module is not None
            self._client = self._module.AsyncOpenAI(
                api_key=self._api_key,
                base_url=self._base_url,
                # See the module docstring: retrying is the service's job.
                max_retries=0,
            )
        return self._client

    async def complete(self, request: AdviceRequest) -> ProviderReply:
        """Ask, and raise a neutral `ProviderError` if there is no answer."""

        module = self._module
        assert module is not None, "complete called on an unconfigured provider"

        started = time.monotonic()
        try:
            response = await self._connect().responses.create(
                model=self._model,
                instructions=request.system,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": request.instructions},
                            {"type": "input_text", "text": request.evidence},
                            *(self._image(image) for image in request.images),
                        ],
                    }
                ],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "swing_advice",
                        "schema": dict(request.schema),
                        "strict": True,
                    }
                },
                max_output_tokens=request.max_output_tokens,
                timeout=request.timeout_seconds,
            )
        except Exception as error:
            raise classify(module, error) from error

        elapsed = time.monotonic() - started
        return ProviderReply(
            payload=_payload(response),
            model=getattr(response, "model", self._model) or self._model,
            latency_seconds=elapsed,
            input_tokens=_usage(response, "input_tokens"),
            output_tokens=_usage(response, "output_tokens"),
        )

    def _image(self, image: ImageInput) -> dict[str, str]:
        encoded = base64.b64encode(image.data).decode("ascii")
        return {
            "type": "input_image",
            "image_url": f"data:{image.media_type};base64,{encoded}",
            "detail": self._detail,
        }

    async def aclose(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            await client.close()


def _payload(response: Any) -> dict[str, Any]:
    """The JSON object the model produced, or a reply error.

    An incomplete response is called out separately because it has one likely
    cause — the output token ceiling — and reporting it as unparseable JSON would
    send whoever reads the log looking in the wrong place.
    """

    if getattr(response, "status", None) == "incomplete":
        reason = getattr(getattr(response, "incomplete_details", None), "reason", None)
        raise ProviderReplyError(f"the reply was incomplete: {reason}")

    text = getattr(response, "output_text", "") or ""
    if not text.strip():
        raise ProviderReplyError("the reply held no text")

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise ProviderReplyError(f"the reply was not JSON: {error}") from error
    if not isinstance(payload, dict):
        raise ProviderReplyError(
            f"the reply was {type(payload).__name__}, not an object"
        )
    return payload


def _usage(response: Any, field: str) -> int | None:
    usage = getattr(response, "usage", None)
    value = getattr(usage, field, None)
    return int(value) if isinstance(value, int) else None
