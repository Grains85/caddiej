"""Authoritative render state for the floor surface.

Positions stored here are **calibrated surface coordinates**: x and y run from
0 to 1 across the projected surface, with (0, 0) at the far left of the back of
the surface and y increasing towards the player. They are a render space, not a
physical one. A club-path angle is computed in metres first
(`caddiej.floor.geometry`) and only then mapped onto these coordinates.

Nothing in this module knows the projector's resolution, the OBS canvas size,
or the browser window size. The browser stretches the unit square over whatever
pixels it has. That multiply is the last step, and it cannot change an angle.
"""

import math
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any

from caddiej.domain.coaching import AdviceBand
from caddiej.domain.events import CoachingStage
from caddiej.floor.coach_layout import CoachRegions
from caddiej.floor.geometry import (
    DEFAULT_CALIBRATION,
    SURFACE_CENTRE,
    FloorCalibration,
    project_club_path_end,
)
from caddiej.floor.geometry import (
    FloorPoint as FloorPoint,
)


class FloorMode(StrEnum):
    """Coarse lifecycle state the overlay renders differently."""

    STARTING = "starting"
    ONLINE = "online"
    SHUTTING_DOWN = "shutting_down"


class MarkerVariant(StrEnum):
    """Which club the ball placement is for, which changes how it is drawn."""

    DRIVER = "driver"
    NORMAL = "normal"
    PUTTER = "putter"


@dataclass(frozen=True, slots=True)
class BallMarker:
    """Where to place the ball, and for which club.

    Visibility is a flag rather than the marker being absent, so hiding it —
    which is what happens once the ball is detected — keeps the position and
    lets the overlay animate out from where the marker actually was.
    """

    position: FloorPoint = SURFACE_CENTRE
    variant: MarkerVariant = MarkerVariant.NORMAL
    visible: bool = False

    def __post_init__(self) -> None:
        if not (0.0 <= self.position.x <= 1.0 and 0.0 <= self.position.y <= 1.0):
            raise ValueError(
                "BallMarker.position must be on the surface, within 0..1 "
                f"(got {self.position.x}, {self.position.y})"
            )


@dataclass(frozen=True, slots=True)
class ShotMetrics:
    """The few launch numbers the floor shows after a shot.

    Only the ball speed is required. Carry is optional because most launch
    monitors do not measure it — the simulator computes the flight — and the
    launch angle because a passive reading of what the simulator wrote
    afterwards does not carry one. An omitted number is left off the floor
    rather than shown as zero, which would be a lie a player might believe.
    """

    ball_speed_mph: float
    launch_angle_degrees: float | None = None
    carry_yards: float | None = None

    def __post_init__(self) -> None:
        for name in ("ball_speed_mph", "launch_angle_degrees", "carry_yards"):
            value = getattr(self, name)
            if value is not None and not math.isfinite(value):
                raise ValueError(f"ShotMetrics.{name} must be a finite number")


@dataclass(frozen=True, slots=True)
class ClubPath:
    """A straight club path, authored as a physical ray and stored for drawing.

    `start` is the tee on the calibrated surface — where the operator placed
    it, by eye, until a wizard measures the mat. `angle_degrees` is the
    launch-monitor number: 0 straight down-range, increasing clockwise seen
    from above. `length` is metres of display line, not a fraction of the
    surface and not carry.

    The end point is the physical ray mapped back onto the surface. On the
    default 1 m × 1 m calibration that mapping is the identity, so a fresh
    checkout keeps the endpoints it already had. The browser draws the two
    points and does no trigonometry, because a renderer that derives geometry
    is a renderer that can disagree with Python.
    """

    start: FloorPoint
    angle_degrees: float
    length: float
    end: FloorPoint

    def __init__(
        self,
        start: FloorPoint,
        angle_degrees: float,
        length: float,
        end: FloorPoint | None = None,
        *,
        calibration: FloorCalibration | None = None,
    ) -> None:
        for name, value in (("angle_degrees", angle_degrees), ("length", length)):
            if not math.isfinite(value):
                raise ValueError(f"ClubPath.{name} must be a finite number")
        if length < 0.0:
            raise ValueError("ClubPath.length must not be negative")
        if end is None:
            mapping = DEFAULT_CALIBRATION if calibration is None else calibration
            end = project_club_path_end(start, angle_degrees, length, mapping)
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "angle_degrees", angle_degrees)
        object.__setattr__(self, "length", length)
        object.__setattr__(self, "end", end)


