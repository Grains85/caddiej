"""Wire format for the floor state feed.

The browser is a rendering client: every message below flows server to client
except the small client control set at the bottom. Nothing here lets a client
assert state.
"""

from datetime import UTC, datetime
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from caddiej.floor.coach_layout import CoachRegions, FloorRect
from caddiej.floor.format import (
    format_ball_speed_kmh,
    format_remaining_distance_m,
    format_total_distance_m,
)
from caddiej.floor.state import (
    AddressView,
    BallMarker,
    BallStatus,
    ClubPath,
    FlightDrawPoint,
    FloorAdvice,
    FloorAnchors,
    FloorEvidence,
    FloorFlourish,
    FloorPoint,
    FloorProgress,
    FloorState,
    HittingStrip,
    LockAcknowledgement,
    PuttingLane,
    PuttPath,
    RoundResultView,
    ShotMetrics,
)

PROTOCOL_VERSION = 1

EVIDENCE_ROUTE = "/evidence"


def evidence_url(evidence: FloorEvidence) -> str:
    """Where the HUD serves this frame. Both halves are quoted, never trusted.

    The ids came through a language model before they got here, and a value
    that could carry a slash or a dot-dot is a value that could name a
    different route. Quoting with nothing marked safe keeps each one a single
    path segment; the route then looks the frame up exactly or refuses.
    """

    return (
        f"{EVIDENCE_ROUTE}/{quote(evidence.capture_id, safe='')}"
        f"/{quote(evidence.frame_id, safe='')}"
    )


class FloorPointPayload(BaseModel):
    """A point in calibrated surface coordinates (0..1).

    These are render coordinates, not metres. Golf geometry has already been
    done: the browser draws the point it is given.
    """

    x: float
    y: float

    @classmethod
    def from_point(cls, point: FloorPoint) -> "FloorPointPayload":
        return cls(x=point.x, y=point.y)


class BallMarkerPayload(BaseModel):
    position: FloorPointPayload
    variant: str
    visible: bool

    @classmethod
    def from_marker(cls, marker: BallMarker) -> "BallMarkerPayload":
        return cls(
            position=FloorPointPayload.from_point(marker.position),
            variant=marker.variant.value,
            visible=marker.visible,
        )


class ShotMetricsPayload(BaseModel):
    ball_speed_mph: float
    # Null when whatever reported the shot did not measure it. The overlay
    # leaves the row out rather than rendering a zero.
    launch_angle_degrees: float | None = None
    carry_yards: float | None = None

    @classmethod
    def from_metrics(cls, metrics: ShotMetrics) -> "ShotMetricsPayload":
        return cls(
            ball_speed_mph=metrics.ball_speed_mph,
            launch_angle_degrees=metrics.launch_angle_degrees,
            carry_yards=metrics.carry_yards,
        )


class ClubPathPayload(BaseModel):
    """A club path as both endpoints plus the ray that produced them.

    `start` and `end` are calibrated surface points. `angle_degrees` is the
    physical launch-monitor angle and `length` is metres of display line —
    documentary, so a snapshot is readable on a projector that has no console.
    The renderer draws the two points and never does the trigonometry itself.
    """

    start: FloorPointPayload
    end: FloorPointPayload
    angle_degrees: float
    length: float

    @classmethod
    def from_club_path(cls, club_path: ClubPath) -> "ClubPathPayload":
        return cls(
            start=FloorPointPayload.from_point(club_path.start),
            end=FloorPointPayload.from_point(club_path.end),
            angle_degrees=club_path.angle_degrees,
            length=club_path.length,
        )


class MetricPayload(BaseModel):
    """One reading in the strip under a coached result.

    A label and an already-formatted value, both strings. No number crosses
    this boundary, because a renderer holding a float would eventually round
    it differently from the launch monitor that produced it.
    """

    label: str
    value: str


class PhasePayload(BaseModel):
    """One scored phase of the swing, `TRANSITION 54`. Data only for now."""

    label: str
    value: int


class LessonPayload(BaseModel):
    """The session lesson's progress, `RELEASE 81 +38 MUCH BETTER RELEASE`."""

    label: str
    value: int | None = None
    delta: int | None = None
    headline: str = ""
    verdict: str = ""
    mode: str = ""


