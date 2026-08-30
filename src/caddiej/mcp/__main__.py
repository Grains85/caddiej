"""Run the MCP server: `python -m caddiej.mcp`.

Configured from the environment rather than from `Settings`, because this
process is started by somebody else's assistant runtime — an MCP client config
file, usually — and reading CaddieJ's `.env` from whatever directory that
runtime happens to launch in would be a guess. Four variables, all optional but
the token.

Logging goes to stderr, and it has to: stdout is the MCP transport, so a single
`print` anywhere in this process corrupts the protocol.
"""

import logging
import os
import sys

from caddiej.mcp.client import CaddieJClient
from caddiej.mcp.server import build_server

_DEFAULT_URL = "http://127.0.0.1:8731/v1"


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("CADDIEJ_MCP_LOG_LEVEL", "INFO").upper(),
        stream=sys.stderr,
        format="%(levelname)s %(name)s %(message)s",
    )
    logger = logging.getLogger("caddiej.mcp")

    token = os.environ.get("CADDIEJ_API_TOKEN", "").strip()
    if not token:
        logger.error(
            "CADDIEJ_API_TOKEN is not set. CaddieJ requires a bearer token; "
            "set the same value here and in CaddieJ's ASSISTANT_API_TOKEN."
        )
        return 2

    client = CaddieJClient(
        base_url=os.environ.get("CADDIEJ_API_URL", _DEFAULT_URL),
        token=token,
        timeout_seconds=float(os.environ.get("CADDIEJ_API_TIMEOUT", "5")),
    )
    allow_mutations = os.environ.get(
        "CADDIEJ_MCP_READ_ONLY", ""
    ).strip().lower() not in {
        "1",
        "true",
        "yes",
    }

    try:
        server = build_server(client, allow_mutations=allow_mutations)
    except ImportError:
        logger.exception(
            "the MCP SDK is not installed; install CaddieJ with the 'mcp' extra"
        )
        return 2

    # Not waiting for CaddieJ, and not checking whether it is up. An assistant
    # runtime starts this at login and keeps it for the session; refusing to
    # start because a golf simulator is not running would mean the tools only
    # existed if the room happened to be in use when somebody logged in.
    logger.info("caddiej mcp server starting for %s", client.base_url)
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