@dataclass(frozen=True, slots=True)
class FloorEvidence:
    """The player's own still that a coaching line points at.

    Not `cue_asset`. That is artwork somebody drew to teach a move; this is a
    frame of this golfer's swing, and the two must never be able to stand in
    for each other. Held as the capture and the frame id rather than as a URL
    or a path, so the only route to a file is the exact lookup the HUD makes
    when it is asked for one.
    """

    capture_id: str
    frame_id: str

    def __post_init__(self) -> None:
        if not self.capture_id or not self.frame_id:
            raise ValueError("FloorEvidence needs a capture and a frame id")


@dataclass(frozen=True, slots=True)
class FloorMetric:
    """One of the four readings shown under a coached result.

    Already formatted, including its unit and the launch monitor's own
    direction word, because the floor is the last place a number should be
    converted. See `caddiej.coaching.metrics_strip` for why.
    """

    label: str
    value: str

    def __post_init__(self) -> None:
        if not self.label or not self.value:
            raise ValueError("FloorMetric needs a label and a value")


@dataclass(frozen=True, slots=True)
class FloorPhase:
    """One scored phase of the swing: `TRANSITION 54`."""

    label: str
    value: int

    def __post_init__(self) -> None:
        if not self.label or not 0 <= self.value <= 100:
            raise ValueError("FloorPhase needs a label and a value in 0..100")


@dataclass(frozen=True, slots=True)
class FloorLesson:
    """The session lesson's progress: `RELEASE 81 +38 MUCH BETTER RELEASE`."""

    label: str
    value: int | None = None
    delta: int | None = None
    headline: str = ""
    verdict: str = ""
    mode: str = ""


@dataclass(frozen=True, slots=True)
class FloorAdvice:
    """One short coaching line, and how strongly to draw it.

    Text arrives here having been validated and sanitized at the boundary that
    produced it, because some of it is authored by a language model and all of
    it ends up in a browser. The band rather than the severity, so that a
    renderer never has to know what 0.42 means.

    `pending` says a model is being asked. It is what the overlay uses to show
    that something is being considered, so that a line appearing four seconds
    after the shot reads as thought rather than as a glitch.

    `text` is the action. `observation` is what was seen, `swing_score` the
    number, `evidence` the frame, `metrics` the four readings — the parts a
    coached result is made of, each absent rather than invented when this
    swing did not produce it. A score with no line is allowed, because a swing
    can be scored and still have nothing worth saying about it; a line with no
    score is what every GSPro shot is.
    """

    text: str
    band: AdviceBand = AdviceBand.WATCH
    pending: bool = False
    cue_asset: str | None = None
    """Optional instructional artwork id. None until artwork exists."""

    observation: str = ""
    swing_score: int | None = None
    evidence: FloorEvidence | None = None
    metrics: tuple[FloorMetric, ...] = ()
    """Carry, face, path and club speed, as the device wrote them. Whatever
    was readable, in strip order, and quite possibly none of them."""

    phases: tuple[FloorPhase, ...] = ()
    """The scored phases, in swing order. Data only: no layout draws them yet."""

    lesson: FloorLesson | None = None
    """The session lesson's progress. Data only, like `phases`."""

    def __post_init__(self) -> None:
        if (
            not self.text
            and not self.pending
            and self.swing_score is None
            and not self.metrics
        ):
            raise ValueError("FloorAdvice needs text unless it is pending or scored")
        if self.swing_score is not None and not 0 <= self.swing_score <= 100:
            raise ValueError("FloorAdvice.swing_score must lie between 0 and 100")


