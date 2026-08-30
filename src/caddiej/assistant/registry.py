"""The tools, written out one by one.

This is a literal list. It is not derived by reflection over the façades, and
that is the point: there is no mechanism here by which a method added to
`ReadFacade` becomes callable from outside, so a capability can only appear by
somebody typing it into this file. A test asserts the names against a literal
set, so adding one is a visible diff and adding one by accident is impossible.

Like `contract.py`, this module imports nothing from the rest of CaddieJ. Both
transports read it, and one of them runs in a different process.

The prose in each `description` is not documentation about the code — it is the
prompt. It is what a language model reads to decide whether to call the tool and
how to interpret the answer, so it is written for that reader: what the tool
tells you, when it will have nothing to say, and what it deliberately does not
cover. The `availability` note appears in most of them because an assistant
confidently reporting "you have not hit any shots" when a database was locked is
the most likely way this boundary produces a wrong answer.
"""

from dataclasses import dataclass, field
from enum import StrEnum

from caddiej.assistant.contract import CONTRACT_VERSION, TRAINING_FOCUSES

# How many coaching lines may be asked for at once. Bounded rather than merely
# positive: an agent in a loop will ask for everything, and the answer goes into
# a context window somebody is paying for.
MAX_COACHING_LINES = 50
DEFAULT_COACHING_LINES = 5


class ToolKind(StrEnum):
    """Whether calling this changes anything.

    Carried through to the transport as the HTTP method and to MCP as the
    read-only annotation, so one declaration decides all three.
    """

    READ = "read"
    MUTATE = "mutate"


@dataclass(frozen=True, slots=True)
class ToolArgument:
    """One argument, and the whole of what it may be.

    Note the absence of a free-form `string` argument anywhere in this file with
    the single exception of `focus`, which is validated against a closed enum.
    Nothing here accepts a path, an expression, a query fragment or a URL, which
    is what makes "no arbitrary execution" a property of the surface rather than
    a promise about the handlers.
    """

    name: str
    json_type: str
    description: str
    required: bool = False


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    kind: ToolKind
    path: str
    """The HTTP path, below the versioned prefix."""

    summary: str
    description: str
    arguments: tuple[ToolArgument, ...] = field(default_factory=tuple)

    @property
    def is_read(self) -> bool:
        return self.kind is ToolKind.READ

    @property
    def http_method(self) -> str:
        return "GET" if self.is_read else "POST"

    @property
    def url_path(self) -> str:
        return f"/v{CONTRACT_VERSION}{self.path}"


_SESSION_ID = ToolArgument(
    name="session_id",
    json_type="string",
    description=(
        "A session UUID. Omit for the session in progress, which is what an "
        "assistant asking about today wants."
    ),
)

