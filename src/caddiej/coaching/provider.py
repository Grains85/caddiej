"""Somewhere to send a request and get JSON back, in nobody's dialect.

The interface is deliberately narrow, because this is where designs like this
usually rot: one vendor's vocabulary — messages, roles, tool calls, response ids,
conversation state — becomes the interface, and then the second provider cannot
be added without touching everything.

So a provider is handed a prompt that has already been written and a schema the
reply must satisfy, and it returns parsed JSON. It does not compose the prompt,
because assembling it is deterministic domain work and a provider that could
rewrite it would be a provider that changed the coaching by being swapped. It
does not validate the reply either, because what makes a coaching line acceptable
is a golf question.

The errors below are the ones the service reacts to differently, and no more.
Which of them a vendor's exception maps to is the adapter's job.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


class ProviderError(RuntimeError):
    """A provider could not answer. Never reaches the player."""


class ProviderUnavailableError(ProviderError):
    """Not reachable, or broken at their end. Worth one more try."""


class ProviderRateLimitedError(ProviderError):
    """Too many requests. Worth one more try, later than usual."""

    def __init__(self, message: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ProviderTimeoutError(ProviderError):
    """No answer inside the budget. Worth one more try only if time remains."""


class ProviderRefusedError(ProviderError):
    """The request itself was rejected: bad credentials, unknown model, refusal.

    Never retried. The same request would be refused the same way, and a retry
    loop against a wrong API key is how a quiet configuration mistake becomes a
    noisy one.
    """


class ProviderReplyError(ProviderError):
    """Something came back, but not the JSON that was asked for."""


@dataclass(frozen=True, slots=True)
class ImageInput:
    """One picture, already encoded and already scaled down.

    `label` is the view the picture is, written beside it in the request so
    the model does not have to infer the camera from the pixels.

    `frame_id` is the name the model may use to point back at this picture,
    such as `swing_side_02`. It is CaddieJ's, derived from where the still sits
    in the capture, and the reply schema enumerates exactly the ids that were
    sent — so "the frame that shows it" is a choice from a list and never a
    filename a model made up. Empty for a picture nothing can point back to.
    """

    data: bytes
    media_type: str = "image/jpeg"
    label: str = ""
    detail: str | None = None
    """How closely the provider should look. None is the provider's default,
    which is coarse; a still the model has to read digits from needs more."""

    frame_id: str = ""


@dataclass(frozen=True, slots=True)
class AdviceRequest:
    """Everything a provider needs, and nothing about who it is going to.

    The prompt arrives rendered. `schema` is JSON Schema, which is a shape
    rather than a vendor concept and is what every serious provider consumes to
    constrain a reply; deriving one per provider from a dataclass would be
    machinery for a problem nobody has.
    """

    system: str
    instructions: str
    evidence: str
    schema: Mapping[str, Any]
    images: tuple[ImageInput, ...] = ()
    max_output_tokens: int = 60
    timeout_seconds: float = 8.0
    schema_name: str = "swing_advice"
    reasoning_effort: str | None = None
    """For reasoning models only. None leaves the provider's default."""


@dataclass(frozen=True, slots=True)
class ProviderReply:
    """Parsed JSON, plus what it cost to get.

    The payload is unvalidated on purpose: a provider that decided what counted
    as acceptable coaching would be making a golf judgement.
    """

    payload: Mapping[str, Any]
    model: str
    latency_seconds: float
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    """The part of `output_tokens` a reasoning model spent thinking. It counts
    against `max_output_tokens` like the visible reply does."""


class AdviceProvider(Protocol):
    """A model that can be asked to phrase one coaching line."""

    @property
    def name(self) -> str:
        """Which provider this is, for logs and for stored rows."""

    @property
    def model(self) -> str: ...

    @property
    def is_configured(self) -> bool:
        """Whether this could work at all: credentials, package, endpoint.

        Asked once at startup, so that a missing key disables analysis with a
        log line rather than failing once per shot for an evening.
        """

    async def complete(self, request: AdviceRequest) -> ProviderReply:
        """Ask, and raise a `ProviderError` if there is no usable answer."""

    async def aclose(self) -> None:
        """Let go of whatever was held open. Called once, at shutdown."""


BuildProvider = Callable[[], Awaitable[AdviceProvider | None]]


def image_inputs(frames: Sequence[bytes]) -> tuple[ImageInput, ...]:
    """Wrap extracted stills, skipping any the extractor could not produce."""

    return tuple(ImageInput(data=frame) for frame in frames if frame)
