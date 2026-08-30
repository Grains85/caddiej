"""The MCP process: what it translates, and what it is unable to reach.

The isolation tests are the ones that matter most, because process separation is
the entire argument for this component existing. If importing the MCP server
pulls in persistence, then an assistant runtime that leaks or hangs is doing so
in a process holding a database handle, and the separation has bought nothing.

The rest is translation, tested against a fake CaddieJ. There is no golf here,
which is the point: this file should be boring, and if it ever stops being
boring then a decision has escaped the simulator.
"""

import importlib.util
import json
import subprocess
import sys
from typing import Any

import pytest

from caddiej.assistant.registry import MAX_COACHING_LINES, TOOLS, tool_named
from caddiej.mcp.client import Caddie, CaddieJError, CaddieJUnreachable
from caddiej.mcp.server import (
    INSTRUCTIONS,
    ToolFailure,
    build_server,
    tool_handlers,
)


class FakeCaddieJ:
    """A CaddieJ that records what it was asked and answers with a script.

    Satisfies the `Caddie` protocol rather than subclassing the real client, so
    no test here opens a socket or needs a token.
    """

    def __init__(
        self,
        answer: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.answer = answer if answer is not None else {"availability": "ok"}
        self.error = error
        self.gets: list[tuple[str, dict[str, Any] | None]] = []
        self.posts: list[tuple[str, dict[str, Any]]] = []

    async def get(
        self, path: str, parameters: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.gets.append((path, parameters))
        if self.error is not None:
            raise self.error
        return self.answer

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self.posts.append((path, body))
        if self.error is not None:
            raise self.error
        return self.answer


def handlers(client: Caddie | None = None) -> dict[str, Any]:
    return tool_handlers(client or FakeCaddieJ())


# --- isolation ------------------------------------------------------------


def imported_by(*modules: str) -> set[str]:
    program = (
        "import sys, json\n"
        f"for name in {list(modules)!r}:\n"
        "    __import__(name)\n"
        "print(json.dumps([n for n in sys.modules if n.startswith('caddiej')]))"
    )
    finished = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=True
    )
    return set(json.loads(finished.stdout))


def test_the_mcp_process_cannot_reach_the_simulator() -> None:
    """The whole isolation argument, as an import graph.

    Checked in a fresh interpreter, because inside this test session most of
    CaddieJ is already loaded and the question would be meaningless. What must
    not appear: persistence, coaching, the domain, any adapter, `Settings`. If
    one does, this process holds a database handle or a credential, and killing
    it stops being free.
    """

    loaded = imported_by("caddiej.mcp.server", "caddiej.mcp.client")
    forbidden = {
        name
        for name in loaded
        if name.split(".")[1:2]
        and name.split(".")[1]
        in {
            "persistence",
            "coaching",
            "domain",
            "adapters",
            "floor",
            "speech",
            "lighting",
            "effects",
            "video",
            "core",
            "runtime",
            "app",
            "config",
        }
    }

    assert forbidden == set(), (
        "the MCP server runs in its own process so that a hung assistant "
        f"runtime holds nothing; these imports would give it something: "
        f"{sorted(forbidden)}"
    )


def test_the_mcp_process_needs_only_the_contract_and_the_registry() -> None:
    loaded = imported_by("caddiej.mcp.server")
    assistant = {name for name in loaded if name.startswith("caddiej.assistant")}

    assert assistant == {
        "caddiej.assistant",
        "caddiej.assistant.contract",
        "caddiej.assistant.registry",
    }


def test_the_sdk_is_not_needed_to_import_anything() -> None:
    """CaddieJ must be installable and fully testable without the MCP SDK.

    Everything in this file except `build_server` runs without it, which is why
    the import is inside that function rather than at the top of the module.
    """

    program = (
        "import sys\nsys.modules['mcp'] = None\nimport caddiej.mcp.server\nprint('ok')"
    )
    finished = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=True
    )

    assert finished.stdout.strip() == "ok"


# --- translation ----------------------------------------------------------


