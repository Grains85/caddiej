"""The published surface. Everything an outside assistant is allowed to know.

This module is the whole API. Not the HTTP routes, not the MCP tool
definitions — those are two transports over what is written here, and either
could be replaced without an assistant noticing. It is also the only thing the
out-of-process MCP server imports from CaddieJ, which is what keeps that server
from growing opinions about golf.

Three rules hold it together.

**Nothing internal appears here.** Not a `Shot`, not a `SessionAnalytics`, not
a `*Health` dataclass. Every model below is written out by hand, and the tedium
is the feature: renaming a field in the domain cannot silently become a breaking
change to somebody's assistant, because the two are not the same declaration.
`/healthz` is the counter-example to avoid — it is `asdict()` over internal
dataclasses, so its shape is whatever those happen to contain today, and it
grew two fields last week without anybody thinking of it as an API change.

**Units are in field names.** `ball_speed_mph`, not `ball_speed`. The consumer
here is frequently a language model, and a model that has to infer units from
context will eventually infer them wrong and tell somebody they hit a 7-iron 214
metres. Spelling them out costs nothing and removes the whole class.

**Absence is never ambiguous.** Every answer carries an `availability`, because
"nobody has hit a ball yet" and "the database is locked" are different facts
that an assistant will otherwise flatten into the same confident sentence. This
is the single most important thing in the file: a null with no explanation is
how an assistant ends up telling a player they have not practised today.

## Compatibility

`CONTRACT_VERSION` is a major version and appears in the URL path. Within one
major, only additive changes are allowed:

* adding an optional field with a default — allowed;
* adding a member to an enum — allowed, and clients must tolerate unknown ones;
* renaming, removing, retyping or narrowing anything — not allowed, ever.

Golden files in `tests/unit/test_assistant_contract.py` hold the serialised
shape of every model, so breaking one of those rules fails a test rather than
somebody's evening.
"""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CONTRACT_VERSION = 1
"""The major version of everything in this module. Carried in the URL path."""

TRAINING_FOCUSES: tuple[str, ...] = (
    "face_control",
    "path",
    "strike",
    "low_point",
    "speed",
    "start_line",
    "curve",
    "launch",
    "spin",
    "consistency",
    "tempo",
    "posture",
    "early_extension",
    "sway",
)
"""Every value `set_training_focus` accepts, spelled out rather than imported.

Written as literals so this module keeps importing nothing from CaddieJ, which
is what lets the out-of-process MCP server share it. `CoachingFocus.NONE` is
absent because it names a shot with nothing wrong rather than something to work
on; clearing a preference is done with null.

A unit test asserts this equals the domain enum, so the two cannot drift — the
same trade the floor demo makes for its flourishes.
"""


