"""The OpenAI adapter, against a stand-in for the SDK.

Nothing here reaches a network or spends anything: the SDK module is a stub with
the same exception hierarchy and a client that returns whatever the test says.
That is also the point of the seam — the adapter takes its module as an argument
precisely so this file can exist.

Two things are being checked. That a request comes out shaped the way the
Responses API wants, including the structured-output parameter and the switched-
off retries. And that every way the call can fail arrives in the coaching layer
as one of four neutral errors, because those four are the only distinctions the
service acts on.
"""

import json
from types import SimpleNamespace
from typing import Any

import pytest

from caddiej.adapters.openai import provider as provider_module
from caddiej.adapters.openai.errors import classify, retry_after_seconds
from caddiej.adapters.openai.provider import OpenAIAdviceProvider
from caddiej.coaching.provider import (
    AdviceRequest,
    ImageInput,
    ProviderRateLimitedError,
    ProviderRefusedError,
    ProviderReplyError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

GOOD = {
    "focus": "face_control",
    "display": "FACE OPEN",
    "speech": "Square the face a little earlier. Keep that same feeling.",
    "severity": 0.5,
}


# --- a stand-in for the SDK ------------------------------------------------


class APIError(Exception):
    pass


class APIStatusError(APIError):
    def __init__(
        self, message: str, status_code: int = 400, headers: Any = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response = SimpleNamespace(headers=headers or {})


class AuthenticationError(APIStatusError):
    pass


class PermissionDeniedError(APIStatusError):
    pass


class NotFoundError(APIStatusError):
    pass


class BadRequestError(APIStatusError):
    pass


class UnprocessableEntityError(APIStatusError):
    pass


class RateLimitError(APIStatusError):
    pass


class APIConnectionError(APIError):
    pass


class APITimeoutError(APIConnectionError):
    pass


class FakeResponses:
    def __init__(self, owner: "FakeClient") -> None:
        self._owner = owner

    async def create(self, **kwargs: Any) -> Any:
        self._owner.calls.append(kwargs)
        if self._owner.error is not None:
            raise self._owner.error
        return self._owner.response


class FakeClient:
    def __init__(self, **kwargs: Any) -> None:
        self.options = kwargs
        self.calls: list[dict[str, Any]] = []
        self.error: Exception | None = None
        self.response: Any = _response(json.dumps(GOOD))
        self.closed = False
        self.responses = FakeResponses(self)

    async def close(self) -> None:
        self.closed = True


def _response(
    text: str,
    status: str = "completed",
    model: str = "gpt-test",
    input_tokens: int | None = 910,
    output_tokens: int | None = 22,
    incomplete_reason: str | None = None,
) -> Any:
    return SimpleNamespace(
        output_text=text,
        status=status,
        model=model,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
        incomplete_details=SimpleNamespace(reason=incomplete_reason),
    )


class FakeSdk(SimpleNamespace):
    """The handful of names the adapter and the classifier touch."""

    def __init__(self) -> None:
        super().__init__(
            APIError=APIError,
            APIStatusError=APIStatusError,
            APIConnectionError=APIConnectionError,
            APITimeoutError=APITimeoutError,
            AuthenticationError=AuthenticationError,
            PermissionDeniedError=PermissionDeniedError,
            NotFoundError=NotFoundError,
            BadRequestError=BadRequestError,
            UnprocessableEntityError=UnprocessableEntityError,
            RateLimitError=RateLimitError,
        )
        self.clients: list[FakeClient] = []

    def AsyncOpenAI(self, **kwargs: Any) -> FakeClient:  # noqa: N802 - the SDK's name
        client = FakeClient(**kwargs)
        self.clients.append(client)
        return client


def a_request(images: tuple[ImageInput, ...] = ()) -> AdviceRequest:
    return AdviceRequest(
        system="You are a golf coach.",
        instructions="Choose one fault.",
        evidence="face open: 6.5 deg",
        schema={"type": "object", "properties": {}},
        images=images,
        max_output_tokens=60,
        timeout_seconds=8.0,
    )


def a_provider(module: FakeSdk, **options: Any) -> OpenAIAdviceProvider:
    return OpenAIAdviceProvider(
        api_key="sk-test", model="gpt-test", module=module, **options
    )


# --- being configured ------------------------------------------------------


def test_a_provider_with_no_package_is_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The supported state of a CaddieJ installed without the AI extra. Not an
    # error, just no model. Simulated by the lookup finding nothing, which is
    # what actually happens, rather than by passing None — that means "find the
    # SDK yourself" and would quietly pass on a machine that has it.
    monkeypatch.setattr(provider_module, "sdk", lambda: None)

    subject = OpenAIAdviceProvider(api_key="sk-test", model="gpt-test")

    assert not subject.is_configured


def test_the_package_is_only_looked_for_when_it_is_not_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(provider_module, "sdk", lambda: None)

    assert a_provider(FakeSdk()).is_configured


def test_a_provider_with_no_key_is_not_configured() -> None:
    provider = OpenAIAdviceProvider(api_key="", model="gpt-test", module=FakeSdk())

    assert not provider.is_configured


def test_a_provider_with_both_is_configured() -> None:
    assert a_provider(FakeSdk()).is_configured


def test_constructing_a_provider_opens_nothing() -> None:
    # A run that never coaches should never build a connection pool.
    module = FakeSdk()

    a_provider(module)

    assert not module.clients


# --- the request -----------------------------------------------------------


async def test_the_request_carries_the_schema_and_the_evidence() -> None:
    module = FakeSdk()
    provider = a_provider(module)
    request = a_request()

    await provider.complete(request)

    sent = module.clients[0].calls[0]
    assert sent["model"] == "gpt-test"
    assert sent["instructions"] == request.system
    assert sent["max_output_tokens"] == 60
    assert sent["timeout"] == 8.0
    fmt = sent["text"]["format"]
    assert fmt["type"] == "json_schema"
    # Strict, so the model is constrained rather than merely asked.
    assert fmt["strict"] is True
    assert fmt["schema"] == dict(request.schema)
    texts = [
        part["text"]
        for part in sent["input"][0]["content"]
        if part["type"] == "input_text"
    ]
    assert request.evidence in texts


async def test_the_sdk_does_not_retry_behind_the_service_s_back() -> None:
    # A retry inside one call cannot be counted against a budget, cannot be
    # abandoned when the next shot arrives, and would be invisible in health.
    module = FakeSdk()

    await a_provider(module).complete(a_request())

    assert module.clients[0].options["max_retries"] == 0


async def test_the_client_is_built_once_and_kept() -> None:
    module = FakeSdk()
    provider = a_provider(module)

    await provider.complete(a_request())
    await provider.complete(a_request())

    assert len(module.clients) == 1


async def test_a_base_url_is_passed_through() -> None:
    # What makes a local or proxied model usable without touching this file.
    module = FakeSdk()

    await a_provider(module, base_url="http://localhost:11434/v1").complete(a_request())

    assert module.clients[0].options["base_url"] == "http://localhost:11434/v1"


async def test_frames_go_as_data_urls_at_low_detail() -> None:
    # Low detail is a fixed small number of tokens per image, and it is enough
    # to see where the hands and hips are.
    module = FakeSdk()
    images = (ImageInput(data=b"\xff\xd8\xff\xd9", media_type="image/jpeg"),)

    await a_provider(module).complete(a_request(images=images))

    parts = module.clients[0].calls[0]["input"][0]["content"]
    attached = [part for part in parts if part["type"] == "input_image"]
    assert len(attached) == 1
    assert attached[0]["detail"] == "low"
    assert attached[0]["image_url"].startswith("data:image/jpeg;base64,")


async def test_each_image_is_named_before_it_is_attached() -> None:
    module = FakeSdk()
    images = (
        ImageInput(data=b"behind", label="SWING_BEHIND / DOWN_THE_LINE"),
        ImageInput(data=b"side", label="SWING_SIDE / FACE_ON"),
        ImageInput(data=b"impact", label="IMPACT"),
        ImageInput(data=b"delivery", label="DELIVERY"),
    )

    await a_provider(module).complete(a_request(images=images))

    parts = module.clients[0].calls[0]["input"][0]["content"]
    texts = [part["text"] for part in parts if part["type"] == "input_text"]
    assert "Next image: SWING_BEHIND / DOWN_THE_LINE" in texts
    assert "Next image: SWING_SIDE / FACE_ON" in texts
    assert "Next image: IMPACT" in texts
    assert "Next image: DELIVERY" in texts
    behind = texts.index("Next image: SWING_BEHIND / DOWN_THE_LINE")
    side = texts.index("Next image: SWING_SIDE / FACE_ON")
    assert behind < side


async def test_a_shot_with_no_frames_sends_no_images() -> None:
    module = FakeSdk()

    await a_provider(module).complete(a_request())

    parts = module.clients[0].calls[0]["input"][0]["content"]
    assert not [part for part in parts if part["type"] == "input_image"]


# --- the reply -------------------------------------------------------------


async def test_a_good_reply_comes_back_parsed_but_unjudged() -> None:
    # Whether a coaching line is acceptable is a golf question, and it is not
    # answered here. This layer only turns bytes into a dictionary.
    module = FakeSdk()

    reply = await a_provider(module).complete(a_request())

    assert reply.payload == GOOD
    assert reply.model == "gpt-test"
    assert reply.input_tokens == 910
    assert reply.output_tokens == 22
    assert reply.latency_seconds >= 0


async def test_a_reply_that_is_not_json_is_a_reply_error() -> None:
    module = FakeSdk()
    provider = a_provider(module)
    await provider.complete(a_request())
    module.clients[0].response = _response("I think you should turn more.")

    with pytest.raises(ProviderReplyError, match="not JSON"):
        await provider.complete(a_request())


async def test_a_reply_that_is_json_but_not_an_object_is_a_reply_error() -> None:
    module = FakeSdk()
    provider = a_provider(module)
    await provider.complete(a_request())
    module.clients[0].response = _response("[1, 2, 3]")

    with pytest.raises(ProviderReplyError, match="not an object"):
        await provider.complete(a_request())


async def test_an_empty_reply_is_a_reply_error() -> None:
    module = FakeSdk()
    provider = a_provider(module)
    await provider.complete(a_request())
    module.clients[0].response = _response("   ")

    with pytest.raises(ProviderReplyError, match="no text"):
        await provider.complete(a_request())


async def test_a_reply_cut_short_says_so_rather_than_looking_like_bad_json() -> None:
    # It has one likely cause — the output ceiling — and calling it unparseable
    # sends whoever reads the log looking in the wrong place.
    module = FakeSdk()
    provider = a_provider(module)
    await provider.complete(a_request())
    module.clients[0].response = _response(
        '{"focus":', status="incomplete", incomplete_reason="max_output_tokens"
    )

    with pytest.raises(ProviderReplyError, match="incomplete"):
        await provider.complete(a_request())


async def test_a_reply_with_no_usage_is_still_a_reply() -> None:
    module = FakeSdk()
    provider = a_provider(module)
    await provider.complete(a_request())
    module.clients[0].response = _response(
        json.dumps(GOOD), input_tokens=None, output_tokens=None
    )

    reply = await provider.complete(a_request())

    assert reply.payload == GOOD
    assert reply.input_tokens is None


# --- failures, as the four the service acts on -----------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (APITimeoutError("timed out"), ProviderTimeoutError),
        (APIConnectionError("connection refused"), ProviderUnavailableError),
        (RateLimitError("slow down", 429), ProviderRateLimitedError),
        (AuthenticationError("bad key", 401), ProviderRefusedError),
        (PermissionDeniedError("no access", 403), ProviderRefusedError),
        (NotFoundError("no such model", 404), ProviderRefusedError),
        (BadRequestError("bad schema", 400), ProviderRefusedError),
        (UnprocessableEntityError("cannot", 422), ProviderRefusedError),
        (APIStatusError("teapot", 418), ProviderRefusedError),
        (APIStatusError("upstream on fire", 503), ProviderUnavailableError),
        (APIError("something"), ProviderUnavailableError),
        (ValueError("not theirs at all"), ProviderReplyError),
    ],
)
def test_every_sdk_failure_becomes_one_of_the_four(
    error: Exception, expected: type[Exception]
) -> None:
    assert isinstance(classify(FakeSdk(), error), expected)


