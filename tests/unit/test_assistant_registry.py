"""The tool list, held to a literal.

The registry is the allowlist. If a capability can appear in it without somebody
noticing, then the allowlist is not one, so the central test here compares the
tool names against a set written out by hand. It is meant to fail on any change
— that is the review prompt.

The rest is about the shape of the surface: that nothing accepts a free-form
string except the one validated enum, and that every tool's prose says the thing
an assistant most needs to be told.
"""

from caddiej.assistant.contract import CONTRACT_VERSION, TRAINING_FOCUSES
from caddiej.assistant.registry import (
    DEFAULT_COACHING_LINES,
    MAX_COACHING_LINES,
    TOOL_NAMES,
    TOOLS,
    ToolKind,
    tool_named,
)

# Written out rather than derived. Changing this set is the point at which
# somebody has to think about whether a new capability belongs on a boundary an
# outside assistant can reach.
EXPECTED = {
    "get_caddiej_status",
    "get_last_shot",
    "get_session_stats",
    "get_current_club",
    "get_recent_coaching",
    "set_training_focus",
}


def test_these_are_the_tools() -> None:
    assert TOOL_NAMES == EXPECTED


def test_exactly_one_of_them_can_change_anything() -> None:
    mutations = {tool.name for tool in TOOLS if tool.kind is ToolKind.MUTATE}

    assert mutations == {"set_training_focus"}


def test_reads_are_gets_and_mutations_are_posts() -> None:
    # One declaration decides the HTTP method and the MCP read-only annotation,
    # so the two transports cannot disagree about whether a tool writes.
    for tool in TOOLS:
        assert tool.http_method == ("GET" if tool.is_read else "POST")


def test_every_path_is_versioned_and_distinct() -> None:
    paths = [tool.url_path for tool in TOOLS]

    assert len(set(paths)) == len(paths)
    for path in paths:
        assert path.startswith(f"/v{CONTRACT_VERSION}/")


def test_nothing_accepts_a_free_form_string_except_the_validated_focus() -> None:
    """Where "no arbitrary execution" comes from.

    Not from filtering the input. From there being nowhere for a path, an
    expression or a query fragment to travel: the only string argument on the
    whole surface is checked against a closed enum, and everything else is a
    UUID or a bounded integer.
    """

    strings = {
        (tool.name, argument.name)
        for tool in TOOLS
        for argument in tool.arguments
        if argument.json_type == "string"
    }

    assert strings == {
        ("set_training_focus", "focus"),
        ("get_last_shot", "session_id"),
        ("get_session_stats", "session_id"),
        ("get_recent_coaching", "session_id"),
    }


def test_no_argument_is_required() -> None:
    # Every tool answers something useful when called with nothing, which is how
    # an assistant that has just been asked "how am I hitting it" can act
    # without first interrogating the user for a session identifier.
    for tool in TOOLS:
        for argument in tool.arguments:
            assert argument.required is False


def test_the_coaching_limit_is_bounded_and_defaults_below_it() -> None:
    # An agent in a loop will ask for everything, and the answer goes into a
    # context window somebody is paying for.
    assert 0 < DEFAULT_COACHING_LINES < MAX_COACHING_LINES
    assert MAX_COACHING_LINES <= 100


def test_every_tool_has_prose_worth_giving_a_model() -> None:
    for tool in TOOLS:
        assert tool.summary.endswith(".")
        # Long enough to say what the tool means rather than restate its name.
        assert len(tool.description) > 200


def test_the_reads_that_can_be_unavailable_say_so_in_their_prose() -> None:
    # An assistant confidently reporting "you have not hit any shots" when a
    # database was locked is the most likely way this boundary produces a wrong
    # answer, so the prose has to warn about it wherever it can happen.
    for name in ("get_last_shot", "get_session_stats", "get_recent_coaching"):
        tool = tool_named(name)
        assert tool is not None
        assert "unavailable" in tool.description
        assert "empty" in tool.description


def test_the_status_tool_tells_a_model_to_read_capabilities() -> None:
    tool = tool_named("get_caddiej_status")

    assert tool is not None
    assert "capabilities" in tool.description


def test_the_mutation_lists_its_legal_values_and_asks_for_confirmation() -> None:
    tool = tool_named("set_training_focus")

    assert tool is not None
    for focus in TRAINING_FOCUSES:
        assert focus in tool.description
    # It changes how somebody will be coached, and they did not necessarily ask.
    assert "Confirm with the player" in tool.description


def test_an_unknown_name_is_not_a_tool() -> None:
    assert tool_named("delete_everything") is None
