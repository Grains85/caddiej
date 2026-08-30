"""Loopback HTTP transport for the assistant façades.

An adapter in the strict sense used elsewhere in CaddieJ: it translates between
HTTP and `caddiej.assistant`, holds no rule about golf, and lets no framework
type past its own boundary. The reason it exists at all — rather than the MCP
server calling the façades directly — is process isolation. An assistant runtime
that hangs, leaks, crashes or gets restarted by its own supervisor must not be
able to do any of that inside the process driving the floor.
"""

from caddiej.adapters.api.guard import Guard, Refusal, is_loopback_bind
from caddiej.adapters.api.server import (
    PREFIX,
    AssistantServer,
    api_url,
    create_api_app,
)

__all__ = [
    "PREFIX",
    "AssistantServer",
    "Guard",
    "Refusal",
    "api_url",
    "create_api_app",
    "is_loopback_bind",
]
