"""Swing video, in the terms the rest of CaddieJ refers to it by.

A clip is a fact about a shot — that swing was filmed, the file is here, and the
ball was struck this far into it — so it belongs beside the shot rather than
inside the subsystem that produced it. A voice line, an overlay, or a later
analysis step can all subscribe to "a clip exists" without importing a camera.

Nothing here knows about FFmpeg, segments, containers or devices. The one thing
that leaks through from the physical world is `TimeQuality`, and it leaks
deliberately: a consumer that is about to say "impact was at frame 118" needs to
be told how much that claim can be trusted.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from caddiej.domain.cues import PanelKind, PanelView, VisualClaim
from caddiej.domain.swing_phase import SwingPhase


class CaptureState(StrEnum):
    """What a camera is doing, as far as CaddieJ can tell."""

    DISABLED = "disabled"
    STARTING = "starting"
    RECORDING = "recording"
    RETRYING = "retrying"
    UNAVAILABLE = "unavailable"


class ClipFailure(StrEnum):
    """Why no clip came out. Reported, never silent."""

    NOT_RECORDING = "not_recording"
    """Nothing was being filmed when the shot happened."""

    BUFFER_RECYCLED = "buffer_recycled"
    """The swing was recorded but had already been deleted to make room."""

    INCOMPLETE = "incomplete"
    """Part of the window is missing, usually because capture restarted."""

    QUEUE_FULL = "queue_full"
    """Clips were already backed up, so this one was refused rather than queued."""

    EXTRACTION_FAILED = "extraction_failed"
    TIMED_OUT = "timed_out"

    ABANDONED = "abandoned"
    """CaddieJ stopped before the clip was finished. Said rather than left
    silent, because anything waiting on the clip — a coaching job holding a shot
    open for footage — otherwise waits for something that is never coming."""


class CameraView(StrEnum):
    """Where the camera is standing, which decides what it can prove.

    Not detected and not detectable from the footage; somebody put the camera
    on a tripod and only they know where. It is here because a coach reading
    stills needs it before anything else: face-on shows sway, posture and how
    the body turns, and down-the-line shows plane, shaft and where the club
    goes. The two are not interchangeable, and a swing fault named from the
    angle that cannot see it is a guess wearing a diagnosis.

    `UNKNOWN` is the default and is not a gap to be filled in with a plausible
    assumption. It means the model is told the angle is unknown and must keep
    to what any angle would show.
    """

    UNKNOWN = "unknown"
    FACE_ON = "face_on"
    """Square to the player, looking down the target line from in front."""

    DOWN_THE_LINE = "down_the_line"
    """Behind the player, along the target line."""


class TimeQuality(StrEnum):
    """How much to trust where impact sits inside a clip.

    Three unknowns lie between a ball being struck and a frame being written:
    the camera's own latency, how long the launch monitor took to report the
    shot, and the drift between two clocks. `ESTIMATED` means none of them has
    been measured, which is the honest default and is why a clip is cut wide
    rather than tight.
    """

    ESTIMATED = "estimated"
    CALIBRATED = "calibrated"
    """A measured per-camera latency was applied."""


@dataclass(frozen=True, slots=True)
class SwingFrame:
    """One still of a swing, and where it sits in time.

    `path` is relative to the capture directory, for the same reason a clip's
    is relative to the media root: the whole capture can be copied elsewhere
    and still be readable.

    `width` and `height` are here because they are what makes a *normalized*
    annotation meaningful later. A coach that answers "a line from (0.42, 0.31)
    to (0.45, 0.78) on camera 1 frame 5" has said something CaddieJ can draw
    only if the pixel size of that frame is recorded beside it. Zero means the
    still could not be measured, not that it is empty.
    """

    camera_id: str
    index: int
    """1-based position within the selected set, which is what an annotation
    refers to. Not the index of the frame within everything captured."""

    offset_seconds: float
    """When the still was taken, measured from the start of the capture window
    rather than from impact: nothing here knows where impact fell."""

    path: Path
    width: int = 0
    height: int = 0

    phase: SwingPhase | None = None
    """Which moment of the swing the selector was aiming at when it kept this
    still, or None when it was not aiming at anything in particular.

    A guess from the clock, and travels as one. Nothing here has decoded a
    pixel; the phase is the calibrated fraction of this camera's replay where
    that moment fell on the captures we have, so it is right about timing and
    only probably right about the picture. It is recorded and shown to the
    model because a named guess can be contradicted -- a model told this is the
    transition can answer that it is looking at the top -- and an unnamed still
    cannot be. A capture whose labels are wrong is then visible as a model that
    keeps disagreeing, rather than as a confident rating of the wrong moment."""


@dataclass(frozen=True, slots=True)
class SwingCameraCapture:
    """What one camera produced for one swing, including nothing at all."""

    camera_id: str
    source: str
    """The OBS source the stills were read from, as configured."""

    ok: bool
    frames: tuple[SwingFrame, ...]
    captured: int
    """How many stills were taken before selection. `frames` is the subset kept."""

    failed: int
    """How many requests for a still did not produce one."""

    detail: str | None = None
    kind: PanelKind = PanelKind.DYNAMIC
    supports: frozenset[VisualClaim] = field(default_factory=frozenset)
    settled: bool = False
    """For a static panel: whether two trailing stills matched."""

    view: PanelView = PanelView.NONE
    blank_rejected: int = 0
    duplicate_frames: int = 0


@dataclass(frozen=True, slots=True)
class MetricsStill:
    """The one post-shot still of ProTee's All Values grid, or why there is none.

    Separate from the swing panels on purpose: it is a static readout that the
    coach reads numbers from, not a body view, and it never counts against the
    swing-frame budget.
    """

    source: str
    ok: bool
    path: Path | None = None
    """Relative to the capture directory, when `ok`."""

    width: int = 0
    height: int = 0
    captured_at: datetime | None = None
    delay_seconds: float = 0.0
    """The configured delay the still was taken after."""

    offset_seconds: float | None = None
    """When it was actually taken, measured from the strike."""

    detail: str | None = None
    """Why there is no usable still, when there is not."""


@dataclass(frozen=True, slots=True)
class SwingCapture:
    """The swing CaddieJ currently holds stills of.

    Deliberately not a history. A handful of the newest shots are kept as
    evidence and older ones are deleted, because the thing a coach is asked
    about is the swing that just happened.
    """

    capture_id: UUID
    trigger_id: str
    """The launch monitor's own detection counter, as `ShotDetected` carried
    it. Not a shot identity: it restarts when the device does."""

    directory: Path
    """This shot's own evidence directory. Never renamed once published."""

    started_at: datetime
    """When the first still was requested, which is after the configured delay
    rather than at the moment of the strike."""

    detected_at: datetime | None
    cameras: tuple[SwingCameraCapture, ...]
    """Panels of the ProTee presentation. Named `cameras` for the two-panel
    origin; each entry is one configured panel."""

    strike_id: UUID | None = None
    """The `ShotDetected` event this capture was taken for. The one identity
    that runs from the strike to the coaching line without GSPro."""

    metrics: MetricsStill | None = None
    """The All Values still, when metrics capture is configured."""

    published: Path | None = None
    """`latest_swing`, when the inspection copy of this shot was put there."""

    @property
    def coaching_id(self) -> UUID:
        """The id a coaching result for this capture is keyed on."""

        return self.strike_id if self.strike_id is not None else self.capture_id

    @property
    def panels(self) -> tuple[SwingCameraCapture, ...]:
        return self.cameras

    @property
    def frames(self) -> int:
        return sum(len(camera.frames) for camera in self.cameras)

    @property
    def complete(self) -> bool:
        return bool(self.cameras) and all(
            camera.ok or not camera.source for camera in self.cameras
        )


@dataclass(frozen=True, slots=True)
class ClipRef:
    """Where a swing clip is, and where in it the ball was struck.

    `path` is relative to the media root so that the whole library can be moved
    or copied to another machine without invalidating a stored reference.

    The times are wall clock. A monotonic reading would be more precise, but it
    is meaningless outside the process that took it, and a clip has to stay
    interpretable next week by something that was not running today.
    """

    clip_id: UUID
    shot_id: UUID
    camera_id: str
    path: Path
    recorded_at: datetime
    impact_at: datetime
    impact_offset_seconds: float
    duration_seconds: float
    fps: float
    width: int
    height: int
    time_quality: TimeQuality = TimeQuality.ESTIMATED
