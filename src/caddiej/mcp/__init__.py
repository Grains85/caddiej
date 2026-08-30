"""The MCP server, as a separate process from the simulator.

Separate for one reason, which is worth stating because collapsing it into
CaddieJ would be less code: an assistant runtime is not something CaddieJ gets
to make promises about. It may hang, leak, be restarted by its own supervisor,
or be replaced wholesale by a different product next year. None of that may
happen inside the process that is drawing a marker on somebody's floor while
they stand over a ball.

So the arrangement is:

    assistant  ──MCP/stdio──▶  caddiej.mcp  ──loopback HTTP──▶  CaddieJ

and the middle process holds nothing. It imports two modules from CaddieJ, both
of which import nothing themselves, so it has no database handle, no credential
beyond one bearer token, and no way to reach anything except one authenticated
port. Killing it affects nothing. Never starting it affects nothing.

Nothing here names a particular assistant. OpenJarvis is the intended first
client and is mentioned nowhere in the code, because MCP is the contract and any
client that speaks it is equally welcome.
"""

from caddiej.mcp.client import (
    Caddie,
    CaddieJClient,
    CaddieJError,
    CaddieJUnreachable,
)
from caddiej.mcp.server import INSTRUCTIONS, ToolFailure, build_server, tool_handlers

__all__ = [
    "INSTRUCTIONS",
    "Caddie",
    "CaddieJClient",
    "CaddieJError",
    "CaddieJUnreachable",
    "ToolFailure",
    "build_server",
    "tool_handlers",
]
