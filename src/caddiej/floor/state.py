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
from enum import StrEnum
from typing import Any

from caddiej.domain.coaching import AdviceBand
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

    Carry is optional because most launch monitors do not measure it: the
    simulator computes the flight. An omitted number is left off the floor
    rather than shown as zero, which would be a lie a player might believe.
    """

    ball_speed_mph: float
    launch_angle_degrees: float
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
class FloorAdvice:
    """One short coaching line, and how strongly to draw it.

    Text arrives here having been validated and sanitized at the boundary that
    produced it, because some of it is authored by a language model and all of
    it ends up in a browser. The band rather than the severity, so that a
    renderer never has to know what 0.42 means.

    `pending` says a model is being asked. It is what the overlay uses to show
    that something is being considered, so that a line appearing four seconds
    after the shot reads as thought rather than as a glitch.
    """

    text: str
    band: AdviceBand = AdviceBand.WATCH
    pending: bool = False

    def __post_init__(self) -> None:
        if not self.text and not self.pending:
            raise ValueError("FloorAdvice needs text unless it is pending")


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
class RoundResultView:
    """Raw GSPro round-play numbers for the diagnostic overlay.

    No unit suffixes. `hole` is the value GSPro stored; it is not adjusted.
    """

    shot_id: str
    player_name: str = ""
    hole: int | None = None
    hole_shot: int | None = None
    ball_speed_raw: float | None = None
    total_distance_raw: float | None = None
    starting_surface_raw: int | None = None
    ending_surface_raw: int | None = None


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
    round_result: RoundResultView | None = None
    ball_flight: tuple[FlightDrawPoint, ...] | None = None

    def evolve(self, **changes: Any) -> "FloorState":
        """Return the next version of this state with `changes` applied."""

        return replace(self, version=self.version + 1, **changes)
