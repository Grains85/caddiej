"""Typed events describing facts observed about the room and the simulator.

These live in the domain, not in the adapter that publishes them, so that a
subscriber never has to import a vendor adapter to understand what happened.
Category base classes exist so that a subscriber to a category receives its
subtypes, without enumerating leaves.

Nothing here carries a vendor payload. These are in-process domain events used
for orchestration, not an event-sourced log. A raw GSPro message inside one
would freeze GSPro's shape into every subscriber, including history.
"""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from caddiej.core.events import Event
from caddiej.domain.coaching import Advice, AdviceFailure, CoachingFocus, Diagnosis
from caddiej.domain.media import CaptureState, ClipFailure, ClipRef
from caddiej.domain.observation import ObservedRoundShot
from caddiej.domain.shot import Club, Handedness, Shot
from caddiej.domain.speech import Envelope, SpeechOutcome, SpeechPriority


class LinkState(StrEnum):
    """The state of the connection to a launch monitor and the simulator."""

    DISABLED = "disabled"
    LISTENING = "listening"
    MONITOR_CONNECTED = "monitor_connected"
    BRIDGED = "bridged"
    SIMULATOR_UNAVAILABLE = "simulator_unavailable"
    BIND_FAILED = "bind_failed"


class RejectionReason(StrEnum):
    """Why a message was refused. Never a partial application."""

    UNPARSEABLE = "unparseable"
    SCHEMA_INVALID = "schema_invalid"
    OVERSIZED = "oversized"
    NO_SHOT_DATA = "no_shot_data"
    IMPLAUSIBLE_VALUE = "implausible_value"


# --- category bases -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LaunchMonitorEvent(Event):
    """Something changed about the launch monitor or its link."""


@dataclass(frozen=True, slots=True)
class ShotEvent(Event):
    """Something happened concerning a shot."""


@dataclass(frozen=True, slots=True)
class PlayerEvent(Event):
    """Something changed about who is playing or with what."""


@dataclass(frozen=True, slots=True)
class SimulatorEvent(Event):
    """The simulator said something back."""


@dataclass(frozen=True, slots=True)
class CaptureEvent(Event):
    """Something happened to the cameras watching the room."""


@dataclass(frozen=True, slots=True)
class CoachingEvent(Event):
    """Something was concluded about how a shot was struck."""


@dataclass(frozen=True, slots=True)
class SpeechEvent(Event):
    """Something changed about what the room is saying out loud."""


# --- launch monitor -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LaunchMonitorLinkChanged(LaunchMonitorEvent):
    state: LinkState
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class LaunchMonitorReadinessChanged(LaunchMonitorEvent):
    ready: bool
    device_id: str | None = None


@dataclass(frozen=True, slots=True)
class BallDetected(LaunchMonitorEvent):
    """A ball is in place. Only published by devices that report it."""

    device_id: str | None = None


@dataclass(frozen=True, slots=True)
class BallCleared(LaunchMonitorEvent):
    """The ball that was in place is gone."""

    device_id: str | None = None


# --- shots ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ShotIngested(ShotEvent):
    """A normalized shot entered CaddieJ. It has not been interpreted yet."""

    shot: Shot


@dataclass(frozen=True, slots=True)
class ShotCompleted(ShotEvent):
    """A shot as CaddieJ owns it, after the domain has said what it was.

    The difference from `ShotIngested` is the club, and it matters more than it
    sounds: no launch monitor names the club in its shot message, so an ingested
    shot is anonymous. This event is the one to subscribe to for anything that
    cares what a shot means — history, statistics, later a voice line — because
    it is the first point at which the shot is attributed.
    """

    shot: Shot


@dataclass(frozen=True, slots=True)
class ShotMessageRejected(ShotEvent):
    """A message could not be trusted, so nothing was applied from it."""

    reason: RejectionReason
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class DuplicateShotIgnored(ShotEvent):
    """The same shot arrived twice. Reported rather than silently dropped."""

    device_sequence: int | None = None
    device_id: str | None = None


@dataclass(frozen=True, slots=True)
class IncompleteShotDiscarded(ShotEvent):
    """Club data arrived with no ball data before its window closed."""

    device_sequence: int | None = None
    had_ball_data: bool = False
    had_club_data: bool = False


@dataclass(frozen=True, slots=True)
class ShotDispatched(LaunchMonitorEvent):
    """The launch monitor said it sent a shot to the simulator.

    Not `ShotCompleted`: that event requires a normalized `Shot`, and the
    ProTee log line that reports a send does not include launch numbers.
    """


