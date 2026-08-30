"""Turning the SDK's exceptions into the four CaddieJ reacts to.

The distinction that matters is not which error occurred but what to do about it:
try again, try again later, or stop asking. Everything here is in service of that
one decision, so the mapping is deliberately coarse.

A wrong API key deserves particular care. It is a refusal, so it is never
retried: an evening of retry loops against a typo produces a log nobody reads and
a rate limit somebody eventually pays for.
"""

from typing import Any

from caddiej.coaching.provider import (
    ProviderError,
    ProviderRateLimitedError,
    ProviderRefusedError,
    ProviderReplyError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)


def classify(module: Any, error: Exception) -> ProviderError:
    """The neutral error for an SDK exception.

    The SDK module is passed in rather than imported, because it is an optional
    dependency and this must remain importable without it.
    """

    detail = f"{type(error).__name__}: {error}"

    if isinstance(error, module.APITimeoutError):
        return ProviderTimeoutError(detail)
    if isinstance(error, module.RateLimitError):
        return ProviderRateLimitedError(detail, retry_after_seconds(error))
    if isinstance(error, module.APIConnectionError):
        return ProviderUnavailableError(detail)
    if isinstance(
        error,
        (
            module.AuthenticationError,
            module.PermissionDeniedError,
            module.NotFoundError,
            module.BadRequestError,
            module.UnprocessableEntityError,
        ),
    ):
        return ProviderRefusedError(detail)
    if isinstance(error, module.APIStatusError):
        # Anything left with a status: their fault is worth another try, ours is
        # not.
        status = getattr(error, "status_code", 500)
        if status >= 500:
            return ProviderUnavailableError(detail)
        return ProviderRefusedError(detail)
    if isinstance(error, module.APIError):
        return ProviderUnavailableError(detail)
    return ProviderReplyError(detail)


def retry_after_seconds(error: Exception) -> float | None:
    """How long they asked us to wait, if they said.

    Honoured when present, because guessing at a backoff against a service that
    has just told you the answer is how a rate limit becomes a ban.
    """

    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        # The header may be an HTTP date. Parsing one to shave a second off a
        # backoff is not worth the code; the caller's own backoff covers it.
        return None
    return seconds if seconds >= 0 else None