@dataclass(frozen=True, slots=True)
class FloorProgress:
    """Which part of coaching the current swing is in, for as long as it is.

    Held rather than signalled, on the same reading as the flourish and the
    lock acknowledgement: this is something the floor is doing for twenty
    seconds, Python decides when it starts and stops, and a Browser Source
    that reconnects in the middle of an analysis has to find one in progress
    rather than a floor that looks idle while a model is still being asked.

    A stage and the moment it began, and nothing that could be read as a
    fraction. `started_at` is wall clock so a client can say how long it has
    been, never how long is left.
    """

    stage: CoachingStage
    started_at: datetime


@dataclass(frozen=True, slots=True)
class FloorFlourish:
    """A short celebration drawn over the middle of the surface.

    Held as state with a duration rather than delivered as a one-shot signal,
    and that is a deliberate reading of the rule that a momentary flash may not
    be modelled as transient state. The rule exists because a client that
    reconnects across a flash misses it with nothing to resend. This is not a
    flash: it is something the floor is doing for several seconds, Python decides
    when it starts and when it stops, and a client that joins in the middle
    correctly finds a celebration in progress. The snapshot feed answers that
    question exactly right.

    `seconds` travels so the browser can pace its own animation to the same
    length the director is holding the state for. Presentation follows the
    simulator's clock rather than keeping one of its own, which is what stops the
    label from fading out a beat before or after the lights do.

    `intensity` is the same dial the effect carried, passed through rather than
    resolved into pixels here: how large is a rendering decision, and this module
    knows nothing about how large anything is on a projector.
    """

    cue: str
    label: str
    intensity: float = 1.0
    seconds: float = 2.0

    def __post_init__(self) -> None:
        if not self.cue:
            raise ValueError("FloorFlourish needs a cue name")
        if not self.label:
            raise ValueError("FloorFlourish needs a label")
        if not 0.0 <= self.intensity <= 1.0:
            raise ValueError("FloorFlourish.intensity must lie between zero and one")
        if not math.isfinite(self.seconds) or self.seconds <= 0.0:
            raise ValueError("FloorFlourish.seconds must be a positive finite number")


@dataclass(frozen=True, slots=True)
class FlightDrawPoint:
    """A point in the diagnostic ball-flight chart (0..1 of that panel).

    Not a calibrated floor coordinate and not a physical metre. The overlay
    draws these inside a labelled BALL FLIGHT box; FloorCalibration never
    sees them.
    """

    x: float
    y: float

    def __post_init__(self) -> None:
        for name in ("x", "y"):
            value = getattr(self, name)
            if not math.isfinite(value):
                raise ValueError(f"FlightDrawPoint.{name} must be a finite number")


@dataclass(frozen=True, slots=True)
class HittingStrip:
    """The physical hitting area, as four calibrated surface corners.

    Corners are already mapped through FloorCalibration. They are not CSS
    pixels. Visibility is a flag so hiding the strip (ball on the mat) keeps
    the geometry for the overlay to fade out from.

    `filled` asks for the whole calibrated area to be lit rather than outlined,
    which is what turns the projector into a work light over the turf in a dark
    bay. It is an operator's choice about their room, so it travels as state
    rather than being decided in a stylesheet.
    """

    corners: tuple[FloorPoint, FloorPoint, FloorPoint, FloorPoint]
    visible: bool = False
    filled: bool = False


