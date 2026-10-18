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

**Image detail comes from the request.** When an image does not name one, the
provider default remains low. A swing-reference call sets high, because hands,
shaft and hips are small in the frame. High detail uses more input tokens per
image. It does not change how many images are sent.

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
from caddiej.adapters.openai.pricing import estimate_cost
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
        options: dict[str, Any] = {}
        if request.reasoning_effort:
            options["reasoning"] = {"effort": request.reasoning_effort}
        try:
            response = await self._connect().responses.create(
                model=self._model,
                instructions=_instructions(request),
                input=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": request.instructions},
                            {"type": "input_text", "text": request.evidence},
                            *self._image_parts(request.images),
                        ],
                    }
                ],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": request.schema_name,
                        "schema": dict(request.schema),
                        "strict": request.strict_schema,
                    }
                },
                max_output_tokens=request.max_output_tokens,
                timeout=request.timeout_seconds,
                **options,
            )
        except Exception as error:
            raise classify(module, error) from error

        elapsed = time.monotonic() - started
        _log_usage(response, request, elapsed, self._model)
        return ProviderReply(
            payload=_payload(response),
            model=getattr(response, "model", self._model) or self._model,
            latency_seconds=elapsed,
            input_tokens=_usage(response, "input_tokens"),
            output_tokens=_usage(response, "output_tokens"),
            reasoning_tokens=_reasoning_tokens(response),
            cached_tokens=_cached_tokens(response),
        )

    def _image_parts(self, images: tuple[ImageInput, ...]) -> list[dict[str, str]]:
        parts: list[dict[str, str]] = []
        for image in images:
            announced = _announce(image)
            if announced:
                parts.append({"type": "input_text", "text": announced})
            parts.append(self._image(image))
        return parts

    def _image(self, image: ImageInput) -> dict[str, str]:
        encoded = base64.b64encode(image.data).decode("ascii")
        return {
            "type": "input_image",
            "image_url": f"data:{image.media_type};base64,{encoded}",
            "detail": image.detail or self._detail,
        }

    async def aclose(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            await client.close()


def _instructions(request: AdviceRequest) -> str:
    """System text, with the stable reference prefix first when one was given.

    The prefix does not include the shot. Providers that cache an identical
    prompt prefix, including OpenAI's automatic prompt cache for long prompts,
    can reuse it. This SDK build has no prompt-cache parameter, so the reuse
    is the prefix order itself.
    """

    if not request.prefix:
        return request.system
    return f"{request.prefix}\n\n{request.system}"


def _announce(image: ImageInput) -> str:
    """The text placed before an image: its id first, so it can be cited.

    The id is what the reply schema enumerates and the label is what the
    picture shows. Putting the two side by side is what lets a model say
    "swing_side_02" because it is the face-on frame at impact, rather than
    because it was the second picture it happened to look at.
    """

    if image.frame_id and image.label:
        return f"Next image: {image.frame_id} ({image.label})"
    if image.frame_id or image.label:
        return f"Next image: {image.frame_id or image.label}"
    return ""


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


def _reasoning_tokens(response: Any) -> int | None:
    details = getattr(getattr(response, "usage", None), "output_tokens_details", None)
    value = getattr(details, "reasoning_tokens", None)
    return int(value) if isinstance(value, int) else None


def _cached_tokens(response: Any) -> int | None:
    details = getattr(getattr(response, "usage", None), "input_tokens_details", None)
    value = getattr(details, "cached_tokens", None)
    return int(value) if isinstance(value, int) else None


def _log_usage(
    response: Any, request: AdviceRequest, elapsed: float, model: str
) -> None:
    """What the reply cost, complete or not. Counts only, never content.

    Logged before the reply is judged, so an incomplete one still says whether
    the ceiling went on reasoning or on the visible JSON. The dollar figure is
    an estimate from `pricing.py` and may be None; see there for why it is
    never more than a log field.
    """

    status = getattr(response, "status", None)
    reason = getattr(getattr(response, "incomplete_details", None), "reason", None)
    incomplete = status == "incomplete"
    input_tokens = _usage(response, "input_tokens")
    output_tokens = _usage(response, "output_tokens")
    answered = getattr(response, "model", None)
    priced = answered if isinstance(answered, str) and answered else model
    (logger.warning if incomplete else logger.info)(
        "coaching_openai_usage",
        extra={
            "coaching_response_status": str(status or "unknown"),
            "coaching_reason": str(reason) if incomplete else "",
            "coaching_usage_input": input_tokens,
            "coaching_usage_output": output_tokens,
            "coaching_usage_reasoning": _reasoning_tokens(response),
            "coaching_usage_total": _usage(response, "total_tokens"),
            "coaching_output_ceiling": request.max_output_tokens,
            "coaching_reasoning_effort": request.reasoning_effort or "default",
            "coaching_latency_ms": round(elapsed * 1000),
            "coaching_cost_usd": _cost(priced, input_tokens, output_tokens),
        },
    )


def _cost(
    model: str, input_tokens: int | None, output_tokens: int | None
) -> float | None:
    """Best effort. A pricing failure must never become a coaching failure."""

    try:
        cost = estimate_cost(model, input_tokens, output_tokens)
    except Exception:
        return None
    return None if cost is None else round(cost, 6)