class AdvicePayload(BaseModel):
    """A coaching line, as the overlay receives it.

    Some of this text was written by a language model, so it crosses this
    boundary as data and is assigned to the DOM as text rather than as markup.
    The band travels instead of the severity: a renderer picks a colour, it does
    not judge a swing.

    `evidence_url` is a URL on this server and nothing else — never a file
    path, never a data URI — and it is absent rather than guessed when the line
    cites no frame. The browser hides the image when the server answers 404,
    which is what a pruned capture looks like, so a missing frame is a blank
    rather than somebody else's swing. `cue_asset` remains instructional
    artwork and is a different thing entirely.
    """

    text: str
    band: str
    pending: bool
    cue_asset: str | None = None
    observation: str = ""
    swing_score: int | None = None
    evidence_url: str | None = None
    evidence_frame_id: str | None = None
    metrics: list[MetricPayload] = Field(default_factory=list)
    phases: list[PhasePayload] = Field(default_factory=list)
    lesson: LessonPayload | None = None

    @classmethod
    def from_advice(cls, advice: FloorAdvice) -> "AdvicePayload":
        evidence = advice.evidence
        lesson = advice.lesson
        return cls(
            text=advice.text,
            band=advice.band.value,
            pending=advice.pending,
            cue_asset=advice.cue_asset,
            observation=advice.observation,
            swing_score=advice.swing_score,
            evidence_url=None if evidence is None else evidence_url(evidence),
            evidence_frame_id=None if evidence is None else evidence.frame_id,
            metrics=[
                MetricPayload(label=metric.label, value=metric.value)
                for metric in advice.metrics
            ],
            phases=[
                PhasePayload(label=phase.label, value=phase.value)
                for phase in advice.phases
            ],
            lesson=(
                None
                if lesson is None
                else LessonPayload(
                    label=lesson.label,
                    value=lesson.value,
                    delta=lesson.delta,
                    headline=lesson.headline,
                    verdict=lesson.verdict,
                    mode=lesson.mode,
                )
            ),
        )


class ProgressPayload(BaseModel):
    """Which coaching stage is running, as the overlay receives it.

    A stage word and when it began. There is no percentage on the wire because
    there is none anywhere: the renderer draws an indeterminate motion keyed on
    the stage, and a client that wants to say how long it has been can use
    `started_at`, which is all anyone actually knows.
    """

    stage: str
    started_at: str

    @classmethod
    def from_progress(cls, progress: FloorProgress) -> "ProgressPayload":
        return cls(
            stage=progress.stage.value, started_at=progress.started_at.isoformat()
        )


class FlourishPayload(BaseModel):
    """A celebration in progress, as the overlay receives it.

    `seconds` is on the wire so the animation runs for as long as the effect is
    being held rather than for a length the stylesheet guessed. `cue` is the
    styling hook and is a fixed vocabulary from the catalogue, never free text —
    the renderer keys colour off it, so a value nobody chose deliberately would
    silently fall back to a default.
    """

    cue: str
    label: str
    intensity: float
    seconds: float

    @classmethod
    def from_flourish(cls, flourish: FloorFlourish) -> "FlourishPayload":
        return cls(
            cue=flourish.cue,
            label=flourish.label,
            intensity=flourish.intensity,
            seconds=flourish.seconds,
        )


class HittingStripPayload(BaseModel):
    """Hitting-strip corners in calibrated surface coordinates (0..1)."""

    corners: list[FloorPointPayload]
    visible: bool
    filled: bool = False

    @classmethod
    def from_strip(cls, strip: HittingStrip) -> "HittingStripPayload":
        return cls(
            corners=[FloorPointPayload.from_point(point) for point in strip.corners],
            visible=strip.visible,
            filled=strip.filled,
        )


