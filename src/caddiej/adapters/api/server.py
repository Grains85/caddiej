"""Loopback HTTP over the assistant façades. Transport only.

Every route below does the same four things and nothing else: check the guard,
validate arguments, call one façade method, return its model. No route holds a
rule about golf, and no FastAPI type escapes this module — which is what makes
the MCP server replaceable and this file uninteresting on purpose.

The routes are generated from nothing. Each is written out against a
`ToolSpec` in the registry, and a test asserts that the set of registered paths
matches the registry exactly, so a tool cannot exist in one place and not the
other.

Two things this deliberately does not do. It does not mount an OpenAPI schema:
the contract is `caddiej.assistant.contract` plus the documented tool reference,
and a second generated description of the same surface would drift from it. And
it does not construct the command façade when mutation is switched off — the
POST route is absent rather than present-and-refusing, so a read-only
deployment can be confirmed by asking the server what it serves.
"""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import FastAPI, Header, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from caddiej.adapters.api.guard import Guard, Refusal
from caddiej.adapters.asgi import LocalAsgiServer
from caddiej.assistant.commands import CommandFacade, FocusRejected
from caddiej.assistant.contract import (
    CONTRACT_VERSION,
    ErrorResult,
    SetTrainingFocusArguments,
)
from caddiej.assistant.reads import ReadFacade
from caddiej.assistant.registry import (
    DEFAULT_COACHING_LINES,
    MAX_COACHING_LINES,
    TOOLS,
    tool_named,
)

logger = logging.getLogger(__name__)

PREFIX = f"/v{CONTRACT_VERSION}"

# Which refusals are the caller's fault and which are the operator's. A missing
# token is 401 because presenting one would fix it; a browser origin is 403
# because nothing the caller can send would.
_STATUS = {
    Refusal.NO_TOKEN_CONFIGURED: status.HTTP_503_SERVICE_UNAVAILABLE,
    Refusal.MISSING_TOKEN: status.HTTP_401_UNAUTHORIZED,
    Refusal.BAD_TOKEN: status.HTTP_401_UNAUTHORIZED,
    Refusal.BROWSER_ORIGIN: status.HTTP_403_FORBIDDEN,
    Refusal.FOREIGN_HOST: status.HTTP_403_FORBIDDEN,
}

_REFUSAL_DETAIL = {
    Refusal.NO_TOKEN_CONFIGURED: "the assistant API has no token configured",
    Refusal.MISSING_TOKEN: "a bearer token is required",
    Refusal.BAD_TOKEN: "a bearer token is required",
    Refusal.BROWSER_ORIGIN: "requests carrying an Origin header are refused",
    Refusal.FOREIGN_HOST: "this API serves loopback requests only",
}

Authorization = Annotated[str | None, Header(alias="Authorization")]
Origin = Annotated[str | None, Header(alias="Origin")]
Host = Annotated[str | None, Header(alias="Host")]