@dataclass(frozen=True, slots=True)
class BallStatus:
    """The compact chamfer on the hitting strip's rear edge.

    The edge is the strip's own rear line, stepped out behind the mat. It is
    not a panel. `label` is the only words, and `ready` is whether the lamp is
    lit. Sizes are the Compact design in millimetres; `millimetre` is one
    millimetre toward the player from the text, already mapped, so the browser
    can turn those millimetres into pixels without knowing the bay.
    """

    label: str
    ready: bool
    edge: tuple[FloorPoint, ...]
    text: FloorPoint
    millimetre: FloorPoint
    font_mm: float
    stroke_mm: float
    lamp_mm: float
    lamp_gap_mm: float
    tracking: float
    weight: int

    def __post_init__(self) -> None:
        if self.label not in {"BALL READY", "PLACE BALL"}:
            raise ValueError("BallStatus.label must be BALL READY or PLACE BALL")
        if len(self.edge) != 6:
            raise ValueError("BallStatus.edge is the rear edge in six points")
        for name in ("font_mm", "stroke_mm", "lamp_mm", "lamp_gap_mm", "tracking"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"BallStatus.{name} must be a positive finite number")
        if self.weight <= 0:
            raise ValueError("BallStatus.weight must be positive")


@dataclass(frozen=True, slots=True)
class LockAcknowledgement:
    """A brief confirmation that the ball has been found.

    Held as state with a duration rather than a one-shot flash, so a client
    that reconnects mid-acknowledgement still sees it. The hitting strip stays
    lit underneath: this is a variation on that light, not a replacement for it.
    `seconds` travels so the overlay can pace the pulse to the same length
    Python holds the state for.

    It is not a caption. The ball status on the rear edge is a different mark;
    this is one pulse, then the pad goes back to being a work light.
    """

    seconds: float = 0.65

    def __post_init__(self) -> None:
        if not math.isfinite(self.seconds) or self.seconds <= 0.0:
            raise ValueError(
                "LockAcknowledgement.seconds must be a positive finite number"
            )


@dataclass(frozen=True, slots=True)
class PuttingLane:
    """A simple guide running down-range from the putter ball position.

    Corners and centre line are already mapped through FloorCalibration, so a
    homography turns the metre-aligned rectangle into the quadrilateral it
    actually occupies and the browser still only joins points. Two shapes
    rather than one because a lane that is only an outline is hard to aim along
    and a lane that is only a line is hard to see: the rectangle says how wide,
    the centre line says where.

    Visibility is a flag for the same reason the strip's is — the geometry is
    worth keeping across a hide, so the overlay can fade out from where the
    lane was.
    """

    corners: tuple[FloorPoint, FloorPoint, FloorPoint, FloorPoint]
    start: FloorPoint
    end: FloorPoint
    visible: bool = False


@dataclass(frozen=True, slots=True)
class PuttPath:
    """Where a putt actually rolled, drawn from the putter ball position.

    Calibrated surface points, in order. `source` names what produced them so
    nothing downstream has to guess how much of a measurement this is, and so a
    later source — a putting-specific launch monitor, a GSPro build that writes
    a clean trajectory — can be told apart from this one in a log.

    Two points are a line, not a path, and a path of one point is nothing, so
    three is the minimum. The rule is here rather than in the caller because
    "this is a shape a player can read" is a property of the shape.
    """

    points: tuple[FloorPoint, ...]
    source: str = "gspro_round_path"

    def __post_init__(self) -> None:
        if len(self.points) < 3:
            raise ValueError("PuttPath needs at least three points to be a path")
        if not self.source:
            raise ValueError("PuttPath needs to say what produced it")


@dataclass(frozen=True, slots=True)
class FloorAnchors:
    """Where anchored overlay elements sit, in calibrated surface coordinates.

    Computed in metres from the hitting strip and mapped here, so moving the
    mat moves the numbers with it. The overlay positions its panels at these
    points and derives nothing: a renderer that could work out where a panel
    belongs is a renderer that can disagree with Python about where the mat is.

    Not everything on the floor is here. The startup banner, the celebration
    layer and the link indicator are drawn across the whole surface and have
    nothing to do with a mat.
    """

    information: FloorPoint
    advice: FloorPoint
    coach: CoachRegions | None = None
    """Where the four parts of a coached result go, and the band they may not
    enter. Absent on a floor whose mat leaves no room above it, and the
    overlay then falls back to the single centred column it has always had.

    A region rather than another point, because a point plus a stylesheet is
    what let the score grow down over the hitting strip: how tall the result
    is was decided in CSS, and CSS could not know where the mat was."""