class BallStatusPayload(BaseModel):
    """The compact chamfer, as the overlay receives it.

    `edge` is the rear edge of the strip, already stepped. `millimetre` is one
    millimetre toward the player from the text. The millimetre sizes are the
    Compact drawing; the browser multiplies them by the pixel length of
    `millimetre` and does no other geometry.
    """

    label: str
    ready: bool
    edge: list[FloorPointPayload]
    text: FloorPointPayload
    millimetre: FloorPointPayload
    font_mm: float
    stroke_mm: float
    lamp_mm: float
    lamp_gap_mm: float
    tracking: float
    weight: int

    @classmethod
    def from_status(cls, status: BallStatus) -> "BallStatusPayload":
        return cls(
            label=status.label,
            ready=status.ready,
            edge=[FloorPointPayload.from_point(point) for point in status.edge],
            text=FloorPointPayload.from_point(status.text),
            millimetre=FloorPointPayload.from_point(status.millimetre),
            font_mm=status.font_mm,
            stroke_mm=status.stroke_mm,
            lamp_mm=status.lamp_mm,
            lamp_gap_mm=status.lamp_gap_mm,
            tracking=status.tracking,
            weight=status.weight,
        )


class LockAcknowledgementPayload(BaseModel):
    """A ball-lock pulse in progress, as the overlay receives it.

    `seconds` is on the wire so the animation runs for as long as Python holds
    the state. The renderer does not decide when the ball was found, and it
    does not invent a second pulse from a snapshot that is merely still set.
    """

    seconds: float

    @classmethod
    def from_acknowledgement(
        cls, acknowledgement: LockAcknowledgement
    ) -> "LockAcknowledgementPayload":
        return cls(seconds=acknowledgement.seconds)


class PuttingLanePayload(BaseModel):
    """The putting guide in calibrated surface coordinates (0..1)."""

    corners: list[FloorPointPayload]
    start: FloorPointPayload
    end: FloorPointPayload
    visible: bool

    @classmethod
    def from_lane(cls, lane: PuttingLane) -> "PuttingLanePayload":
        return cls(
            corners=[FloorPointPayload.from_point(point) for point in lane.corners],
            start=FloorPointPayload.from_point(lane.start),
            end=FloorPointPayload.from_point(lane.end),
            visible=lane.visible,
        )


class PuttPathPayload(BaseModel):
    """A measured putt roll, as calibrated surface points in order.

    ``source`` travels so the overlay, a snapshot and a log all say what made
    these points. It is not styling: a floor that could draw a derived line and
    a measured one identically is a floor that can claim a measurement it does
    not have.
    """

    points: list[FloorPointPayload]
    source: str

    @classmethod
    def from_path(cls, path: PuttPath) -> "PuttPathPayload":
        return cls(
            points=[FloorPointPayload.from_point(point) for point in path.points],
            source=path.source,
        )


class FloorRectPayload(BaseModel):
    """A box in calibrated surface coordinates. Top-left, width, height."""

    x: float
    y: float
    width: float
    height: float

    @classmethod
    def from_rect(cls, rect: FloorRect) -> "FloorRectPayload":
        return cls(x=rect.x, y=rect.y, width=rect.width, height=rect.height)


class CoachRegionsPayload(BaseModel):
    """Where each part of a coached result goes, as the overlay receives it.

    Boxes rather than a single anchor, and the protected band travels with
    them. The overlay does not have to honour `protected` to look right — the
    four drawn regions already clear it — but it is on the wire because a
    snapshot that shows coaching over the mat should make it obvious from the
    data which of the two was wrong.
    """

    score: FloorRectPayload
    evidence: FloorRectPayload
    observation: FloorRectPayload
    metrics: FloorRectPayload
    protected: FloorRectPayload

    @classmethod
    def from_regions(cls, regions: CoachRegions) -> "CoachRegionsPayload":
        return cls(
            score=FloorRectPayload.from_rect(regions.score),
            evidence=FloorRectPayload.from_rect(regions.evidence),
            observation=FloorRectPayload.from_rect(regions.observation),
            metrics=FloorRectPayload.from_rect(regions.metrics),
            protected=FloorRectPayload.from_rect(regions.protected),
        )


class AnchorsPayload(BaseModel):
    """Where the anchored panels go, in calibrated surface coordinates.

    Python derives these from the hitting strip in metres. The overlay places
    the panels at them and computes nothing, which is what keeps moving a mat
    from needing a stylesheet edit.
    """

    information: FloorPointPayload
    advice: FloorPointPayload
    coach: CoachRegionsPayload | None = None

    @classmethod
    def from_anchors(cls, anchors: FloorAnchors) -> "AnchorsPayload":
        return cls(
            information=FloorPointPayload.from_point(anchors.information),
            advice=FloorPointPayload.from_point(anchors.advice),
            coach=(
                None
                if anchors.coach is None
                else CoachRegionsPayload.from_regions(anchors.coach)
            ),
        )