class _Wire(BaseModel):
    """Base for every model that crosses the boundary.

    `extra="forbid"` matters most on the way in. An argument CaddieJ does not
    recognise means the caller believes something untrue about this API, and
    accepting it quietly is how an assistant comes to think it turned a feature
    on. The HUD's client protocol deliberately does the opposite, because a
    browser may legitimately be older than the server it is talking to; an
    assistant talking to a versioned surface has no such excuse.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class Availability(StrEnum):
    """Why an answer looks the way it does.

    Three values rather than a bare null, because a null cannot distinguish the
    two things an assistant most needs to tell apart.
    """

    OK = "ok"
    """The answer is the answer."""

    EMPTY = "empty"
    """CaddieJ is working and there is genuinely nothing to report. Nobody has
    hit a ball, or nothing has been coached yet."""

    UNAVAILABLE = "unavailable"
    """CaddieJ could not answer: the subsystem is switched off, failed to
    start, or errored. Say so rather than reporting an absence of golf."""


# --- shots --------------------------------------------------------------


class BallPayload(_Wire):
    """What the ball did. Only the speed is always there.

    The launch angles are null for a shot CaddieJ read from the simulator's own
    round file rather than from a launch monitor: that file records a ball
    speed and a distance and no angles. An assistant reading this must treat a
    null as "not measured" and not as zero degrees.
    """

    speed_mph: float
    launch_angle_degrees: float | None = None
    launch_direction_degrees: float | None = None
    carry_yards: float | None = None
    total_spin_rpm: float | None = None
    back_spin_rpm: float | None = None
    side_spin_rpm: float | None = None
    spin_axis_degrees: float | None = None


class ClubDeliveryPayload(_Wire):
    """How the club arrived. Entirely optional: many devices measure none of it.

    `face_to_path_degrees` is computed by CaddieJ rather than measured by
    anything, and it is included because it is the number that explains which
    way the ball curved — the one an assistant asked "why did that slice" needs.
    """

    speed_mph: float | None = None
    angle_of_attack_degrees: float | None = None
    path_degrees: float | None = None
    face_to_target_degrees: float | None = None
    face_to_path_degrees: float | None = None
    loft_degrees: float | None = None
    impact_horizontal_mm: float | None = None
    impact_vertical_mm: float | None = None


class ShotPayload(_Wire):
    """One shot, as an assistant sees it.

    Note what is absent, and stays absent. There is no device identifier, no
    device session, no device sequence number and no raw payload. Those are
    diagnostics for somebody holding the launch monitor, they are how a
    debugging capture would leak off the machine, and no conversation about golf
    needs them.
    """

    shot_id: str
    ordinal: int
    """Which shot of the session this was, counting from one."""

    observed_at: datetime
    """When the launch monitor saw it, in UTC."""

    club: str | None = None
    """A `ClubKind` value — `\"iron\"`, `\"driver\"` — or null if the simulator
    had not said what was in play. Never a vendor's own code for a club."""

    handedness: str
    ball: BallPayload | None = None
    club_delivery: ClubDeliveryPayload | None = None


class LastShotResult(_Wire):
    """The answer to `get_last_shot`."""

    availability: Availability
    shot: ShotPayload | None = None


# --- session statistics -------------------------------------------------


class MetricPayload(_Wire):
    """One measurement across a set of shots.

    `count` is how many shots carried this measurement, which is not how many
    shots were hit. A device that reports carry for drives and not for putts
    would otherwise look like it had hit some very soft drives.
    """

    name: str
    unit: str
    count: int
    mean: float
    median: float
    minimum: float
    maximum: float
    standard_deviation: float | None = None


class ClubStatsPayload(_Wire):
    """Everything measured for one club, busiest club first in the parent list."""

    club: str
    shots: int
    metrics: list[MetricPayload]


class SessionStatsResult(_Wire):
    """The answer to `get_session_stats`.

    Deliberately not the whole of CaddieJ's own session summary. The dispersion
    samples are left out: they are one row per shot, they exist so that
    something which knows the shape of a surface can draw a picture, and pouring
    four hundred of them into a language model's context window would be an
    expensive way to answer "how am I hitting it".
    """

    availability: Availability
    session_id: str | None = None
    label: str | None = None
    started_at: datetime | None = None
    shots: int = 0
    first_shot_at: datetime | None = None
    last_shot_at: datetime | None = None
    overall: list[MetricPayload] = Field(default_factory=list)
    by_club: list[ClubStatsPayload] = Field(default_factory=list)
    faults: dict[str, int] = Field(default_factory=dict)
    """How many shots each named fault accounted for. Shots with nothing wrong
    are not counted, so an empty map on a long session means it went well."""


# --- the state of play --------------------------------------------------


class CurrentClubResult(_Wire):
    """The answer to `get_current_club`.

    `EMPTY` here means the simulator has not named a club, which is the normal
    state before a round starts and after a link drops. It is not an error.
    """

    availability: Availability
    club: str | None = None
    ball_on_mat: bool | None = None
    """Null when nothing knows: many launch monitors do not report ball
    presence at all, and a device that says nothing is not a device saying no."""

    launch_monitor_ready: bool | None = None
    handedness: str | None = None


