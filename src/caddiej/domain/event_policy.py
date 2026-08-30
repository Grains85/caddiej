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
    DuplicateShotIgnored,
    HandednessChanged,
    IncompleteShotDiscarded,
    LaunchMonitorLinkChanged,
    LaunchMonitorReadinessChanged,
    ShotCompleted,
    ShotDiagnosed,
    ShotIngested,
    ShotMessageRejected,
    SimulatorAcknowledgedShot,
    SimulatorReportedError,
    SpeechCompleted,
    SpeechStarted,
    SwingClipFailed,
    SwingClipReady,
)
from caddiej.domain.scoring import HoleCompleted

CRITICAL = Delivery.CRITICAL
COALESCE = Delivery.COALESCE
DROP = Delivery.DROP

EVENT_DELIVERY: dict[type[Event], Delivery] = {
    ShotIngested: CRITICAL,
    ShotCompleted: CRITICAL,
    ShotMessageRejected: CRITICAL,
    DuplicateShotIgnored: CRITICAL,
    IncompleteShotDiscarded: CRITICAL,
    HoleCompleted: CRITICAL,
    ClubSelected: COALESCE,
    HandednessChanged: COALESCE,
    LaunchMonitorLinkChanged: COALESCE,
    LaunchMonitorReadinessChanged: COALESCE,
    BallDetected: COALESCE,
    BallCleared: COALESCE,
    CameraStateChanged: COALESCE,
    ShotDiagnosed: COALESCE,
    AnalysisStarted: DROP,
    AnalysisCompleted: COALESCE,
    AnalysisFailed: DROP,
    SwingClipReady: DROP,
    SwingClipFailed: DROP,
    SpeechStarted: COALESCE,
    SpeechCompleted: COALESCE,
    SimulatorAcknowledgedShot: DROP,
    SimulatorReportedError: DROP,
}

SUBSCRIBER_DELIVERY: dict[tuple[str, type[Event]], Delivery] = {
    ("coaching", ShotCompleted): DROP,
    ("coaching", SwingClipReady): DROP,
    ("coaching", SwingClipFailed): DROP,
    ("video", ShotCompleted): DROP,
    ("speech", AnalysisCompleted): COALESCE,
    ("lighting", SpeechStarted): COALESCE,
    ("lighting", SpeechCompleted): COALESCE,
    ("effects", HoleCompleted): DROP,
    ("play", ShotDiagnosed): COALESCE,
    ("play", AnalysisCompleted): COALESCE,
}

QUEUE_SIZES: dict[str, int] = {
    "play": 64,
    "history": 128,
    "coaching": 16,
    "video": 16,
    "speech": 8,
    "lighting": 8,
    "effects": 4,
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
