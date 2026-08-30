"""The MCP server. A translator, and nothing more.

It runs in its own process, speaks MCP to an assistant on stdin and stdout, and
speaks loopback HTTP to CaddieJ. There is no third thing it does. Every tool
below is the same four lines — take validated arguments, call one HTTP path,
hand back what came out — and the flatness is the point: if this file ever grows
a decision about golf, that decision has escaped the simulator and is now in a
process an assistant's runtime can restart at will.

What it imports is the load-bearing detail. Only `caddiej.assistant.contract`
and `caddiej.assistant.registry`, both of which import nothing from CaddieJ.
No domain, no persistence, no coaching, no adapters, no `Settings`. So this
process holds no database handle, no OBS connection, no API key, and no ability
to reach anything except one authenticated HTTP port. A test asserts that
import graph, because it is the whole isolation argument and a convenience
import would quietly end it.

The MCP SDK is imported lazily, inside `build_server`. CaddieJ must remain
installable and fully testable without it, and everything in this module except
that one function is exercised by tests that never import it.
"""

import logging
from collections.abc import Callable
from functools import wraps
from typing import TYPE_CHECKING, Any

from caddiej.assistant.contract import CONTRACT_VERSION, TRAINING_FOCUSES
from caddiej.assistant.registry import (
    DEFAULT_COACHING_LINES,
    MAX_COACHING_LINES,
    TOOLS,
    ToolSpec,
    tool_named,
)
from caddiej.mcp.client import Caddie, CaddieJError, CaddieJUnreachable

if TYPE_CHECKING:  # pragma: no cover - the SDK is an optional dependency
    from mcp.server import MCPServer

logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
CaddieJ is a golf simulator orchestration layer running on the user's own
machine. These tools report what a launch monitor measured and what CaddieJ
concluded about it, and one of them sets what coaching leans towards.

Read `availability` on every answer. `empty` means CaddieJ is working and there
is nothing to report yet; `unavailable` means a subsystem is off or failed.
Reporting the second as the first tells the user something untrue about their
own practice.

Call get_caddiej_status when anything answers `unavailable`, and read its
`capabilities` before assuming a tool will return data. Units are in field
names. A null measurement means the device did not measure it, never zero.
"""


class ToolFailure(RuntimeError):
    """What an assistant is told when a tool could not run.

    Deliberately the only exception type that leaves a tool. An MCP client shows
    tool errors to a model, so the text has to be a sentence a model can act on
    rather than an internal one it will try to interpret.
    """


def _describe(tool: ToolSpec) -> str:
    """The tool's prose, with its summary as the first line."""

    return f"{tool.summary}\n\n{tool.description}"