# --- coaching -----------------------------------------------------------


class CoachingLinePayload(_Wire):
    """One thing CaddieJ concluded about one shot.

    `display` and `speech` are what the player actually read and heard, so this
    is a record of the floor rather than of a request. Both are omitted unless
    the prose gate is open; `focus` and `severity` are always present, because a
    named fault is not a transcript.
    """

    shot_id: str
    focus: str
    severity: float
    band: str
    """`fine`, `watch` or `fault` — CaddieJ's own reading of the severity, so an
    assistant does not have to invent thresholds."""

    source: str
    """`template` if CaddieJ wrote the words, `model` if a language model did."""

    created_at: datetime
    display: str | None = None
    speech: str | None = None


class RecentCoachingResult(_Wire):
    """The answer to `get_recent_coaching`, oldest of the recent lines first."""

    availability: Availability
    prose_included: bool = False
    """Whether `display` and `speech` are populated. False means the operator
    has not opened that gate, not that nothing was said."""

    lines: list[CoachingLinePayload] = Field(default_factory=list)


# --- status -------------------------------------------------------------


class SubsystemPayload(_Wire):
    """Whether one part of CaddieJ would do anything if asked.

    Two booleans and no counters. The counters on `/healthz` are telemetry for
    somebody reading a dashboard, they change shape as subsystems grow, and
    `available` is the only one of them an assistant should ever act on.
    """

    enabled: bool
    available: bool


class StatusResult(_Wire):
    """The answer to `get_caddiej_status`.

    The one tool that answers when everything else is degraded, which is its
    job: an assistant that cannot tell "CaddieJ is off" from "CaddieJ has
    nothing to say" is an assistant that guesses.
    """

    availability: Literal[Availability.OK] = Availability.OK
    contract_version: int = CONTRACT_VERSION
    caddiej_version: str
    uptime_seconds: float
    session_id: str | None = None
    """The session in progress, or null when history is switched off or failed
    to open.

    Present from the moment CaddieJ starts, while `get_session_stats` for the
    same session is still `empty`: a session is prepared at start-up and its row
    is written with the first shot, so that a run nobody hit a ball in leaves
    nothing behind. Both answers are correct, and the pair means "a session
    exists and nothing has happened in it yet"."""

    player_name: str | None = None
    """Whoever the operator configured, or null. There is no roster: CaddieJ
    has no player model, and this is a label on a shot row rather than an
    identity."""

    training_focus: str | None = None
    subsystems: dict[str, SubsystemPayload] = Field(default_factory=dict)
    capabilities: list[str] = Field(default_factory=list)
    """Which tools will actually return data right now. An assistant should
    consult this rather than discovering a switched-off subsystem by calling
    into it and reading an `unavailable`."""


# --- the one mutation ---------------------------------------------------


class SetTrainingFocusArguments(_Wire):
    """What `set_training_focus` accepts, and the whole of it.

    One closed enum value or null. Not a free string, so there is no request
    that can carry a path, an expression, or a sentence — and the validation
    error for a misspelling names the legal values, which is more
    useful to an assistant than a rejection is.
    """

    focus: str | None = Field(
        default=None,
        description=(
            "A CaddieJ coaching focus, or null to clear any preference. "
            "Must be one of the values reported by get_caddiej_status."
        ),
    )


class TrainingFocusResult(_Wire):
    """The answer to `set_training_focus`.

    Reports the previous value as well as the new one, because an assistant that
    just changed something on somebody's behalf should be able to say what it
    changed it from.
    """

    availability: Availability
    training_focus: str | None = None
    previous_focus: str | None = None
    changed: bool = False


class ErrorResult(_Wire):
    """A refusal, in a shape an assistant can act on rather than parse.

    `detail` is CaddieJ's own text, never an exception message and never a
    traceback: an error string that quotes internals is both a leak and a
    contract nobody agreed to.
    """

    error: str
    detail: str
