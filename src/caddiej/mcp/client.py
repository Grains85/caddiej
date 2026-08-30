"""Talking to CaddieJ over loopback HTTP, from a process that is not CaddieJ.

Written on `urllib.request` rather than an HTTP client library, and that is a
considered choice rather than asceticism. This client makes one kind of request
— a short GET or POST to 127.0.0.1 with a bearer token — and the whole of what
an async client would buy is connection pooling for a call that happens when
somebody asks a question out loud. Against that, a dependency here would be
installed into whatever environment the operator runs their assistant in, which
is the one place in CaddieJ where dependencies are least under our control.

The blocking call goes through `asyncio.to_thread`, so the MCP server's event
loop stays free.

What this does not do is retry. A caddie asked a question either answers or says
it cannot; retrying behind the assistant's back turns a two-second refusal into
a thirty-second silence, and the assistant is the thing that should decide
whether to ask again.
"""

import asyncio
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class CaddieJError(RuntimeError):
    """CaddieJ could not be asked, or refused to answer."""


class CaddieJUnreachable(CaddieJError):
    """Nothing is listening, or it did not answer in time.

    Its own class because it is the expected case rather than a fault: CaddieJ
    is a program somebody starts when they walk into a golf room, and an
    assistant asking about golf at lunchtime should be told the simulator is not
    running rather than shown an error.
    """


class Caddie(Protocol):
    """Whatever the tools ask their questions through.

    The tools depend on this rather than on the client below, so the translation
    can be tested without a socket and so replacing urllib with something else
    would touch one file.
    """

    async def get(
        self, path: str, parameters: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class CaddieJClient:
    """A pointer at a running CaddieJ, and the token to talk to it."""

    base_url: str
    token: str
    timeout_seconds: float = 5.0

    async def get(
        self, path: str, parameters: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        query = {
            name: value
            for name, value in (parameters or {}).items()
            if value is not None
        }
        url = self.base_url.rstrip("/") + path
        if query:
            url = f"{url}?{urlencode(query)}"
        return await asyncio.to_thread(self._request, url, None)

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        url = self.base_url.rstrip("/") + path
        return await asyncio.to_thread(self._request, url, json.dumps(body).encode())

    def _request(self, url: str, body: bytes | None) -> dict[str, Any]:
        request = urllib.request.Request(  # noqa: S310 - loopback http by design
            url,
            data=body,
            method="POST" if body is not None else "GET",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                # No Origin header. CaddieJ refuses any request carrying one,
                # because only a browser sends it.
                **({"Content-Type": "application/json"} if body else {}),
            },
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - loopback http by design
                request, timeout=self.timeout_seconds
            ) as response:
                return _decode(response.read(_MAX_RESPONSE_BYTES + 1))
        except urllib.error.HTTPError as error:
            # A refusal has a body worth reading: CaddieJ puts a machine-readable
            # reason in it, and passing that through is more use to an assistant
            # than a status code.
            raise CaddieJError(_refusal(error)) from None
        except urllib.error.URLError as error:
            raise CaddieJUnreachable(
                f"CaddieJ is not answering on {self.base_url}: {error.reason}"
            ) from None
        except TimeoutError:
            raise CaddieJUnreachable(
                f"CaddieJ did not answer within {self.timeout_seconds} seconds"
            ) from None


def _decode(raw: bytes) -> dict[str, Any]:
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise CaddieJError("CaddieJ sent a response larger than expected")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise CaddieJError("CaddieJ sent a reply that was not JSON") from None
    if not isinstance(payload, dict):
        raise CaddieJError("CaddieJ sent a reply that was not an object")
    return payload


def _refusal(error: urllib.error.HTTPError) -> str:
    """CaddieJ's own words for why it refused, or the status if it had none."""

    try:
        payload = json.loads(error.read(_MAX_RESPONSE_BYTES))
        detail = payload.get("detail")
        reason = payload.get("error")
    except Exception:
        detail = reason = None
    if isinstance(detail, str) and detail:
        return detail if not reason else f"{reason}: {detail}"
    return f"CaddieJ refused the request with status {error.code}"