async def _call(
    client: Caddie,
    tool_name: str,
    parameters: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One HTTP call, with every failure turned into a sentence.

    Every tool goes through here so that "CaddieJ is not running" reads the same
    way whichever question was asked, and so no urllib exception can reach an
    assistant.
    """

    tool = tool_named(tool_name)
    if tool is None:  # pragma: no cover - would fail at import in every test
        raise ToolFailure(f"{tool_name} is not a CaddieJ tool")
    try:
        if body is not None:
            return await client.post(tool.path, body)
        return await client.get(tool.path, parameters)
    except CaddieJUnreachable as error:
        raise ToolFailure(
            f"CaddieJ does not appear to be running. {error} "
            "Tell the user the simulator is not started rather than reporting "
            "that they have no golf data."
        ) from None
    except CaddieJError as error:
        raise ToolFailure(str(error)) from None


def _validate_limit(limit: int) -> int:
    if not 1 <= limit <= MAX_COACHING_LINES:
        raise ToolFailure(
            f"limit must be between 1 and {MAX_COACHING_LINES}, not {limit}"
        )
    return limit


def _validate_focus(focus: str | None) -> str | None:
    """Refuse a bad focus here as well as at the boundary.

    Duplicated on purpose. CaddieJ validates it too and is the authority, but
    catching it in this process turns a round trip into an immediate correction,
    and the message names the legal values — which is the difference between an
    assistant fixing its own mistake and an assistant telling the user it
    failed.
    """

    if focus is None:
        return None
    candidate = focus.strip().lower().replace(" ", "_").replace("-", "_")
    if not candidate or candidate == "none":
        return None
    if candidate not in TRAINING_FOCUSES:
        raise ToolFailure(
            f"'{focus}' is not a CaddieJ training focus. Choose one of: "
            + ", ".join(TRAINING_FOCUSES)
        )
    return candidate


def tool_handlers(client: Caddie) -> dict[str, Callable[..., Any]]:
    """Every tool as a plain async function, keyed by its MCP name.

    Separated from registration so the translation can be tested against a fake
    client with the MCP SDK absent. What `build_server` adds is the SDK's
    schema derivation and its stdio loop, neither of which is CaddieJ's logic.
    """

    async def get_caddiej_status() -> dict[str, Any]:
        return await _call(client, "get_caddiej_status")

    async def get_last_shot(session_id: str | None = None) -> dict[str, Any]:
        return await _call(client, "get_last_shot", {"session_id": session_id})

    async def get_session_stats(session_id: str | None = None) -> dict[str, Any]:
        return await _call(client, "get_session_stats", {"session_id": session_id})

    async def get_current_club() -> dict[str, Any]:
        return await _call(client, "get_current_club")

    async def get_recent_coaching(
        limit: int = DEFAULT_COACHING_LINES, session_id: str | None = None
    ) -> dict[str, Any]:
        return await _call(
            client,
            "get_recent_coaching",
            {"limit": _validate_limit(limit), "session_id": session_id},
        )

    async def set_training_focus(focus: str | None = None) -> dict[str, Any]:
        return await _call(
            client,
            "set_training_focus",
            body={"focus": _validate_focus(focus)},
        )

    return {
        "get_caddiej_status": get_caddiej_status,
        "get_last_shot": get_last_shot,
        "get_session_stats": get_session_stats,
        "get_current_club": get_current_club,
        "get_recent_coaching": get_recent_coaching,
        "set_training_focus": set_training_focus,
    }


def _anticipated(
    handler: Callable[..., Any], reported: type[Exception]
) -> Callable[..., Any]:
    """Re-raise CaddieJ's refusals as the SDK's own anticipated-failure type.

    The SDK draws a line between a failure a tool saw coming and a crash. The
    first reaches the model with its message; the second is replaced by
    "Error executing tool <name>" and logged with a traceback, on the reasonable
    grounds that an unplanned exception's text is not something to show anybody.

    Every `ToolFailure` is the first kind — CaddieJ is not running, that is not a
    training focus, that limit is too high — and each of those sentences is the
    entire value of raising it. Without this the assistant is told only that
    something failed, which is how a model ends up telling somebody their golf
    simulator is broken when the answer was that they misspelled "tempo".

    Taking the exception class as an argument keeps the SDK import inside
    `build_server`, so nothing above needs it installed.
    """

    @wraps(handler)
    async def reporting(*arguments: Any, **keywords: Any) -> Any:
        try:
            return await handler(*arguments, **keywords)
        except ToolFailure as failure:
            raise reported(str(failure)) from failure

    return reporting


def build_server(client: Caddie, allow_mutations: bool = True) -> "MCPServer[None]":
    """Register the tools with the MCP SDK.

    The SDK derives each tool's argument schema from the handler's signature and
    its description from the registry, so a tool is described in one place.

    `allow_mutations` off means the mutating tools are not registered at all
    rather than registered and refusing. An assistant should discover a
    read-only CaddieJ by not being offered a way to change anything.
    """

    from mcp.server import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations

    server = MCPServer(
        name="caddiej",
        title="CaddieJ",
        version=f"{CONTRACT_VERSION}",
        instructions=INSTRUCTIONS,
    )

    handlers = tool_handlers(client)
    for tool in TOOLS:
        if not tool.is_read and not allow_mutations:
            continue
        server.add_tool(
            _anticipated(handlers[tool.name], ToolError),
            name=tool.name,
            description=_describe(tool),
            annotations=ToolAnnotations(
                read_only_hint=tool.is_read,
                # Nothing here deletes or overwrites anything a user would miss,
                # and the one mutation is safe to repeat with the same argument.
                destructive_hint=False,
                idempotent_hint=True,
                # Everything is answered from this machine.
                open_world_hint=False,
            ),
        )
    return server