class AddressPayload(BaseModel):
    """Pre-shot numbers as the overlay receives them. Display strings only.

    ``recommended_club_display`` is empty rather than absent when CaddieJ has no
    answer, which the overlay renders as no figure at all. A club is never sent
    without a target distance having produced it.
    """

    club_display: str = ""
    distance_display: str | None = None
    recommended_club_display: str = ""

    @classmethod
    def from_view(cls, address: AddressView) -> "AddressPayload":
        return cls(
            club_display=address.club_display,
            distance_display=address.distance_display,
            recommended_club_display=address.recommended_club_display,
        )


class RoundResultPayload(BaseModel):
    """GSPro round-play numbers as the overlay receives them.

    Display strings are always produced here from the raw domain values.
    Raw floats stay on ``RoundResultView`` and never cross this boundary, so a
    stale cached renderer cannot ``String()`` them onto the floor.

    ``remaining_distance_display`` is absent rather than empty when GSPro gave no
    usable distance to the pin, because the overlay hides that figure entirely in
    that case: a metric the round did not supply is not shown as a dash.
    """

    shot_id: str
    player_name: str = ""
    hole: int | None = None
    hole_shot: int | None = None
    ball_speed_display: str | None = None
    shot_distance_display: str | None = None
    remaining_distance_display: str | None = None
    starting_surface_raw: int | None = None
    ending_surface_raw: int | None = None

    @classmethod
    def from_result(cls, result: RoundResultView) -> "RoundResultPayload":
        return cls(
            shot_id=result.shot_id,
            player_name=result.player_name,
            hole=result.hole,
            hole_shot=result.hole_shot,
            ball_speed_display=format_ball_speed_kmh(result.ball_speed_raw),
            shot_distance_display=format_total_distance_m(result.total_distance_raw),
            remaining_distance_display=format_remaining_distance_m(
                result.distance_to_pin_raw
            ),
            starting_surface_raw=result.starting_surface_raw,
            ending_surface_raw=result.ending_surface_raw,
        )


class FlightDrawPointPayload(BaseModel):
    """A point in the diagnostic ball-flight chart (0..1 of that panel)."""

    x: float
    y: float

    @classmethod
    def from_point(cls, point: FlightDrawPoint) -> "FlightDrawPointPayload":
        return cls(x=point.x, y=point.y)


class FloorStatePayload(BaseModel):
    """Serialised floor state as the overlay receives it.

    Round-result raw floats stay on ``RoundResultView``; the wire carries
    display strings for those two numbers. Everything else mirrors ``FloorState``.
    """

    version: int
    mode: str
    status_text: str
    marker: BallMarkerPayload
    metrics: ShotMetricsPayload | None
    club_path: ClubPathPayload | None
    advice: AdvicePayload | None = None
    flourish: FlourishPayload | None = None
    play_status: str = ""
    club_label: str = ""
    ball_status: BallStatusPayload | None = None
    round_result: RoundResultPayload | None = None
    ball_flight: list[FlightDrawPointPayload] | None = None
    hitting_strip: HittingStripPayload | None = None
    address: AddressPayload | None = None
    lock_acknowledgement: LockAcknowledgementPayload | None = None
    anchors: AnchorsPayload | None = None
    putting_lane: PuttingLanePayload | None = None
    putt_path: PuttPathPayload | None = None
    progress: ProgressPayload | None = None

    @classmethod
    def from_state(cls, state: FloorState) -> "FloorStatePayload":
        return cls(
            version=state.version,
            mode=state.mode.value,
            status_text=state.status_text,
            marker=BallMarkerPayload.from_marker(state.marker),
            metrics=(
                None
                if state.metrics is None
                else ShotMetricsPayload.from_metrics(state.metrics)
            ),
            club_path=(
                None
                if state.club_path is None
                else ClubPathPayload.from_club_path(state.club_path)
            ),
            advice=(
                None
                if state.advice is None
                else AdvicePayload.from_advice(state.advice)
            ),
            flourish=(
                None
                if state.flourish is None
                else FlourishPayload.from_flourish(state.flourish)
            ),
            play_status=state.play_status,
            club_label=state.club_label,
            ball_status=(
                None
                if state.ball_status is None
                else BallStatusPayload.from_status(state.ball_status)
            ),
            round_result=(
                None
                if state.round_result is None
                else RoundResultPayload.from_result(state.round_result)
            ),
            ball_flight=(
                None
                if state.ball_flight is None
                else [FlightDrawPointPayload.from_point(p) for p in state.ball_flight]
            ),
            hitting_strip=(
                None
                if state.hitting_strip is None
                else HittingStripPayload.from_strip(state.hitting_strip)
            ),
            address=(
                None
                if state.address is None
                else AddressPayload.from_view(state.address)
            ),
            lock_acknowledgement=(
                None
                if state.lock_acknowledgement is None
                else LockAcknowledgementPayload.from_acknowledgement(
                    state.lock_acknowledgement
                )
            ),
            anchors=(
                None
                if state.anchors is None
                else AnchorsPayload.from_anchors(state.anchors)
            ),
            putting_lane=(
                None
                if state.putting_lane is None
                else PuttingLanePayload.from_lane(state.putting_lane)
            ),
            putt_path=(
                None
                if state.putt_path is None
                else PuttPathPayload.from_path(state.putt_path)
            ),
            progress=(
                None
                if state.progress is None
                else ProgressPayload.from_progress(state.progress)
            ),
        )


