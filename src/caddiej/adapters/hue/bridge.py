"""The only module in CaddieJ that speaks HTTP to a Hue bridge.

Everything Hue-shaped is here: that the modern API lives under `/clip/v2`, that
authentication is a header rather than a path segment, that brightness is a
percentage from a tenth to a hundred rather than a fraction, that a transition is
milliseconds inside a `dynamics` object, and that the bridge answers `200` with a
list of errors in the body when it has refused something. None of that reaches
the lighting service, which knows only about fractions and seconds.

**Certificate verification is deliberately off, and this is the one place that
decision lives.** A Hue bridge presents a self-signed certificate for its own
local address; there is no certificate authority in the chain that anything can
check, and there is no way to obtain one. The alternatives were the deprecated
plaintext v1 API, which is worse, or pinning the bridge's certificate by its
identifier, which is more code than this milestone justifies for a device on the
same LAN. So the transport is encrypted but unauthenticated, the application key
is still handled as a secret, and nothing here carries anything but a brightness.
Worth stating rather than leaving as a quiet `verify=False` in a constructor.

**One command per light, and the caller owns the budget.** A bridge tolerates
roughly ten light commands a second before it starts refusing them, and it is the
service that knows how many it is about to send. This module sends what it is
told, concurrently, and reports what came back.
"""

import asyncio
import logging
from typing import Any

from caddiej.lighting.port import LightingCommandError, LightingUnavailableError

logger = logging.getLogger(__name__)

OUTPUT_NAME = "hue"
API_ROOT = "/clip/v2/resource"
KEY_HEADER = "hue-application-key"

# Hue's own scale: a tenth of a per cent to a hundred, and zero is not a legal
# brightness. The service floors its fractions well above the bottom of this, so
# the conversion never has to invent a value.
BRIGHTNESS_MIN = 0.2
BRIGHTNESS_MAX = 100.0


def sdk() -> Any | None:
    """The HTTP client, or None if it was never installed.

    Optional on purpose: CaddieJ runs, coaches, films and speaks with no lighting
    dependency present at all, so its absence is a supported state rather than an
    import error at start-up.
    """

    try:
        import httpx
    except ModuleNotFoundError:
        return None
    return httpx


def to_percent(fraction: float) -> float:
    """A brightness fraction as Hue's percentage, inside Hue's legal range."""

    return round(min(BRIGHTNESS_MAX, max(BRIGHTNESS_MIN, fraction * 100.0)), 2)


def to_fraction(percent: float) -> float:
    """Hue's percentage back as a fraction, for the service's arithmetic."""

    return min(1.0, max(0.0, percent / 100.0))


class HueBridge:
    """A conversation with one bridge, and the only holder of the key."""

    def __init__(
        self,
        host: str,
        application_key: str,
        timeout_seconds: float = 3.0,
        module: Any | None = None,
        client: Any | None = None,
    ) -> None:
        self._host = host.strip().removeprefix("https://").removeprefix("http://")
        self._host = self._host.rstrip("/")
        self._key = application_key.strip()
        self._timeout = timeout_seconds
        self._module = module if module is not None else sdk()
        self._client = client

    @property
    def host(self) -> str:
        return self._host

    @property
    def is_configured(self) -> bool:
        """Whether this could work: a client, an address, and a key.

        Cheap by design — no socket is opened. A missing key is much the most
        likely of the three, because it comes from a one-time pairing step
        somebody has to do by hand.
        """

        if self._client is not None:
            return True
        if self._module is None:
            logger.warning("lighting_httpx_missing")
            return False
        if not self._host:
            logger.warning("lighting_bridge_unset")
            return False
        if not self._key:
            logger.warning("lighting_key_unset")
            return False
        return True

    async def open(self) -> None:
        """Build the client. Cheap, and opens no connection of its own."""

        if self._client is not None:
            return
        if self._module is None or not self._host or not self._key:
            raise LightingUnavailableError(
                "no Hue bridge is configured, or the HTTP client is not installed"
            )

        self._client = self._module.AsyncClient(
            base_url=f"https://{self._host}",
            headers={KEY_HEADER: self._key},
            timeout=self._timeout,
            # See this module's own note. A bridge's certificate is self-signed
            # for a private address and there is nothing to check it against.
            verify=False,
        )

    async def aclose(self) -> None:
        client = self._client
        self._client = None
        if client is None:
            return
        try:
            await client.aclose()
        except Exception:
            logger.warning("lighting_bridge_close_failed", exc_info=True)

    async def get(self, resource: str) -> list[dict[str, Any]]:
        """Everything of one kind the bridge knows about."""

        payload = await self._request("GET", f"{API_ROOT}/{resource}")
        data = payload.get("data")
        if not isinstance(data, list):
            raise LightingCommandError(
                f"the bridge answered {resource} with no data list"
            )
        return [item for item in data if isinstance(item, dict)]

    async def put(self, resource: str, identifier: str, body: dict[str, Any]) -> None:
        """Change one thing, and raise if the bridge would not."""

        await self._request("PUT", f"{API_ROOT}/{resource}/{identifier}", body)

    async def put_all(self, resource: str, bodies: dict[str, dict[str, Any]]) -> None:
        """Change several things at once, reporting whether any of them failed.

        Concurrent because the alternative is a round trip per lamp in series,
        and four lamps at thirty milliseconds each would put the last one a
        tenth of a second behind the first — visible, as a room that ripples
        instead of breathing.

        Every command is attempted even after one fails. A partial update is the
        better outcome: the lights that could be reached stay in step with the
        voice, and the one that could not is going to be restored anyway.
        """

        if not bodies:
            return
        results = await asyncio.gather(
            *(
                self.put(resource, identifier, body)
                for identifier, body in bodies.items()
            ),
            return_exceptions=True,
        )
        failures = [result for result in results if isinstance(result, BaseException)]
        if not failures:
            return
        for failure in failures:
            if isinstance(failure, asyncio.CancelledError):
                raise failure
        raise LightingCommandError(
            f"{len(failures)} of {len(bodies)} lights refused: {failures[0]}"
        )

    async def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """One call, with every way it can go wrong turned into one of two errors."""

        client = self._client
        if client is None:
            raise LightingUnavailableError("the bridge was not opened")

        try:
            response = await client.request(method, path, json=body)
        except Exception as error:
            # An unreachable address, a refused connection, a timeout, a TLS
            # failure. All the same thing from here: the bridge is not answering
            # right now, and the next command may well work.
            raise LightingCommandError(f"{type(error).__name__}: {error}") from error

        if response.status_code == 401 or response.status_code == 403:
            # The key is wrong or has been revoked. Retrying will not fix it, so
            # this is the one HTTP status worth treating as terminal.
            raise LightingUnavailableError(
                f"the bridge rejected the application key ({response.status_code})"
            )
        if response.status_code >= 400:
            raise LightingCommandError(f"the bridge answered {response.status_code}")

        try:
            payload = response.json()
        except Exception as error:
            raise LightingCommandError(f"the bridge answered unreadably: {error}")
        if not isinstance(payload, dict):
            raise LightingCommandError("the bridge answered with something unexpected")

        # Hue reports refusals in the body of a 200, so the status code alone is
        # not enough to know a command was carried out.
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            raise LightingCommandError(f"the bridge refused: {_describe(errors[0])}")
        return payload


def _describe(error: object) -> str:
    """A bridge error as one short line, without echoing the whole body back."""

    if isinstance(error, dict):
        return str(error.get("description", error))[:200]
    return str(error)[:200]