@dataclass(frozen=True, slots=True)
class RoundResultView:
    """GSPro round-play numbers for the floor's result panel.

    `*_raw` are the values currentRound.dat stored, under the names it used.
    Display strings are HUD formatting only (integer km/h and metres) and must
    not be treated as a change to the domain schema. `hole` is the value GSPro
    stored; it is not adjusted. Surface integers stay unnamed.

    The two display names that are not a source field name — `shot_distance` and
    `remaining_distance` — say what the player is being shown, and each is only
    named that way because the meaning of the number behind it was verified on a
    real round: `TotalDistance` is how far this shot went, `DistanceToPin` is what
    is left after it. `remaining_distance_display` is None when GSPro gave no
    usable value, and the panel then shows one figure rather than a dash.
    `ball_speed_display` is kept for snapshot and logging; the overlay does not
    draw it.
    """

    shot_id: str
    player_name: str = ""
    hole: int | None = None
    hole_shot: int | None = None
    ball_speed_raw: float | None = None
    total_distance_raw: float | None = None
    distance_to_pin_raw: float | None = None
    ball_speed_display: str | None = None
    shot_distance_display: str | None = None
    remaining_distance_display: str | None = None
    starting_surface_raw: int | None = None
    ending_surface_raw: int | None = None


@dataclass(frozen=True, slots=True)
class AddressView:
    """What is worth knowing before the next shot: how far, and with what.

    Display strings only. Raw DistanceToTarget stays in PlayFlow and the
    recommendation is made in `caddiej.domain.bag`; the browser receives neither
    a float nor a rule, because a renderer that can do golf arithmetic is a
    renderer that can disagree with Python about a club.

    Every field is independently optional. A bay with a target distance and no
    measured bag shows one figure; a bay with neither shows no panel at all.
    `club_display` is the club the launch monitor detected, which is a different
    claim from the one CaddieJ is making, and travels for diagnostics rather
    than as a second headline.
    """

    club_display: str = ""
    distance_display: str | None = None
    recommended_club_display: str = ""


@dataclass(frozen=True, slots=True)
class FloorState:
    """An immutable snapshot of everything the floor surface should render.

    `version` doubles as the feed sequence number, so a client can tell whether
    a snapshot it holds is older than one it has already seen.

    Metrics, the club path and the coaching line are absent until something
    produces them, while the marker always exists and carries a visibility flag,
    because it has a position worth keeping across a hide.

    The flourish is a layer rather than a mode: everything else stays exactly as
    it was underneath it. That is what makes an effect's reset clean — there is
    one field to clear, and clearing it cannot disturb what the round had put on
    the floor.
    """

    version: int = 0
    mode: FloorMode = FloorMode.STARTING
    status_text: str = ""
    marker: BallMarker = field(default_factory=BallMarker)
    metrics: ShotMetrics | None = None
    club_path: ClubPath | None = None
    advice: FloorAdvice | None = None
    flourish: FloorFlourish | None = None
    play_status: str = ""
    club_label: str = ""
    ball_status: BallStatus | None = None
    round_result: RoundResultView | None = None
    ball_flight: tuple[FlightDrawPoint, ...] | None = None
    hitting_strip: HittingStrip | None = None
    address: AddressView | None = None
    lock_acknowledgement: LockAcknowledgement | None = None
    # Where the anchored panels go. Absent until a presenter has said, because
    # a fresh `FloorState()` has no room to be anchored to; the overlay then
    # falls back to the placement it has always had.
    anchors: FloorAnchors | None = None
    putting_lane: PuttingLane | None = None
    putt_path: PuttPath | None = None
    # A layer of its own, like the flourish, so clearing it cannot take the
    # line, the result or the ball status with it.
    progress: FloorProgress | None = None

    def evolve(self, **changes: Any) -> "FloorState":
        """Return the next version of this state with `changes` applied."""

        return replace(self, version=self.version + 1, **changes)