_AVAILABILITY = (
    "Every answer carries `availability`: `ok` means the data is real, `empty` "
    "means CaddieJ is working and there is genuinely nothing yet, and "
    "`unavailable` means the subsystem is off or failed. Never report `empty` "
    "and `unavailable` as the same thing — one is about golf, the other is "
    "about a computer."
)

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="get_caddiej_status",
        kind=ToolKind.READ,
        path="/status",
        summary="Whether CaddieJ is running and which of its parts work.",
        description=(
            "Reports CaddieJ's version, how long it has been running, the "
            "session in progress if any, the configured player name, the "
            "current training focus, and whether each subsystem is enabled and "
            "available.\n\n"
            "Call this first when anything else answers `unavailable`, and read "
            "`capabilities` to see which tools will return data before calling "
            "them. This is the only tool that answers when everything else is "
            "degraded.\n\n"
            "`session_id` is null until the first shot of a run has been "
            "recorded, which is normal rather than a fault. There is no roster: "
            "`player_name` is a label the operator configured."
        ),
    ),
    ToolSpec(
        name="get_last_shot",
        kind=ToolKind.READ,
        path="/last-shot",
        summary="The most recent shot, with its measurements.",
        description=(
            "The last shot of a session: which club, which way round the player "
            "was standing, what the ball did, and how the club arrived if the "
            "launch monitor measured it.\n\n"
            "Units are in the field names. Most club-delivery fields are null "
            "on most devices, because most launch monitors do not measure them; "
            "a null means 'not measured', never zero. `face_to_path_degrees` is "
            "computed by CaddieJ and is the number that explains which way the "
            "ball curved.\n\n" + _AVAILABILITY
        ),
        arguments=(_SESSION_ID,),
    ),
    ToolSpec(
        name="get_session_stats",
        kind=ToolKind.READ,
        path="/session-stats",
        summary="Averages and spread for a session, overall and per club.",
        description=(
            "How a session has gone: shot count, when it started and when the "
            "first and last shots were struck, then per-measurement summaries "
            "overall and broken down by club, busiest club first. Each summary "
            "carries mean, median, minimum, maximum and standard deviation.\n\n"
            "`count` on a measurement is how many shots carried that "
            "measurement, not how many shots were hit — devices report "
            "different subsets. `faults` counts how many shots each named "
            "coaching fault accounted for, leaving out shots with nothing "
            "wrong, so an empty map after a long session means it went well.\n\n"
            "Shot-by-shot dispersion is deliberately not included: it is one "
            "row per shot and exists for drawing pictures.\n\n" + _AVAILABILITY
        ),
        arguments=(_SESSION_ID,),
    ),
    ToolSpec(
        name="get_current_club",
        kind=ToolKind.READ,
        path="/current-club",
        summary="What is in play right now, and whether a shot is imminent.",
        description=(
            "The club the simulator last said was in play, whether there is a "
            "ball on the mat, whether the launch monitor reports itself ready, "
            "and the player's handedness.\n\n"
            "This is live state rather than history, so it is `empty` rather "
            "than `unavailable` before a round starts or after the launch "
            "monitor disconnects. `ball_on_mat` and `launch_monitor_ready` are "
            "null when nothing knows: many devices report neither, and a device "
            "that says nothing is not a device saying no."
        ),
    ),
    ToolSpec(
        name="get_recent_coaching",
        kind=ToolKind.READ,
        path="/recent-coaching",
        summary="The last few things CaddieJ concluded about the player's swing.",
        description=(
            "Recent coaching outcomes, oldest of the recent ones first. Each "
            "carries the named fault, a severity from zero to one, CaddieJ's own "
            "band for that severity, and whether the words came from a template "
            "or a language model.\n\n"
            "The coaching prose itself is only included when the operator has "
            "switched that on; `prose_included` says which. When it is off, the "
            "named faults and severities are still present, which is enough to "
            "say what somebody has been fighting without reproducing a "
            "transcript of their practice session.\n\n"
            "A severity below 0.25 (`band` of `fine`) was not worth mentioning "
            "to the player and should not be presented as a problem. A `focus` "
            "of `none` means the shot had nothing wrong with it, so a run of "
            "them is a compliment rather than missing data.\n\n" + _AVAILABILITY
        ),
        arguments=(
            ToolArgument(
                name="limit",
                json_type="integer",
                description=(
                    f"How many lines to return, {1} to {MAX_COACHING_LINES}. "
                    f"Defaults to {DEFAULT_COACHING_LINES}."
                ),
            ),
            _SESSION_ID,
        ),
    ),
    ToolSpec(
        name="set_training_focus",
        kind=ToolKind.MUTATE,
        path="/training-focus",
        summary="Set what coaching should lean towards on the next shots.",
        description=(
            "Biases CaddieJ's coaching towards one thing the player wants to "
            "work on. This changes wording and emphasis on subsequent shots "
            "only: it does not alter measurements, scoring, what the floor "
            "shows, or anything already in flight.\n\n"
            "`focus` must be one of: " + ", ".join(TRAINING_FOCUSES) + ". Pass "
            "null to clear the preference and let CaddieJ pick whatever is "
            "worst on each shot.\n\n"
            "Confirm with the player before calling this. It changes how they "
            "will be coached, and they did not necessarily ask for it."
        ),
        arguments=(
            ToolArgument(
                name="focus",
                json_type="string",
                description="A coaching focus, or null to clear the preference.",
            ),
        ),
    ),
)

TOOL_NAMES = frozenset(tool.name for tool in TOOLS)


def tool_named(name: str) -> ToolSpec | None:
    for tool in TOOLS:
        if tool.name == name:
            return tool
    return None