def create_api_app(
    reads: ReadFacade,
    guard: Guard,
    commands: CommandFacade | None = None,
) -> FastAPI:
    """Build the ASGI application for the assistant API."""

    app = FastAPI(
        title="CaddieJ assistant API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # Two handlers so that every refusal this API can produce has the contract's
    # shape, whether a route decided it or the framework did. Without them a
    # caller has to parse `ErrorResult` for a bad token, a list of nested
    # objects for a bad argument, and a bare string for an absent route — three
    # shapes for one question, two of them written for a developer reading a
    # browser console rather than for a client deciding what to do next.
    @app.exception_handler(RequestValidationError)
    async def invalid_arguments(
        _request: Request, error: RequestValidationError
    ) -> Response:
        return _bad_request(_explain(error))

    @app.exception_handler(StarletteHTTPException)
    async def no_such_route(
        _request: Request, error: StarletteHTTPException
    ) -> Response:
        # Chiefly the mutating path on a read-only deployment, which is a
        # question an assistant asks by accident and should be answered
        # plainly: this CaddieJ does not serve that.
        return _error(
            error.status_code,
            error="no_such_tool",
            detail="this CaddieJ does not serve that path",
        )

    def refuse(
        authorization: str | None, origin: str | None, host: str | None
    ) -> Response | None:
        reason = guard.refuse(authorization, origin, host)
        if reason is None:
            return None
        logger.warning(
            "assistant_request_refused",
            extra={"assistant_refusal": reason.value},
        )
        return _error(
            _STATUS[reason],
            error=reason.value,
            detail=_REFUSAL_DETAIL[reason],
        )

    @app.get(f"{PREFIX}/tools")
    async def tools(
        authorization: Authorization = None,
        origin: Origin = None,
        host: Host = None,
    ) -> Response:
        """What this build serves, so a client can check before it calls.

        Reports the mutating tools only when they are actually registered, so
        this doubles as the answer to "is this deployment read-only".
        """

        if (refused := refuse(authorization, origin, host)) is not None:
            return refused
        return JSONResponse(
            {
                "contract_version": CONTRACT_VERSION,
                "tools": [
                    {
                        "name": tool.name,
                        "kind": tool.kind.value,
                        "method": tool.http_method,
                        "path": tool.url_path,
                        "summary": tool.summary,
                        "description": tool.description,
                        "arguments": [
                            {
                                "name": argument.name,
                                "type": argument.json_type,
                                "required": argument.required,
                                "description": argument.description,
                            }
                            for argument in tool.arguments
                        ],
                    }
                    for tool in TOOLS
                    if tool.is_read or commands is not None
                ],
            }
        )

    @app.get(_path("get_caddiej_status"))
    async def get_status(
        authorization: Authorization = None,
        origin: Origin = None,
        host: Host = None,
    ) -> Response:
        if (refused := refuse(authorization, origin, host)) is not None:
            return refused
        return _ok(reads.status())

    @app.get(_path("get_last_shot"))
    async def get_last_shot(
        session_id: Annotated[str | None, Query()] = None,
        authorization: Authorization = None,
        origin: Origin = None,
        host: Host = None,
    ) -> Response:
        if (refused := refuse(authorization, origin, host)) is not None:
            return refused
        try:
            target = _session(session_id)
        except ValueError as error:
            return _bad_request(str(error))
        return _ok(await reads.last_shot(target))

    @app.get(_path("get_session_stats"))
    async def get_session_stats(
        session_id: Annotated[str | None, Query()] = None,
        authorization: Authorization = None,
        origin: Origin = None,
        host: Host = None,
    ) -> Response:
        if (refused := refuse(authorization, origin, host)) is not None:
            return refused
        try:
            target = _session(session_id)
        except ValueError as error:
            return _bad_request(str(error))
        return _ok(await reads.session_stats(target))

    @app.get(_path("get_current_club"))
    async def get_current_club(
        authorization: Authorization = None,
        origin: Origin = None,
        host: Host = None,
    ) -> Response:
        if (refused := refuse(authorization, origin, host)) is not None:
            return refused
        return _ok(reads.current_club())

    @app.get(_path("get_recent_coaching"))
    async def get_recent_coaching(
        limit: Annotated[
            int, Query(ge=1, le=MAX_COACHING_LINES)
        ] = DEFAULT_COACHING_LINES,
        session_id: Annotated[str | None, Query()] = None,
        authorization: Authorization = None,
        origin: Origin = None,
        host: Host = None,
    ) -> Response:
        if (refused := refuse(authorization, origin, host)) is not None:
            return refused
        try:
            target = _session(session_id)
        except ValueError as error:
            return _bad_request(str(error))
        return _ok(await reads.recent_coaching(limit, target))

    # Registered inside the branch on purpose. With mutation switched off the
    # route does not exist, rather than existing and refusing, so "this
    # deployment cannot change anything" is visible from the route table.
    if commands is not None:
        writer = commands

        @app.post(_path("set_training_focus"))
        async def set_training_focus(
            arguments: SetTrainingFocusArguments,
            authorization: Authorization = None,
            origin: Origin = None,
            host: Host = None,
        ) -> Response:
            if (refused := refuse(authorization, origin, host)) is not None:
                return refused
            try:
                return _ok(writer.set_training_focus(arguments.focus))
            except FocusRejected as error:
                return _bad_request(str(error))

    return app


def _path(tool_name: str) -> str:
    """The registered path for a tool, from the registry rather than a literal.

    So a route and its documented contract cannot disagree: changing the path in
    the registry moves the route, and a tool with no spec fails at import.
    """

    tool = tool_named(tool_name)
    if tool is None:  # pragma: no cover - a typo here fails every test at import
        raise KeyError(f"no tool named {tool_name} in the registry")
    return tool.url_path


def _explain(error: RequestValidationError) -> str:
    """One sentence naming what was wrong with which argument.

    Deliberately built from the location and the message only. Pydantic's error
    entries also carry the offending input, and echoing that back would put a
    caller's own bytes into CaddieJ's log lines and an assistant's context.
    """

    faults = []
    for entry in error.errors():
        location = ".".join(
            str(part) for part in entry.get("loc", ()) if part != "body"
        )
        message = str(entry.get("msg", "is not acceptable"))
        faults.append(f"{location}: {message}" if location else message)
    return "; ".join(faults) or "the request could not be understood"


def _session(value: str | None) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(value)
    except ValueError:
        raise ValueError("session_id must be a UUID") from None


def _ok(payload: BaseModel) -> Response:
    # `model_dump_json` rather than returning the model, so that datetimes and
    # enums serialise exactly as the contract's golden files say they do rather
    # than as FastAPI's response encoder happens to.
    return Response(content=payload.model_dump_json(), media_type="application/json")


def _bad_request(detail: str) -> Response:
    return _error(status.HTTP_400_BAD_REQUEST, error="invalid_argument", detail=detail)


def _error(code: int, error: str, detail: str) -> Response:
    return Response(
        status_code=code,
        content=ErrorResult(error=error, detail=detail).model_dump_json(),
        media_type="application/json",
    )


class AssistantServer(LocalAsgiServer):
    """Owns the uvicorn server for the assistant API."""

    def __init__(self, app: FastAPI, host: str, port: int) -> None:
        super().__init__(app, host=host, port=port, name="assistant API server")


def api_url(host: str, port: int) -> str:
    """The base URL an MCP server should be pointed at."""

    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}{PREFIX}"