@dataclass(frozen=True, slots=True)
class RoundShotObserved(ShotEvent):
    """A newly completed round-play shot, observed after CaddieJ started.

    Produced from GSPro's currentRound.dat. It is not a launch-monitor `Shot`:
    values stay raw because their units have not been verified. Historical
    shots already in the file at startup are recorded as a baseline and never
    published.
    """

    shot: ObservedRoundShot


# --- player ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClubSelected(PlayerEvent):
    """The club in play changed, as reported by the simulator."""

    club: Club


@dataclass(frozen=True, slots=True)
class HandednessChanged(PlayerEvent):
    """The player's handedness changed.

    This is the closest thing GSPro Open Connect v1 offers to a player change;
    it carries no player identity, so CaddieJ owns who is playing.
    """

    handedness: Handedness


# --- simulator ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SimulatorAcknowledgedShot(SimulatorEvent):
    device_sequence: int | None = None


@dataclass(frozen=True, slots=True)
class SimulatorReportedError(SimulatorEvent):
    code: int
    message: str


# --- capture --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CameraStateChanged(CaptureEvent):
    """A camera started, stopped, or gave up.

    Published so that a camera problem is visible somewhere other than the log,
    and so that nothing has to poll to find out filming stopped an hour ago.
    """

    camera_id: str
    state: CaptureState
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class SwingClipReady(CaptureEvent):
    """A clip of a swing is on disk, complete, and safe to open.

    This is the event a later analysis step waits for. It is published only
    after the file has been given its final name, so a subscriber never sees a
    clip that is still being written.
    """

    shot_id: UUID
    clip: ClipRef


@dataclass(frozen=True, slots=True)
class SwingClipFailed(CaptureEvent):
    """No clip could be produced for a shot, and this is why.

    A shot with no clip is worth saying out loud: it is the difference between
    "the swing was not filmed" and "the swing is still being written".
    """

    shot_id: UUID
    camera_id: str
    reason: ClipFailure
    detail: str | None = None


# --- coaching -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ShotDiagnosed(CoachingEvent):
    """The deterministic verdict on a shot, available the moment it arrives.

    Published for every shot, including the good ones, where the focus is
    `NONE`. This is the event the floor acts on, so that what a player reads is
    never waiting on a network.
    """

    diagnosis: Diagnosis


@dataclass(frozen=True, slots=True)
class AnalysisStarted(CoachingEvent):
    """A model is being asked to phrase coaching for a shot.

    Worth an event rather than only a log line: it is what lets the floor show
    that something is being thought about, so a line arriving four seconds later
    reads as considered rather than broken.
    """

    shot_id: UUID
    focus: CoachingFocus
    frames: int
    model: str


@dataclass(frozen=True, slots=True)
class AnalysisCompleted(CoachingEvent):
    """A model's words passed validation and are safe to show or speak."""

    shot_id: UUID
    advice: Advice


@dataclass(frozen=True, slots=True)
class AnalysisFailed(CoachingEvent):
    """No coached line for this shot, and why.

    Published even though the templated line is already on the floor, because
    "the provider was rate-limited" and "that shot was fine" are different facts,
    and health, cost review and a later session summary all want to tell them
    apart.
    """

    shot_id: UUID
    reason: AdviceFailure
    detail: str | None = None


# --- speech ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpeechStarted(SpeechEvent):
    """Audio for an utterance began playing.

    Published at the moment sound starts rather than when the text was queued,
    because the two can be seconds apart and only one of them is a fact about
    the room. Anything that wants to duck music, dim a light or hold a line on
    the floor for as long as it is being read wants this one.
    """

    utterance_id: UUID
    text: str
    voice: str
    priority: SpeechPriority
    duration_seconds: float
    """How long the rendered audio runs. Known before playback because the
    samples already exist, so a listener can plan around the whole line rather
    than discovering its length when it ends."""

    shot_id: UUID | None = None
    envelope: Envelope | None = None
    """The shape of the line, for anything that wants to move with the voice.

    On the event rather than fetched, because the alternative is handing out the
    audio, and then everything that wanted to dim a light would need to know
    about PCM. Carrying the shape instead is a few dozen floats and keeps the
    samples where they belong.

    `None` means nobody measured it, not that the line was silent. It is
    optional because a listener that only wants to know a voice is talking
    should not be broken by an engine that cannot say how loudly."""


@dataclass(frozen=True, slots=True)
class SpeechCompleted(SpeechEvent):
    """An utterance is finished with, one way or another.

    Published for every utterance the service accepted, including those that
    never made a sound — so a `SpeechCompleted` with no preceding
    `SpeechStarted` is normal and means exactly that. `outcome` says which
    happened. Without this, silence has no explanation and nothing downstream
    can tell a quiet room from a broken one.
    """

    utterance_id: UUID
    outcome: SpeechOutcome
    spoken_seconds: float = 0.0
    shot_id: UUID | None = None
    detail: str | None = None