async def test_a_failed_call_raises_the_neutral_error() -> None:
    module = FakeSdk()
    provider = a_provider(module)
    await provider.complete(a_request())
    module.clients[0].error = AuthenticationError("incorrect api key", 401)

    with pytest.raises(ProviderRefusedError):
        await provider.complete(a_request())


async def test_a_rate_limit_carries_the_wait_it_asked_for() -> None:
    module = FakeSdk()
    provider = a_provider(module)
    await provider.complete(a_request())
    module.clients[0].error = RateLimitError("slow down", 429, {"retry-after": "12"})

    with pytest.raises(ProviderRateLimitedError) as raised:
        await provider.complete(a_request())

    assert raised.value.retry_after_seconds == 12.0


def test_a_retry_after_that_is_a_date_is_ignored_rather_than_guessed_at() -> None:
    error = RateLimitError(
        "slow", 429, {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}
    )

    assert retry_after_seconds(error) is None


def test_a_missing_retry_after_is_simply_absent() -> None:
    assert retry_after_seconds(RateLimitError("slow", 429)) is None


def test_a_nonsense_retry_after_is_ignored() -> None:
    assert (
        retry_after_seconds(RateLimitError("slow", 429, {"retry-after": "-5"})) is None
    )


# --- shutdown --------------------------------------------------------------


async def test_closing_shuts_the_client_down() -> None:
    module = FakeSdk()
    provider = a_provider(module)
    await provider.complete(a_request())

    await provider.aclose()

    assert module.clients[0].closed


async def test_closing_a_provider_that_never_called_anything_is_fine() -> None:
    await a_provider(FakeSdk()).aclose()


async def test_closing_twice_is_fine() -> None:
    module = FakeSdk()
    provider = a_provider(module)
    await provider.complete(a_request())

    await provider.aclose()
    await provider.aclose()

    assert len(module.clients) == 1
