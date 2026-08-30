"""Who is allowed to ask, on a machine where anything local can reach the port.

Loopback is necessary here and nowhere near sufficient, which is worth stating
plainly because "it only listens on 127.0.0.1" is the usual reason a local API
has no authentication.

The attack this defends against is DNS rebinding. Any web page the operator
visits can make requests to `http://127.0.0.1:8731/`, because the browser will
resolve an attacker's hostname to loopback quite happily and then treat the
result as that attacker's own origin. A local API with no token is therefore
readable by any web page, and a local API with a mutation route is writable by
one. Three gates, each cheap and each covering a different way in:

1. **A bearer token.** The real defence. A page cannot guess it, and it is not
   in a URL where it would land in a log or a referrer header.
2. **No `Origin` header at all.** A legitimate MCP server or `curl` sends none;
   a browser always sends one. So the presence of the header is itself the
   signal, and rejecting on it closes the browser path even if a token leaked.
3. **A loopback `Host`.** Cheap, and it refuses a request that arrived claiming
   to be for somebody else's hostname.

None of the three involves CORS. No CORS middleware is installed: not
permissive, not restrictive, absent. There is no browser client for this API and
adding the headers would only help build one.
"""

import logging
import secrets
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


class Refusal(StrEnum):
    """Why a request was refused. Named so the log says which gate closed."""

    NO_TOKEN_CONFIGURED = "no_token_configured"
    MISSING_TOKEN = "missing_token"
    BAD_TOKEN = "bad_token"
    BROWSER_ORIGIN = "browser_origin"
    FOREIGN_HOST = "foreign_host"


@dataclass(frozen=True, slots=True)
class Guard:
    """The three gates, as one decision.

    Holds the token as a plain string because it has already been taken out of
    `SecretStr` by the caller for comparison. It is never logged, never returned
    in a body, and never included in a refusal.
    """

    token: str
    require_loopback_host: bool = True

    def refuse(
        self,
        authorization: str | None,
        origin: str | None,
        host: str | None,
    ) -> Refusal | None:
        """The reason to refuse, or None to let the request through."""

        if not self.token:
            # Fail closed. An unauthenticated local API is not a lesser version
            # of this feature, it is a different and worse one, so a missing
            # token refuses every request rather than allowing every request.
            return Refusal.NO_TOKEN_CONFIGURED
        if origin is not None:
            return Refusal.BROWSER_ORIGIN
        if self.require_loopback_host and not _is_loopback(host):
            return Refusal.FOREIGN_HOST

        presented = _bearer(authorization)
        if presented is None:
            return Refusal.MISSING_TOKEN
        # Constant time, so a wrong token cannot be improved on by measuring how
        # long the refusal took.
        if not secrets.compare_digest(presented, self.token):
            return Refusal.BAD_TOKEN
        return None


def _bearer(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip() or None


def _is_loopback(host: str | None) -> bool:
    """Whether a `Host` header names this machine.

    A missing header is allowed: HTTP/1.0 clients omit it, and the token has
    already done the real work by this point.
    """

    if host is None:
        return True
    # `urlsplit` needs a scheme to populate `hostname`, and it is the only
    # correct way to strip a port from an IPv6 literal in brackets.
    hostname = urlsplit(f"//{host}").hostname
    if hostname is None:
        return False
    return hostname.lower() in _LOOPBACK_HOSTS


def is_loopback_bind(host: str) -> bool:
    """Whether binding to this address keeps the API on the machine.

    Used to refuse a non-loopback bind at start-up rather than to check a
    request. The HUD passes its host straight to uvicorn, so `HUD_HOST=0.0.0.0`
    silently works; this surface has a token and a mutation route, so the same
    mistake is worth refusing outright.
    """

    return host.strip().lower() in _LOOPBACK_HOSTS