def test_every_registered_tool_has_a_handler() -> None:
    assert set(handlers()) == {tool.name for tool in TOOLS}


async def test_each_read_calls_its_own_documented_path() -> None:
    client = FakeCaddieJ()
    every = tool_handlers(client)

    for tool in TOOLS:
        if not tool.is_read:
            continue
        await every[tool.name]()

    assert [path for path, _ in client.gets] == [
        tool.path for tool in TOOLS if tool.is_read
    ]


async def test_a_read_passes_the_answer_through_unchanged() -> None:
    # The translator holds no opinion about what came back. Reshaping it here
    # would make the contract two contracts.
    client = FakeCaddieJ(answer={"availability": "empty", "shot": None})

    result = await tool_handlers(client)["get_last_shot"]()

    assert result == {"availability": "empty", "shot": None}


async def test_a_session_id_travels_as_a_query_parameter() -> None:
    client = FakeCaddieJ()

    await tool_handlers(client)["get_last_shot"](session_id="abc")

    assert client.gets[0][1] == {"session_id": "abc"}


async def test_recent_coaching_sends_its_default_limit() -> None:
    client = FakeCaddieJ()

    await tool_handlers(client)["get_recent_coaching"]()

    path, parameters = client.gets[0]
    assert parameters is not None
    assert 1 <= parameters["limit"] <= MAX_COACHING_LINES


@pytest.mark.parametrize("limit", [0, -1, MAX_COACHING_LINES + 1])
async def test_a_bad_limit_is_refused_without_a_round_trip(limit: int) -> None:
    # Caught here as well as at the boundary. CaddieJ is the authority, but
    # correcting it in this process turns a round trip into an immediate answer.
    client = FakeCaddieJ()

    with pytest.raises(ToolFailure, match=str(MAX_COACHING_LINES)):
        await tool_handlers(client)["get_recent_coaching"](limit=limit)

    assert client.gets == []


async def test_the_mutation_posts_a_validated_focus() -> None:
    client = FakeCaddieJ()

    await tool_handlers(client)["set_training_focus"](focus="Face Control")

    assert client.posts == [("/training-focus", {"focus": "face_control"})]


async def test_a_focus_the_boundary_would_refuse_is_refused_here_first() -> None:
    client = FakeCaddieJ()

    with pytest.raises(ToolFailure, match="face_control"):
        await tool_handlers(client)["set_training_focus"](focus="swing harder")

    assert client.posts == []


@pytest.mark.parametrize("cleared", [None, "", "none", "  "])
async def test_clearing_the_focus_posts_a_null(cleared: str | None) -> None:
    client = FakeCaddieJ()

    await tool_handlers(client)["set_training_focus"](focus=cleared)

    assert client.posts[0][1] == {"focus": None}


# --- what an assistant is told when things go wrong -----------------------


async def test_caddiej_not_running_is_explained_rather_than_reported_as_no_data() -> (
    None
):
    """The failure an assistant is most likely to see, and most likely to
    misreport.

    CaddieJ is a program somebody starts when they walk into a golf room. An
    assistant asked about golf at lunchtime must say the simulator is not
    running, not that the user has not practised.
    """

    client = FakeCaddieJ(error=CaddieJUnreachable("connection refused"))

    with pytest.raises(ToolFailure) as failure:
        await tool_handlers(client)["get_last_shot"]()

    assert "does not appear to be running" in str(failure.value)
    assert "rather than reporting" in str(failure.value)


async def test_a_refusal_is_passed_on_in_caddiejs_own_words() -> None:
    client = FakeCaddieJ(error=CaddieJError("a bearer token is required"))

    with pytest.raises(ToolFailure, match="bearer token"):
        await tool_handlers(client)["get_caddiej_status"]()


async def test_no_other_exception_type_escapes_a_tool() -> None:
    # An MCP client shows tool errors to a model, so the text has to be a
    # sentence a model can act on rather than an internal one.
    for error in (CaddieJUnreachable("gone"), CaddieJError("no")):
        client = FakeCaddieJ(error=error)
        for name, handler in tool_handlers(client).items():
            with pytest.raises(ToolFailure):
                await handler()
            assert name


