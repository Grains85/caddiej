"""Explicit delivery policy for in-process domain events.

The bus does not guess from class names. This table is the policy: a
test can read it, and `subscribe` call sites only pass a subscriber name.
Anything unlisted is treated as critical, so a new event type cannot
silently become droppable.

Two layers:

* `EVENT_DELIVERY` is the default for a fact, wherever it is subscribed.
* `SUBSCRIBER_DELIVERY` is the override for a named subscriber. Enrichment
  subscribers treat shot facts as stale work they may drop; history and
  play do not appear here, so they keep the lossless default.
"""

from caddiej.core.delivery import DEFAULT_QUEUE_SIZE, Delivery
from caddiej.core.events import Event
from caddiej.domain.events import (
    AnalysisCompleted,
    AnalysisFailed,
    AnalysisStarted,
    BallCleared,
    BallDetected,
    CameraStateChanged,
    ClubSelected,
    CoachingProgress,
    DuplicateShotIgnored,
    HandednessChanged,
    IncompleteShotDiscarded,
    LaunchMonitorLinkChanged,
    LaunchMonitorReadinessChanged,
    RoundShotObserved,
    ShotCompleted,
    ShotDetected,
    ShotDiagnosed,
    ShotDispatched,
    ShotIngested,
    ShotMessageRejected,
    SimulatorAcknowledgedShot,
    SimulatorReportedError,
    SpeechCompleted,
    SpeechStarted,
    SwingClipFailed,
    SwingClipReady,
    SwingFramesReady,
    TargetDistanceObserved,
)
from caddiej.domain.scoring import HoleCompleted

CRITICAL = Delivery.CRITICAL
COALESCE = Delivery.COALESCE
DROP = Delivery.DROP

EVENT_DELIVERY: dict[type[Event], Delivery] = {
    ShotIngested: CRITICAL,
    ShotCompleted: CRITICAL,
    RoundShotObserved: CRITICAL,
    ShotMessageRejected: CRITICAL,
    DuplicateShotIgnored: CRITICAL,
    IncompleteShotDiscarded: CRITICAL,
    HoleCompleted: CRITICAL,
    ClubSelected: COALESCE,
    HandednessChanged: COALESCE,
    LaunchMonitorLinkChanged: CRITICAL,
    LaunchMonitorReadinessChanged: COALESCE,
    # One per strike, and not interchangeable with the one before it: coalescing
    # two detections would lose a shot rather than collapse a repeated status.
    ShotDetected: CRITICAL,
    ShotDispatched: COALESCE,
    BallDetected: COALESCE,
    BallCleared: COALESCE,
    TargetDistanceObserved: COALESCE,
    CameraStateChanged: COALESCE,
    ShotDiagnosed: COALESCE,
    AnalysisStarted: DROP,
    AnalysisCompleted: COALESCE,
    AnalysisFailed: DROP,
    # Animation. Published from the capture task and the coaching worker, and
    # neither may ever wait on a floor to draw a bar.
    CoachingProgress: DROP,
    SwingClipReady: DROP,
    SwingClipFailed: DROP,
    # Enrichment, and superseded by its own successor: only the newest swing
    # exists on disk, so a queued announcement of an older one is already
    # describing a directory that has been replaced.
    SwingFramesReady: COALESCE,
    SpeechStarted: COALESCE,
    SpeechCompleted: COALESCE,
    SimulatorAcknowledgedShot: DROP,
    SimulatorReportedError: DROP,
}

SUBSCRIBER_DELIVERY: dict[tuple[str, type[Event]], Delivery] = {
    # The reference coach retires an analysis when the next unique strike
    # arrives, and again when that ball's result completes. Losing either
    # notice costs a stale line, never the shot. The publisher is the ProTee
    # observer and must not wait on evidence processing. A full coach queue
    # drops its oldest item so the new strike is still delivered.
    ("reference-coach", ShotCompleted): DROP,
    ("reference-coach", ShotDetected): DROP,
    ("video", ShotCompleted): DROP,
    # The clip's anchor. Droppable for the same reason as the shot itself: the
    # publisher is the ProTee log observer that also feeds the floor, and it
    # may never wait on a camera. A lost anchor costs one badly centred clip,
    # which is what every clip used to be.
    ("video", ShotDetected): DROP,
    ("speech", AnalysisCompleted): COALESCE,
    # The ProTee log observer publishes the strike and must never wait on a
    # voice: losing one costs a stale line being retired by the next event
    # instead of this one, never a shot.
    ("speech", ShotDetected): DROP,
    ("lighting", SpeechStarted): COALESCE,
    ("lighting", SpeechCompleted): COALESCE,
    ("effects", HoleCompleted): DROP,
    # Decoration, and the only subscriber allowed to lose a shot. Critical
    # delivery lets a publisher wait for a full queue, and the publisher here is
    # the ProTee log observer that also feeds the address HUD and the floor's
    # readiness: a replay must never be able to hold up what the room reports.
    ("shot_video", ShotDetected): DROP,
    # The other ProTee view, and decoration for the same reason.
    ("top_impact", ShotDetected): DROP,
    # Same reasoning, and the same publisher: the observer that feeds the floor
    # must never wait on a subsystem that takes screenshots for several seconds.
    ("swing_capture", ShotDetected): DROP,
    ("play", ShotDiagnosed): COALESCE,
    ("play", AnalysisCompleted): COALESCE,
    # Still never waits, but on the play queue a drop would be the wrong kind
    # of loss. DROP makes room by discarding the oldest item of any type, and
    # the oldest item here may be a strike or a result. Coalescing replaces a
    # waiting progress with the newer one, or drops the incoming one when the
    # queue is full of other facts, and so can only ever lose animation.
    # Subscribed under `play` rather than a name of its own so it is ordered
    # with the strike and the completion that make it stale.
    ("play", CoachingProgress): COALESCE,
}

QUEUE_SIZES: dict[str, int] = {
    "play": 64,
    "history": 128,
    "reference-coach": 16,
    "video": 16,
    "speech": 8,
    "lighting": 8,
    "effects": 4,
    "shot_video": 8,
    "top_impact": 8,
    "swing_capture": 8,
}


def delivery_for(subscriber: str, event_type: type[Event]) -> Delivery:
    """Policy for this subscriber receiving this event class.

    Subscriber overrides win. Then the event class, then its bases, then
    critical. Walking bases means a category subscription still has a
    policy when the leaf type is the one being published.
    """

    override = SUBSCRIBER_DELIVERY.get((subscriber, event_type))
    if override is not None:
        return override
    for cls in event_type.__mro__:
        if cls is Event:
            break
        listed = EVENT_DELIVERY.get(cls)
        if listed is not None:
            return listed
    return CRITICAL


def queue_size_for(subscriber: str) -> int:
    return QUEUE_SIZES.get(subscriber, DEFAULT_QUEUE_SIZE)