class HelloMessage(BaseModel):
    v: int = PROTOCOL_VERSION
    type: Literal["hello"] = "hello"
    protocol_version: int = PROTOCOL_VERSION
    server_time: str
    # Content hash of the overlay JS/CSS this process is serving. The browser
    # compares it to the hash baked into the module it actually loaded, so a
    # stale OBS CEF cache is a log line rather than a silent dash on the floor.
    hud_assets_version: str = ""


class SnapshotMessage(BaseModel):
    v: int = PROTOCOL_VERSION
    type: Literal["snapshot"] = "snapshot"
    seq: int
    state: FloorStatePayload


class PongMessage(BaseModel):
    v: int = PROTOCOL_VERSION
    type: Literal["pong"] = "pong"


class ErrorMessage(BaseModel):
    v: int = PROTOCOL_VERSION
    type: Literal["error"] = "error"
    detail: str


class ReloadMessage(BaseModel):
    """Load the overlay this server is serving, at exactly this address.

    Sent only to a page that has just said it is running a different build. A
    Browser Source outlives the CaddieJ that served it, so after a restart the
    projector can be connected, receiving every snapshot, and still drawing
    them with yesterday's renderer: nothing on screen is wrong enough to make
    anyone refresh it, and the new parts of the floor simply never appear.

    `url` is a same-origin path the server built, never something the client
    sent. It names the build it expects and how many times this page has been
    asked to reach it, which is what lets the server stop asking.
    """

    v: int = PROTOCOL_VERSION
    type: Literal["reload"] = "reload"
    hud_assets_version: str
    url: str


class ClientMessage(BaseModel):
    """The only messages a client may send.

    Unknown fields are ignored so an older or newer client cannot break the
    server by sending extra keys.
    """

    model_config = ConfigDict(extra="ignore")

    type: str
    surface: str | None = None
    message: str | None = None
    assets_version: str | None = None
    # Echoed from the page's own address after a requested reload. Text rather
    # than a number so that a garbled count costs one reload decision, not the
    # whole hello.
    reload_for: str | None = None
    reload_attempt: str | None = None
    # Which page this is, and where it thinks it is. New after the first
    # hardware test, where two overlays were connected, one of them stale, and
    # nothing in the protocol distinguished them: both said `surface: floor`.
    # The id lasts as long as the page, so a reload that genuinely replaced
    # one shows up as a new id rather than as the same client claiming to have
    # changed its mind about which build it is.
    page_id: str | None = None
    href: str | None = None


def hello_message(*, hud_assets_version: str = "") -> HelloMessage:
    return HelloMessage(
        server_time=datetime.now(UTC).isoformat(),
        hud_assets_version=hud_assets_version,
    )


def snapshot_message(state: FloorState) -> SnapshotMessage:
    """Build a full snapshot.

    Snapshots are always complete: there is no delta protocol, because the state
    is small and a full snapshot removes an entire class of divergence bug.
    """

    return SnapshotMessage(seq=state.version, state=FloorStatePayload.from_state(state))