# --- registration ---------------------------------------------------------
#
# The only tests here that need the SDK, which is an optional extra. They skip
# rather than fail without it, because a checkout with no MCP installed is a
# supported way to run CaddieJ and its suite.

needs_sdk = pytest.mark.skipif(
    importlib.util.find_spec("mcp") is None,
    reason="install the 'mcp' extra to exercise tool registration",
)


@needs_sdk
async def test_the_server_registers_every_tool_with_its_documented_prose() -> None:
    server = build_server(FakeCaddieJ())

    registered = {tool.name: tool for tool in await server.list_tools()}

    assert set(registered) == {tool.name for tool in TOOLS}
    for spec in TOOLS:
        described = registered[spec.name]
        assert described.description is not None
        assert spec.summary in described.description


@needs_sdk
async def test_reads_are_annotated_as_read_only() -> None:
    # So a client can present them differently, and so a cautious operator can
    # allow the reads and gate the write.
    server = build_server(FakeCaddieJ())

    for tool in await server.list_tools():
        spec = tool_named(tool.name)
        assert spec is not None
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is spec.is_read


@needs_sdk
async def test_a_read_only_server_offers_no_way_to_change_anything() -> None:
    # Not registered rather than registered and refusing: an assistant should
    # discover a read-only CaddieJ by not being offered a mutation.
    server = build_server(FakeCaddieJ(), allow_mutations=False)

    names = {tool.name for tool in await server.list_tools()}

    assert names == {tool.name for tool in TOOLS if tool.is_read}


@needs_sdk
async def test_every_tool_declares_a_schema_for_its_arguments() -> None:
    server = build_server(FakeCaddieJ())

    for tool in await server.list_tools():
        spec = tool_named(tool.name)
        assert spec is not None
        properties = tool.input_schema.get("properties", {})
        assert set(properties) == {argument.name for argument in spec.arguments}


@needs_sdk
async def test_a_refusal_reaches_the_assistant_in_caddiejs_own_words() -> None:
    """The sentence is the entire value of raising `ToolFailure`.

    The SDK tells an anticipated failure from a crash, and a crash reaches the
    model as "Error executing tool <name>" with nothing else. Left as a plain
    `RuntimeError`, every refusal CaddieJ writes would be replaced by that — so
    an assistant would report the simulator broken when the answer was that
    somebody misspelled a training focus.
    """

    from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

    server = build_server(FakeCaddieJ())

    with pytest.raises(ToolError) as refusal:
        await server.call_tool("set_training_focus", {"focus": "swing harder"})

    # Anticipated, not a crash: the SDK logs the second with a traceback and
    # withholds its text.
    assert not isinstance(refusal.value, UnexpectedToolError)
    assert "swing harder" in str(refusal.value)
    assert "face_control" in str(refusal.value)


@needs_sdk
async def test_caddiej_being_switched_off_is_explained_to_the_assistant() -> None:
    # The likeliest thing an assistant meets: a golf room nobody is in.
    from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

    server = build_server(FakeCaddieJ(error=CaddieJUnreachable("nothing there")))

    with pytest.raises(ToolError) as refusal:
        await server.call_tool("get_last_shot", {})

    assert not isinstance(refusal.value, UnexpectedToolError)
    assert "does not appear to be running" in str(refusal.value)


def test_the_instructions_tell_a_model_the_thing_it_must_not_get_wrong() -> None:
    assert "empty" in INSTRUCTIONS
    assert "unavailable" in INSTRUCTIONS
    assert "get_caddiej_status" in INSTRUCTIONS
    # Nothing about any particular assistant. MCP is the contract, and any
    # client that speaks it is equally welcome.
    assert "openjarvis" not in INSTRUCTIONS.lower()


def test_nothing_in_the_mcp_package_names_a_particular_assistant() -> None:
    import caddiej.mcp.client as client_module
    import caddiej.mcp.server as server_module

    for module in (client_module, server_module):
        source = (module.__doc__ or "").lower()
        assert "openjarvis" not in source
