"""A provider that answers from a script instead of from a network.

Everything the real subsystem has to survive can be arranged here: a good reply,
a reply that breaks each validation rule, a silence that outlasts the timeout,
a rate limit, an outage, a refusal. None of it costs anything, none of it needs a
key, and the whole thing runs in a test in milliseconds.

It also gives CaddieJ a demo mode. Set the provider to `scripted` and a simulated
round produces coaching lines end to end, on a machine with no credentials and no
internet, which is how the floor gets reviewed without buying anything.
"""

import asyncio
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from caddiej.coaching.provider import (
    AdviceRequest,
    ProviderError,
    ProviderReply,
    ProviderTimeoutError,
)

# A reply that passes every rule: two words upper case, an affirmation and one
# instruction, no numbers, no directions.
GOOD_REPLY: Mapping[str, Any] = {
    "focus": "face_control",
    "display": "FACE OPEN",
    "speech": "Good path. Square the face a little earlier.",
    "severity": 0.42,
}


@dataclass
class ScriptedProvider:
    """Answers with the next thing on the script, then repeats the last one.

    Repeating rather than running out, because a test about the third shot should
    not have to say anything about the first two, and a demo round should not
    stop coaching once the script is spent.
    """

    replies: Sequence[Mapping[str, Any] | BaseException] = (GOOD_REPLY,)
    model_name: str = "scripted-1"
    configured: bool = True
    delay: float = 0.0
    """How long to take. Longer than the caller's timeout produces a timeout,
    which is the honest way to test one."""

    input_tokens: int | None = 900
    output_tokens: int | None = 24
    requests: list[AdviceRequest] = field(default_factory=list)
    _queue: deque[Mapping[str, Any] | BaseException] = field(
        default_factory=deque, init=False
    )

    def __post_init__(self) -> None:
        if not self.replies:
            raise ValueError("a scripted provider needs at least one reply")
        self._queue = deque(self.replies)

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def model(self) -> str:
        return self.model_name

    @property
    def is_configured(self) -> bool:
        return self.configured

    @property
    def calls(self) -> int:
        return len(self.requests)

    async def complete(self, request: AdviceRequest) -> ProviderReply:
        self.requests.append(request)

        if self.delay:
            # The timeout belongs to the provider in the real adapter too, so it
            # is enforced here rather than by the caller.
            try:
                await asyncio.wait_for(
                    asyncio.sleep(self.delay), request.timeout_seconds
                )
            except TimeoutError:
                raise ProviderTimeoutError(
                    f"no answer within {request.timeout_seconds}s"
                ) from None

        reply = self._queue[0] if len(self._queue) == 1 else self._queue.popleft()
        if isinstance(reply, BaseException):
            raise reply
        return ProviderReply(
            payload=reply,
            model=self.model_name,
            latency_seconds=self.delay,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
        )

    async def aclose(self) -> None:
        return None


# Passes validation: two sentences, one instruction, no numbers, no directions.
DEMO_SPEECH = "Good contact. Feel that same move on the next one."


@dataclass
class DemoProvider:
    """Answers legally for whatever shot it is given, by reading the schema.

    The request already carries the focuses and displays CaddieJ will accept, so
    a fake that picks the first of each produces a valid reply for every shot
    rather than for one. That is what makes `COACHING_PROVIDER=scripted` a demo
    mode worth having: a simulated round shows real coaching lines moving across
    the floor, on a machine with no credentials and no network.
    """

    model_name: str = "demo-1"
    configured: bool = True
    delay: float = 0.4
    """A pause, so the floor is watched doing what it really does: templated line
    first, coached wording a moment later."""

    speech: str = DEMO_SPEECH
    requests: list[AdviceRequest] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "demo"

    @property
    def model(self) -> str:
        return self.model_name

    @property
    def is_configured(self) -> bool:
        return self.configured

    async def complete(self, request: AdviceRequest) -> ProviderReply:
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)

        properties = request.schema.get("properties", {})
        focuses = properties.get("focus", {}).get("enum", ["none"])
        displays = properties.get("display", {}).get("enum", [""])
        return ProviderReply(
            payload={
                "focus": focuses[0],
                "display": displays[0],
                "speech": self.speech,
                "severity": 0.5,
            },
            model=self.model_name,
            latency_seconds=self.delay,
            input_tokens=len(request.evidence) // 4,
            output_tokens=len(self.speech) // 4,
        )

    async def aclose(self) -> None:
        return None


@dataclass
class SilentProvider:
    """Never answers. For testing that a hung provider costs nothing but time."""

    model_name: str = "silent-1"
    configured: bool = True
    calls: int = 0

    @property
    def name(self) -> str:
        return "silent"

    @property
    def model(self) -> str:
        return self.model_name

    @property
    def is_configured(self) -> bool:
        return self.configured

    async def complete(self, request: AdviceRequest) -> ProviderReply:
        self.calls += 1
        await asyncio.sleep(request.timeout_seconds)
        raise ProviderTimeoutError(f"no answer within {request.timeout_seconds}s")

    async def aclose(self) -> None:
        return None


@dataclass
class BrokenProvider:
    """Raises the same thing every time, for the paths that must not retry."""

    error: ProviderError
    model_name: str = "broken-1"
    configured: bool = True
    calls: int = 0

    @property
    def name(self) -> str:
        return "broken"

    @property
    def model(self) -> str:
        return self.model_name

    @property
    def is_configured(self) -> bool:
        return self.configured

    async def complete(self, request: AdviceRequest) -> ProviderReply:
        self.calls += 1
        raise self.error

    async def aclose(self) -> None:
        return None


@dataclass
class StillFrames:
    """A frame source that hands back bytes without a camera being involved."""

    frame: bytes = b"\xff\xd8\xff\xd9"
    count: int | None = None
    error: BaseException | None = None
    requests: list[tuple[str, tuple[float, ...]]] = field(default_factory=list)

    async def frames(self, clip: Any, offsets: Sequence[float]) -> tuple[bytes, ...]:
        self.requests.append((str(clip.path), tuple(offsets)))
        if self.error is not None:
            raise self.error
        wanted = len(offsets) if self.count is None else self.count
        return tuple(self.frame for _ in range(wanted))
