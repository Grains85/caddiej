"""Coaching a shot, without a language model ever being able to delay one.

Four rules shape this file.

**The event path only does arithmetic.** `on_shot_completed` diagnoses the shot,
publishes the verdict and puts a job on a queue. It touches no network, no disk
and no model, so the line a player reads is on the floor before anything can go
wrong. Everything slow happens in the worker.

**There is no catching up.** History queues hundreds of shots because a shot
recorded late is still a shot recorded. Coaching is the opposite: advice for three
swings ago is worse than silence, because the player has moved on and would map
the words onto the wrong swing. So the queue is short, the newest shot displaces
the oldest waiting one, and a reply that arrives after the next shot is thrown
away rather than shown.

**Failure is invisible.** Every way this can go wrong ends with the templated
line still on the floor, a row written saying what happened, and a counter moved.
Nothing raises, and nothing reaches the application.

**A model chooses words and nothing else.** It is given the faults CaddieJ
measured and may pick among them; the severity it returns is discarded wherever a
measurement ranked the fault. Its answer is two short strings, so there is no
field in it that could name an action. Where it has to choose before the faults
are known — ProTee, whose numbers it reads in the same reply — a cue the numbers
then do not admit loses to CaddieJ's own, and its words are kept only if they
could have been written for CaddieJ's.
"""

import asyncio
import logging
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from caddiej.coaching import vx
from caddiej.coaching.baseline import Baseline
from caddiej.coaching.cache import AdviceCache, fingerprint
from caddiej.coaching.cues import (
    CUES_BY_ID,
    CueAuthority,
    admissible_cues,
    authority_cue,
    cue_speech,
    cue_supported,
    evidence_grade_summary,
    preferred_cue,
    promoted_diagnosis,
)
from caddiej.coaching.diagnose import _CONSISTENCY_MINIMUM_SHOTS, diagnose
from caddiej.coaching.evidence import render
from caddiej.coaching.faults import is_positive
from caddiej.coaching.frames import (
    DEFAULT_OFFSETS,
    CaptureStillReader,
    FrameSource,
    offsets_for,
)
from caddiej.coaching.lesson import LessonStep
from caddiej.coaching.mechanics import club_motion, phase_summary
from caddiej.coaching.metrics_strip import strip_metrics
from caddiej.coaching.prompt import (
    PROMPT_VERSION,
    build_request,
    fingerprint_parts,
)
from caddiej.coaching.provider import (
    AdviceProvider,
    AdviceRequest,
    ImageInput,
    ProviderError,
    ProviderRateLimitedError,
    ProviderRefusedError,
    ProviderReply,
    ProviderTimeoutError,
    ProviderUnavailableError,
    image_inputs,
)
from caddiej.coaching.score import score_swing
from caddiej.coaching.session import CoachingSession, SessionProgress
from caddiej.coaching.swing_faults import (
    corroborate,
    infer_faults,
    protee_signs,
    rank_root_cause,
)
from caddiej.coaching.templates import legal_displays, speech_for
from caddiej.coaching.validate import (
    AdviceRejected,
    SpeechRejected,
    from_cue,
    from_diagnosis,
    recent_lines,
    validate,
)
from caddiej.core.event_bus import EventBus
from caddiej.core.events import Event
from caddiej.domain.coaching import (
    OBSERVED_FOCUSES,
    SEVERITY_FINE,
    Advice,
    AdviceFailure,
    AdviceKind,
    AdviceOutcome,
    AdviceSource,
    AdviceStatus,
    CoachingFocus,
    Diagnosis,
)
from caddiej.domain.cues import CueId, EvidenceGrade
from caddiej.domain.events import (
    AnalysisCompleted,
    AnalysisFailed,
    AnalysisStarted,
    ClubSelected,
    CoachingProgress,
    CoachingStage,
    EvidenceFrame,
    HandednessChanged,
    ShotCompleted,
    ShotDetected,
    ShotDiagnosed,
    SwingClipFailed,
    SwingClipReady,
    SwingFramesReady,
    TargetDistanceObserved,
)
from caddiej.domain.media import CameraView, ClipRef, SwingCapture
from caddiej.domain.shot import Club, ClubKind, Handedness
from caddiej.domain.statistics import club_of
from caddiej.domain.swing_checkpoints import (
    CHECKPOINT_VIEW_WORDS,
    CheckpointAssessment,
)
from caddiej.domain.swing_review import (
    InferredFault,
    LessonSource,
    ProteeSign,
    RootCause,
    SwingReview,
)
from caddiej.domain.swing_score import MechanicalScore, SwingScore
from caddiej.domain.vx import CoachResult, VxMetrics
from caddiej.runtime.backoff import Backoff
from caddiej.swing_capture.sheet import (
    SHEET_LABEL,
    SHEET_PANEL_ID,
    CellKind,
    EvidenceSheet,
    SheetCell,
    plan_cells,
    render_evidence_sheet,
)
from caddiej.swing_capture.stills import (
    IMPACT_PANEL_ID as _IMPACT_PANEL_ID,
)
from caddiej.swing_capture.stills import (
    METRICS_LABEL,
    METRICS_PANEL_ID,
    frame_views,
    note_provider_images,
    read_metrics_still,
)
from caddiej.swing_capture.store import METRICS_FRAME_ID, frame_files

logger = logging.getLogger(__name__)

Publish = Callable[[Event], Awaitable[None]]
Record = Callable[[AdviceOutcome], Awaitable[object]]
RecordCoach = Callable[[CoachResult], Awaitable[object]]
SheetRenderer = Callable[
    [SwingCapture, Sequence[SheetCell]], Awaitable[EvidenceSheet | None]
]
"""Draws a shot's Swing Evidence Sheet from its originals. Never raises."""

TRIGGER_PROTEE = "protee"
TRIGGER_GSPRO = "gspro"
# The two moments a shot's focus is logged. A diagnosis is a candidate: the
# worst-ranked fault, named before anything was asked. The line that passed
# validation is final, and it is the one the floor, the voice and the session
# all follow. Logged under one field so a filter on it reads as a timeline.
STAGE_CANDIDATE = "candidate"
STAGE_FINAL = "final"
Sleep = Callable[[float], Awaitable[None]]
Now = Callable[[], datetime]

_STOP = object()
# How many shots back the "do not repeat yourself" list reaches.
_RECENT_LINES = 3
# Bounded, so a long evening cannot grow the set of shots already handled.
_SEEN_LIMIT = 256

_DRY_RUN = "diagnostic_dry_run"
_DRY_RUN_DETAIL = (
    "COACHING_DIAGNOSTIC_DRY_RUN is on; the evidence was prepared and no "
    "provider request was made"
)


def _insufficient_detail(diagnosis: Diagnosis) -> str:
    """Which evidence was missing, so silence is not an empty log."""

    missing = ", ".join(diagnosis.unmeasured) or "none"
    parts = [f"missing measurements: {missing}"]
    if (
        diagnosis.club is not ClubKind.PUTTER
        and diagnosis.baseline_shots < _CONSISTENCY_MINIMUM_SHOTS
        and not any(
            item.focus is CoachingFocus.CONSISTENCY for item in diagnosis.observations
        )
    ):
        parts.append(
            "consistency needs "
            f"{_CONSISTENCY_MINIMUM_SHOTS} earlier shots with this club "
            f"(have {diagnosis.baseline_shots})"
        )
    parts.append("no coaching line was invented")
    return "; ".join(parts)


def _scored(advice: Advice, job: "_Job") -> Advice:
    """The line with its swing's score and readings on it, whoever wrote it.

    Applied where a line is settled and where it is stored rather than where
    each one is built, because a line can be built in half a dozen places and
    neither the number nor the readings must depend on which of them it was.
    """

    metrics = strip_metrics(job.metrics)
    if (
        (job.swing_score is None or advice.swing_score is job.swing_score)
        and advice.metrics == metrics
        and (job.review is None or advice.review is job.review)
    ):
        return _reinforced(advice, job)
    return _reinforced(
        replace(
            advice,
            swing_score=job.swing_score or advice.swing_score,
            metrics=metrics,
            review=job.review or advice.review,
        ),
        job,
    )


REINFORCEMENT_SEVERITY = 0.15


def _reinforced(advice: Advice, job: "_Job") -> Advice:
    """The lesson said as a reinforcement under its progress headline.

    The cue's reviewed action stays the line; only the headline and the kind
    change, so "MUCH BETTER RELEASE" is followed by the same physical thing
    the player has been working on."""

    authority = job.authority
    if (
        authority is None
        or not authority.reinforce
        or advice.cue is not authority.cue
        or advice.kind is AdviceKind.REINFORCEMENT
    ):
        return advice
    return replace(
        advice,
        kind=AdviceKind.REINFORCEMENT,
        observation=authority.headline or advice.observation,
        severity=min(advice.severity, REINFORCEMENT_SEVERITY),
    )


def _evidence(job: "_Job", advice: Advice) -> EvidenceFrame | None:
    """The still the *player* is shown, in the capture it was sent from.

    Not the same question as "which frame did the line cite". A measured cue
    cites the All Values grid, correctly — that grid is where the fault was
    read, and it stays in the stored row as provenance. It is 5102 × 435
    pixels of eight-point type, and putting it on a projected floor as the
    picture of the player's swing is worse than putting nothing there: it is
    unreadable, it is not a swing, and it crowds out the words that are.

    So the grid is translated here, once, deterministically: to the impact
    still if this capture has one, because impact is the moment the numbers
    describe and is the one frame that is genuinely about them, and otherwise
    to nothing. Never to a replay frame chosen for the occasion — a picture of
    a backswing offered as evidence for a face angle is a claim CaddieJ did
    not make and cannot support.

    Everything else is as it was: only a frame the request carried and the
    capture names. No file is opened here.
    """

    frame = advice.evidence_frame_id
    capture = job.capture
    if frame is None or capture is None:
        return None
    if frame == METRICS_FRAME_ID:
        frame = _impact_frame(job)
        if frame is None:
            return None
    elif frame not in job.frame_ids:
        return None
    if frame not in frame_files(capture):
        return None
    return EvidenceFrame(capture_id=capture.capture_id, frame_id=frame)


def _impact_frame(job: "_Job") -> str | None:
    """The impact still this request sent, if it sent one.

    The lowest-numbered `impact_*` id, so a capture with several is resolved
    the same way every time rather than by whatever order the reader returned.
    """

    impact = sorted(
        frame for frame in job.frame_ids if frame.startswith(f"{_IMPACT_PANEL_ID}_")
    )
    return impact[0] if impact else None


def _cue_frame(job: "_Job", cue: CueId, frame_id: str | None = None) -> str | None:
    """The still a cue CaddieJ chose points at, when it chose rather than a model.

    A measured cue came from the All Values grid, so the grid is its evidence
    whenever it was sent; `_evidence` is what decides what the player then
    sees. A visual cue CaddieJ promoted points at the frame the rating that
    promoted it named, which is the only picture anybody has claimed shows
    that movement. A swing frame the model cited for some *other* cue is not
    used, because it was chosen to illustrate a different fault.
    """

    if CUES_BY_ID[cue].grade is EvidenceGrade.VISUAL:
        return frame_id if frame_id in job.frame_ids else None
    return METRICS_FRAME_ID if METRICS_FRAME_ID in job.frame_ids else None


def _promoted_confidence(
    cue: CueId, authority: "CueAuthority | None"
) -> dict[str, float]:
    """The rating's own confidence, for a visual cue CaddieJ substituted in.

    A visual cue is judged on `visual_confidence` and has no other source of
    severity, and the payload being edited was written by a model that chose a
    *measured* cue and had no reason to put one in. Substituting the cue
    without the number produced the one failure the first hardware test found:
    a promoted `OVER_THE_TOP` refused for having no visual confidence, after
    the same reply had supplied one with the rating that promoted it.

    So the number travels with the promotion. It is the model's own, already
    quantized and ceilinged by `cap_visual_confidence` on the way in, and it
    is written here rather than defaulted: a cue CaddieJ promoted with no
    confidence behind it still has none, and is still refused.
    """

    if authority is None or not authority.from_visual:
        return {}
    if authority.cue is not cue or authority.confidence is None:
        return {}
    return {"visual_confidence": authority.confidence}


def _needs_reconciling(
    chosen: str,
    diagnosis: Diagnosis,
    allowed: Sequence[CueId],
    authority: "CueAuthority | None",
) -> bool:
    """Whether CaddieJ's lesson has to replace the one the model named.

    Two cases, and deliberately only two. The model picked something the
    numbers do not admit, which has always been reconciled. Or a seen swing
    fault was promoted over a measured line, in which case any other cue —
    admissible or not — is the wrong lesson for this shot and the model is
    not left to choose.

    Everywhere else the model keeps its choice. It is picking from a list
    CaddieJ computed, and a reply that lands on a different admissible cue
    from the one `preferred_cue` would have is a second opinion worth having
    rather than an error.
    """

    if diagnosis.cue is None or diagnosis.cue not in allowed:
        return False
    if authority is not None and authority.from_visual:
        # A mechanical lesson is always said in its reviewed words, even if
        # a reply happened to name the same cue.
        return True
    if chosen == diagnosis.cue.value:
        return False
    return chosen not in {cue.value for cue in allowed}


def _protee_signs(
    metrics: VxMetrics | None, diagnosis: Diagnosis
) -> tuple[ProteeSign, ...]:
    """ProTee's delivery patterns, player-relative, against the club's windows."""

    if metrics is None:
        return ()
    handedness = diagnosis.handedness

    def relative(name: str) -> float | None:
        value = vx.signed(metrics, name, handedness)
        if value is None:
            return None
        return -value if handedness is Handedness.LEFT else value

    return protee_signs(
        club=diagnosis.club,
        path=relative("swing_path"),
        face=relative("face_angle"),
        attack=vx.signed(metrics, "attack_angle", handedness),
    )


MAX_LOGGED_FAULTS = 8


def _faults_summary(faults: Sequence[InferredFault]) -> str:
    """`fault=severity:confidence[+protee]` for the first few, swing order."""

    return (
        ",".join(
            f"{item.fault.value}={item.severity:.2f}:{item.confidence:.2f}"
            + ("+protee" if item.corroborated_by else "")
            for item in faults[:MAX_LOGGED_FAULTS]
        )
        or "none"
    )


def _ranking_summary(root: RootCause | None) -> str:
    if root is None or not root.ranking:
        return "none"
    return ",".join(
        f"{row.fault.value}={row.effective:.3f}"
        + ("" if row.explained_by is None else f"<{row.explained_by.value}")
        for row in root.ranking[:MAX_LOGGED_FAULTS]
    )


def _component_summary(score: SwingScore) -> str:
    """`name=grade:sub-score` per component, in rubric order.

    Sub-scores rather than readings: the log allowlist admits no metric value,
    and the readings are stored whole with the row the score came from."""

    return ",".join(
        f"{item.component.value}={item.grade.value}"
        + ("" if item.sub_score is None else f":{item.sub_score:.2f}")
        for item in score.components
    )


# Which provider failures are worth one more attempt, and what to call them.
_RETRYABLE: dict[type[ProviderError], AdviceFailure] = {
    ProviderUnavailableError: AdviceFailure.PROVIDER_ERROR,
    ProviderRateLimitedError: AdviceFailure.RATE_LIMITED,
    ProviderTimeoutError: AdviceFailure.TIMED_OUT,
}


@dataclass(frozen=True, slots=True)
class AnalysisHealth:
    """Everything that can go wrong with coaching, plus what it has cost.

    Counts rather than money. Prices change, and a per-token figure written into
    the source goes quietly wrong and becomes a number somebody trusts.
    """

    enabled: bool
    available: bool
    provider: str | None
    model: str | None
    focus: str | None
    """What the session has settled on, which is the one thing here that says
    whether coaching is behaving like a lesson or like a list of faults."""

    shots_diagnosed: int
    corrections: int
    reinforcements: int
    lines_from_model: int
    lines_from_template: int
    calls: int
    retries: int
    calls_remaining: int
    rejected: int
    failures: int
    dropped: int
    cache_hits: int
    images_sent: int
    input_tokens: int
    output_tokens: int
    queued: int
    last_error: str | None
    visual_cues_chosen: int = 0
    cues_admitted: int = 0


@dataclass(slots=True)
class _Job:
    """One shot waiting to be coached, and the clip it may be waiting for."""

    diagnosis: Diagnosis
    queued_monotonic: float
    progress: SessionProgress = field(default_factory=SessionProgress)
    """The session as it stood before this shot, so a trend shown to a model
    describes the swings leading up to this one rather than including it."""

    wants_frames: bool = False
    wants_clip: bool = False
    """Whether a camera clip is still expected. It decides who is allowed to
    end the wait: a capture arriving first must not, because on hardware it
    always arrives first — ProTee has the replay on screen before FFmpeg has
    finished cutting — and a bay that went to the trouble of placing a camera
    would then never once use it."""

    trigger_id: str | None = None
    """The detection this shot came from, which is how a swing capture — keyed
    on the device's trigger and not on a shot id — finds the job it belongs
    to."""

    clip: ClipRef | None = None
    capture: SwingCapture | None = None
    footage: asyncio.Event = field(default_factory=asyncio.Event)

    protee: bool = False
    """Coached from the ProTee strike and its All Values still, not from a
    GSPro shot. `shot_id` is then the strike id."""

    metrics: VxMetrics | None = None
    metrics_status: str = "not_configured"
    swing_score: SwingScore | None = None
    """Made from `metrics` and `visual` the moment they are read, before the
    coaching line is judged, so every way the line can end still carries it."""

    checkpoints: tuple[CheckpointAssessment, ...] = ()
    """The model's checkpoint scores that survived parsing: checked against
    the frames this request sent, the camera each frame came from, and the
    phase the model itself said each picture shows."""

    review: SwingReview | None = None
    """CaddieJ's faults, root cause and lesson progress for this swing."""

    authority: CueAuthority | None = None
    """The lesson this shot is coached on, and whether it is a reinforcement."""

    swing_images: int = 0
    frame_ids: tuple[str, ...] = ()
    """The ids of the images the request carried, which are the only frames a
    reply may cite as its evidence. Always canonical original-frame ids, even
    when the pictures travelled as cells of one evidence sheet."""

    cell_map: tuple[tuple[str, str], ...] = ()
    """Printed sheet cell ID -> canonical frame id, for this request's reply."""

    @property
    def shot_id(self) -> UUID:
        return self.diagnosis.shot_id

    def offer_capture(self, capture: SwingCapture) -> None:
        """Attach a swing capture, ending the wait only if nothing better is
        still coming."""

        self.capture = capture
        if not self.wants_clip:
            self.footage.set()


def _images_from_capture(
    raw: object,
) -> tuple[tuple[ImageInput, ...], tuple[tuple[str, str], ...]]:
    """Bytes become unlabeled images. Labeled stills keep panel, view and id.

    A reader that names its frames is believed. One that does not — three
    items rather than four — gets ids by position within each panel, which is
    the same answer whenever no still of a panel was skipped. A fifth item is
    an image detail the still is asking for; its absence means the request's
    own setting is right for it, which is true of most stills.
    """

    if not isinstance(raw, tuple) or not raw:
        return (), ()
    first = raw[0]
    if (
        isinstance(first, tuple)
        and 3 <= len(first) <= 5
        and isinstance(first[0], bytes | bytearray)
    ):
        images: list[ImageInput] = []
        sent: list[tuple[str, str]] = []
        ordinals: dict[str, int] = {}
        for item in raw:
            if not isinstance(item, tuple) or not 3 <= len(item) <= 5:
                continue
            data, label, panel = item[0], item[1], item[2]
            if not isinstance(data, bytes | bytearray) or not data:
                continue
            ordinals[str(panel)] = ordinals.get(str(panel), 0) + 1
            named = item[3] if len(item) >= 4 else None
            frame_id = (
                str(named)
                if isinstance(named, str) and named
                else f"{panel}_{ordinals[str(panel)]:02d}"
            )
            # An empty detail means "whatever this request is set to", which is
            # what almost every still wants. A still that names one is a moment
            # whose answer lives in a few pixels; see `HIGH_DETAIL_PHASES`.
            asked = item[4] if len(item) >= 5 else None
            detail = str(asked) if isinstance(asked, str) and asked else ""
            images.append(
                ImageInput(
                    data=bytes(data),
                    label=str(label),
                    frame_id=frame_id,
                    **({"detail": detail} if detail else {}),
                )
            )
            sent.append((str(panel), str(label)))
        return tuple(images), tuple(sent)
    if isinstance(first, bytes | bytearray):
        frames = tuple(
            bytes(item) for item in raw if isinstance(item, bytes | bytearray) and item
        )
        return image_inputs(frames), ()
    return (), ()


def _images_sent_detail(job: _Job, images: tuple[ImageInput, ...]) -> str:
    """How many stills were handed to the provider, and which view each was."""

    if job.capture is None:
        sources = "none"
    else:
        sources = (
            ", ".join(
                f"{camera.camera_id}={camera.source}:kept={len(camera.frames)}"
                for camera in job.capture.cameras
            )
            or "none"
        )
    counts: dict[str, int] = {}
    for image in images:
        key = image.label or "unlabeled"
        counts[key] = counts.get(key, 0) + 1
    labeled = ", ".join(f"{label}:{count}" for label, count in counts.items()) or "none"
    return f"images_sent={len(images)}; labels={labeled}; sources={sources}"


def _metrics_detail(capture: SwingCapture, present: bool) -> str:
    """Why the All Values still is or is not part of this request."""

    still = capture.metrics
    if still is None:
        return "COACH_METRICS_CAPTURE_ENABLED is off or no COACH_METRICS_SOURCE is set"
    if present:
        return (
            f"source={still.source}; {still.width}x{still.height}; "
            f"offset={still.offset_seconds}s"
        )
    return f"source={still.source}; {still.detail or 'the still could not be read'}"


_Unrecorded = tuple["_Job", AdviceFailure, str, ProviderReply | None]
"""A failure that was reported but whose row has not been written yet."""


class AnalysisService:
    """Owns the diagnosis, the queue behind it, and the model beyond that."""

    def __init__(
        self,
        publish: Publish,
        provider: AdviceProvider | None = None,
        frames: FrameSource | None = None,
        capture_stills: CaptureStillReader | None = None,
        record: Record | None = None,
        enabled: bool = True,
        send_frames: bool = False,
        frame_offsets: Sequence[float] = DEFAULT_OFFSETS,
        camera_view: CameraView = CameraView.UNKNOWN,
        preferred_focus: CoachingFocus | None = None,
        severity_fine: float = SEVERITY_FINE,
        baseline_window: int = 40,
        queue_size: int = 2,
        clip_wait_seconds: float = 8.0,
        timeout_seconds: float = 8.0,
        total_budget_seconds: float = 20.0,
        retry_limit: int = 1,
        min_interval_seconds: float = 8.0,
        session_call_limit: int = 60,
        max_output_tokens: int = 120,
        visual_cues: bool = True,
        max_images: int = 6,
        cache_capacity: int = 128,
        drain_timeout_seconds: float = 5.0,
        backoff_initial_seconds: float = 1.0,
        backoff_max_seconds: float = 8.0,
        trigger: str = TRIGGER_GSPRO,
        protee_max_output_tokens: int = 10000,
        protee_timeout_seconds: float | None = None,
        protee_budget_seconds: float | None = None,
        reasoning_effort: str | None = None,
        record_coach: RecordCoach | None = None,
        evidence_sheet: SheetRenderer = render_evidence_sheet,
        diagnostic_dry_run: bool = False,
        monotonic: Callable[[], float] = time.monotonic,
        now: Now = lambda: datetime.now(UTC),
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if trigger not in (TRIGGER_PROTEE, TRIGGER_GSPRO):
            raise ValueError(f"trigger must be protee or gspro, not {trigger!r}")
        self._dry_run = diagnostic_dry_run
        self._protee = trigger == TRIGGER_PROTEE
        self._protee_max_output_tokens = protee_max_output_tokens
        self._reasoning_effort = reasoning_effort or None
        self._record_coach = record_coach
        self._render_sheet = evidence_sheet
        self._club: Club | None = None
        self._handedness = Handedness.UNKNOWN
        self._strike_trigger: str | None = None
        self._publish = publish
        self._provider = provider
        self._frames = frames
        self._capture_stills = capture_stills
        self._record = record
        self._enabled = enabled
        # Either pipeline is enough. A bay with no camera of its own still has
        # the launch monitor's replay window, and that is the normal case here.
        self._send_frames = send_frames and (
            frames is not None or capture_stills is not None
        )
        self._frame_offsets = tuple(frame_offsets)
        self._camera_view = camera_view
        self._preferred = preferred_focus
        self._fine = severity_fine
        self._clip_wait = clip_wait_seconds
        self._timeout = timeout_seconds
        self._budget = total_budget_seconds
        self._protee_timeout = (
            timeout_seconds
            if protee_timeout_seconds is None
            else protee_timeout_seconds
        )
        self._protee_budget = (
            total_budget_seconds
            if protee_budget_seconds is None
            else protee_budget_seconds
        )
        # The provider call in flight, so the next strike can stop it rather
        # than leave its own shot queued behind a reply nobody will show.
        self._in_flight: tuple[UUID, asyncio.Timeout] | None = None
        self._retry_limit = retry_limit
        self._min_interval = min_interval_seconds
        self._call_limit = session_call_limit
        self._max_output_tokens = max_output_tokens
        self._visual_cues = visual_cues
        self._max_images = max_images
        self._target_distance_m: float | None = None
        self.visual_cues_chosen = 0
        self.cues_admitted = 0
        self._drain_timeout = drain_timeout_seconds
        self._backoff_initial = backoff_initial_seconds
        self._backoff_max = backoff_max_seconds
        self._monotonic = monotonic
        self._now = now
        self._sleep = sleep

        self._baseline = Baseline(window=baseline_window)
        self._session = CoachingSession()
        self._cache = AdviceCache(capacity=cache_capacity)
        self._queue: asyncio.Queue[_Job | object] = asyncio.Queue(maxsize=queue_size)
        self._jobs: dict[UUID, _Job] = {}
        self._seen: OrderedDict[UUID, None] = OrderedDict()
        self._recent: deque[Advice] = deque(maxlen=_RECENT_LINES)
        self._worker: asyncio.Task[None] | None = None
        self._closed = False
        self._provider_closed = False
        self._current: UUID | None = None
        # The one capture on disk, held until a job claims it. One slot,
        # because there is exactly one capture directory and the next swing
        # replaces it whole. It usually arrives before the shot does: the
        # replay is on screen seconds before GSPro has finished flying the
        # ball, so the job it belongs to does not exist yet.
        self._capture: SwingCapture | None = None
        # Failures the event path reported but was not allowed to write. See
        # `_flush_unrecorded`.
        self._unrecorded: deque[_Unrecorded] = deque(maxlen=8)

        self.shots_diagnosed = 0
        self.corrections = 0
        self.reinforcements = 0
        self.lines_from_model = 0
        self.lines_from_template = 0
        self.calls = 0
        self.retries = 0
        self.rejected = 0
        self.failures = 0
        self.dropped = 0
        self.images_sent = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.last_error: str | None = None
        self._last_call_monotonic: float | None = None

    # --- lifecycle ---------------------------------------------------------

    @property
    def is_available(self) -> bool:
        """Whether a model will actually be asked anything."""

        return self._enabled and self._provider is not None

    @property
    def is_coaching(self) -> bool:
        """Whether the next shot will be diagnosed and worded at all.

        True with no model provider configured, because the deterministic tier
        names the fault and words it from a template, and the floor shows the
        line either way. That is a working CaddieJ rather than a degraded one,
        which is why `is_available` — the narrower question of whether a model
        will be asked — is the wrong predicate for anything reporting to a
        player or an assistant.
        """

        return self._enabled and self._worker is not None

    @property
    def preferred_focus(self) -> CoachingFocus | None:
        """What the player said they are working on, or None for no preference."""

        return self._preferred

    def set_preferred_focus(self, focus: CoachingFocus | None) -> None:
        """Change what coaching leans towards, from now on.

        Safe to call at any time, and the reason it is safe is worth stating,
        because this is the only thing outside CaddieJ that can change how
        coaching behaves. It replaces one reference, read once per shot when
        `diagnose` picks between measured faults. Nothing recomputes, nothing is
        cancelled, no queue is touched and no shot already in flight changes: a
        shot is diagnosed against whatever the preference was when it arrived.
        The worst a badly timed call can do is make the next shot lean somewhere
        the one before it did not, which is what changing your mind looks like.

        `NONE` is not a preference — it is the name for a shot with nothing
        wrong — so callers wanting no preference pass None.
        """

        previous = self._preferred
        if focus is CoachingFocus.NONE:
            focus = None
        self._preferred = focus
        if focus is not previous:
            logger.info(
                "coaching_focus_changed",
                extra={"coaching_focus": "none" if focus is None else focus.value},
            )

    async def open(self) -> None:
        """Start the worker and decide whether a model is usable. Never raises.

        A missing key, a missing package or no configured provider at all are
        supported ways to run CaddieJ, not failures to start it. The
        deterministic tier runs regardless, so the floor still names faults.
        """

        if not self._enabled:
            logger.info("coaching_disabled")
            return

        if self._dry_run:
            logger.warning(
                "coaching_diagnostic_dry_run_enabled",
                extra={
                    "coaching_detail": (
                        "COACHING_DIAGNOSTIC_DRY_RUN: evidence and the sheet are "
                        "prepared for every shot and no provider request is made"
                    ),
                },
            )
        if self._provider is None:
            logger.info("coaching_deterministic_only")
        elif not self._provider.is_configured:
            logger.warning(
                "coaching_provider_unconfigured",
                extra={"coaching_provider": self._provider.name},
            )
            self._provider = None
        else:
            logger.info(
                "coaching_ready",
                extra={
                    "coaching_provider": self._provider.name,
                    "coaching_model": self._provider.model,
                    "coaching_frames": self._send_frames,
                    "coaching_trigger": self.trigger,
                },
            )
        if self._protee:
            logger.info(
                "coaching_trigger_protee",
                extra={
                    "coaching_trigger": TRIGGER_PROTEE,
                    "coaching_detail": (
                        "coaching runs from each ProTee strike, its swing capture "
                        "and the All Values still; GSPro is not required"
                    ),
                },
            )

        self._worker = asyncio.create_task(self._work(), name="coaching-worker")

    async def aclose(self) -> None:
        """Finish what is in flight, then stop. Leaves nothing running."""

        # Set before anything else, so that a shot arriving during shutdown is
        # declined rather than half handled: diagnosed and queued for a worker
        # that is on its way out, then displaced by the next one and recorded as
        # superseded by a shot that never existed.
        self._closed = True
        if self._worker is None:
            await self._close_provider()
            return

        await self._queue.put(_STOP)
        try:
            await asyncio.wait_for(self._worker, self._drain_timeout)
        except TimeoutError:
            # A job waiting on a model that shutdown means it will never hear
            # from. Shutdown is not negotiable.
            logger.warning(
                "coaching_drain_timed_out",
                extra={"coaching_queued": self._queue.qsize()},
            )
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        self._worker = None
        # Anything the event path handed over and the worker did not reach.
        # Bounded by the deque, and history's own drain is still ahead of this
        # in the shutdown order.
        await self._flush_unrecorded()
        # After the worker, so nothing is mid-call, and last because a failure
        # here must not cost the flush above.
        await self._close_provider()

    async def _close_provider(self) -> None:
        """Let go of the model's connection pool.

        `AdviceProvider` has said "called once, at shutdown" since it was
        written and nothing called it, so the HTTPS pool behind the OpenAI
        client stayed open for the life of the process — sockets held against
        a host CaddieJ has finished talking to, and a documented contract that
        was not kept. Best effort: the session is ending either way.
        """

        # The provider itself is kept, not dropped: `health` names the model
        # that was in use, and a session that has finished is still a session
        # somebody wants to read the numbers for.
        if self._provider is None or self._provider_closed:
            return
        self._provider_closed = True
        try:
            await self._provider.aclose()
        except Exception:
            logger.warning("coaching_provider_close_failed", exc_info=True)

    # --- the event path ----------------------------------------------------

    @property
    def trigger(self) -> str:
        """What starts a coaching job: the ProTee strike, or a GSPro shot."""

        return TRIGGER_PROTEE if self._protee else TRIGGER_GSPRO

    def subscribe(self, bus: EventBus) -> None:
        bus.subscribe(ShotCompleted, self.on_shot_completed, name="coaching")
        bus.subscribe(SwingClipReady, self.on_clip_ready, name="coaching")
        bus.subscribe(SwingClipFailed, self.on_clip_failed, name="coaching")
        bus.subscribe(SwingFramesReady, self.on_swing_frames, name="coaching")
        bus.subscribe(TargetDistanceObserved, self.on_target_distance, name="coaching")
        if self._protee:
            bus.subscribe(ShotDetected, self.on_shot_detected, name="coaching")
            bus.subscribe(ClubSelected, self.on_club_selected, name="coaching")
            bus.subscribe(
                HandednessChanged, self.on_handedness_changed, name="coaching"
            )

    async def on_shot_detected(self, event: ShotDetected) -> None:
        """A new strike: from now on, anything about an earlier one is stale."""

        if not self._protee or not self._enabled or self._closed:
            return
        if event.trigger_id and event.trigger_id == self._strike_trigger:
            return
        self._strike_trigger = event.trigger_id or None
        self._current = event.event_id
        in_flight = self._in_flight
        if in_flight is not None and in_flight[0] != event.event_id:
            # Only a strike does this. Ball ready, a club change or a GSPro shot
            # never reach here, so they never cost the line being worded.
            in_flight[1].reschedule(asyncio.get_running_loop().time())
            logger.info(
                "coaching_request_cancelled",
                extra={
                    "shot_id": str(in_flight[0]),
                    "coaching_strike_id": str(in_flight[0]),
                    "coaching_reason": AdviceFailure.SUPERSEDED.value,
                    "coaching_detail": (
                        f"strike {event.trigger_id or event.event_id} replaced it"
                    ),
                },
            )
        logger.info(
            "coaching_strike_detected",
            extra={
                "coaching_strike_id": str(event.event_id),
                "shot_trigger_id": event.trigger_id,
                "coaching_trigger": TRIGGER_PROTEE,
                "coaching_detail": (
                    "coaching starts when swing capture publishes this strike's "
                    "frames and All Values still; GSPro is not required"
                ),
            },
        )

    async def on_club_selected(self, event: ClubSelected) -> None:
        self._club = event.club

    async def on_handedness_changed(self, event: HandednessChanged) -> None:
        self._handedness = event.handedness

    async def on_shot_completed(self, event: ShotCompleted) -> None:
        """Diagnose the shot and publish the verdict. Touches nothing slow."""

        if self._protee:
            # GSPro's shot is history's and the floor's. Coaching on this path
            # already has the strike, its pictures and ProTee's own numbers.
            logger.info(
                "coaching_shot_ignored",
                extra={
                    "shot_id": str(event.shot.shot_id),
                    "shot_trigger_id": event.trigger_id or "",
                    "coaching_reason": "protee_trigger",
                    "coaching_detail": (
                        "COACHING_TRIGGER=protee coaches the ProTee strike; "
                        "the GSPro shot is recorded but not coached twice"
                    ),
                },
            )
            return

        if not self._enabled or self._closed:
            logger.info(
                "coaching_shot_ignored",
                extra={
                    "shot_id": str(event.shot.shot_id),
                    "coaching_reason": "disabled" if not self._enabled else "closed",
                    "coaching_detail": "coaching did not diagnose this shot",
                },
            )
            return

        shot = event.shot
        if shot.shot_id in self._seen:
            # The same shot twice: one diagnosis, one line, one call, one row.
            #
            # Checked here and not after diagnosing, which is where it used to
            # be. Everything below this point changes what CaddieJ believes
            # about the session — the club's baseline, the sticky focus and
            # its trend, the counters — and a duplicate did all of it twice.
            # A trend that has folded the same swing in twice is no longer a
            # description of the shots leading up to this one.
            logger.info(
                "coaching_shot_ignored",
                extra={
                    "shot_id": str(shot.shot_id),
                    "coaching_reason": "duplicate",
                    "coaching_detail": "this shot was already diagnosed",
                },
            )
            return
        baseline = self._baseline.for_club(club_of(shot))
        progress = self._session.progress
        club = club_of(shot)
        preferred = self._preferred_for(club)
        diagnosis = diagnose(
            shot,
            baseline,
            preferred,
            self._fine,
            progress,
            target_distance_m=self._target_distance_m,
        )
        if diagnosis.cue is not None:
            variant = self._session.speech_variant(diagnosis.focus, diagnosis.cue)
            if variant >= 2:
                matched = next(
                    (
                        observation
                        for observation in diagnosis.observations
                        if observation.focus is diagnosis.focus
                    ),
                    None,
                )
                diagnosis = replace(
                    diagnosis,
                    speech=speech_for(
                        diagnosis.focus,
                        True
                        if matched is None
                        else is_positive(diagnosis.focus, matched.label),
                        diagnosis.kind,
                        diagnosis.improving,
                        variant=variant,
                    )
                    or diagnosis.speech,
                )
        self.cues_admitted = len(diagnosis.admissible_cues)
        # Both after diagnosing, never before. A shot included in its own
        # baseline is by definition average, which would make every first shot
        # unremarkable, and a trend that already holds this shot has nothing
        # left to say about whether it was an improvement on the ones before.
        self._baseline.remember(shot)
        self._session.remember(diagnosis)

        self.shots_diagnosed += 1
        if diagnosis.kind is AdviceKind.CORRECTION:
            self.corrections += 1
        elif diagnosis.kind is AdviceKind.REINFORCEMENT:
            self.reinforcements += 1
        self._current = diagnosis.shot_id
        logger.info(
            "coaching_shot_diagnosed",
            extra={
                "shot_id": str(diagnosis.shot_id),
                "coaching_stage": STAGE_CANDIDATE,
                "coaching_focus": diagnosis.focus.value,
                "coaching_kind": diagnosis.kind.value,
                "coaching_improving": diagnosis.improving,
                "coaching_focus_shots": diagnosis.focus_shots,
                "coaching_severity": round(diagnosis.severity, 3),
                "coaching_baseline_shots": diagnosis.baseline_shots,
                "shot_trigger_id": event.trigger_id or "",
                "coaching_cue": "" if diagnosis.cue is None else diagnosis.cue.value,
            },
        )
        await self._publish(ShotDiagnosed(diagnosis=diagnosis))
        session = self._session.progress
        logger.info(
            "coaching_session_updated",
            extra={
                "shot_id": str(diagnosis.shot_id),
                "shot_trigger_id": event.trigger_id or "",
                "coaching_focus": (
                    "none" if session.focus is None else session.focus.value
                ),
                "coaching_focus_shots": session.focus_shots,
                "coaching_kind": diagnosis.kind.value,
            },
        )

        self._remember_seen(diagnosis.shot_id)
        await self._enqueue(diagnosis, progress, event.trigger_id)

    async def on_clip_ready(self, event: SwingClipReady) -> None:
        """Hand a clip to the job waiting for it, if one still is."""

        job = self._jobs.get(event.shot_id)
        if job is None or not job.wants_frames:
            return
        job.clip = event.clip
        job.footage.set()

    async def on_clip_failed(self, event: SwingClipFailed) -> None:
        """Release a waiting job at once. It will coach from the numbers.

        Without this, a shot that was never filmed would sit out the whole clip
        wait before finding out, which is most of the window in which its advice
        is still worth showing.
        """

        job = self._jobs.get(event.shot_id)
        if job is None or not job.wants_frames:
            return
        job.footage.set()

    async def on_swing_frames(self, event: SwingFramesReady) -> None:
        """Hold the newest swing capture, and give it to its job if one waits.

        Almost always the other way round: ProTee puts the replay on screen
        while GSPro is still flying the ball, so the capture is finished
        before the shot that explains it has been observed at all. The one
        slot is claimed by whichever job arrives with a matching trigger.
        """

        if self._protee:
            await self._on_protee_capture(event.capture)
            return
        self._capture = event.capture
        job = self._job_for_trigger(event.capture.trigger_id)
        if job is not None:
            job.offer_capture(event.capture)

    async def _on_protee_capture(self, capture: SwingCapture) -> None:
        """Queue one job for this strike. Touches nothing slow."""

        strike = capture.coaching_id
        trigger = capture.trigger_id or ""
        reason = ""
        if not self._enabled:
            reason = "disabled"
        elif self._closed:
            reason = "closed"
        elif (
            capture.strike_id is not None
            and self._current is not None
            and strike != self._current
        ):
            reason = "superseded"
        elif strike in self._seen:
            reason = "duplicate"
        if reason:
            logger.info(
                "coaching_shot_ignored",
                extra={
                    "shot_id": str(strike),
                    "coaching_strike_id": str(strike),
                    "shot_trigger_id": trigger,
                    "coaching_reason": reason,
                    "coaching_detail": "this capture was not coached",
                },
            )
            return
        self._remember_seen(strike)
        if self._current is None:
            self._current = strike
        club = ClubKind.UNKNOWN if self._club is None else self._club.kind
        job = _Job(
            diagnosis=Diagnosis(
                shot_id=strike,
                focus=CoachingFocus.NONE,
                severity=0.0,
                display="",
                speech="",
                club=club,
                handedness=self._handedness,
                baseline_shots=0,
            ),
            queued_monotonic=self._monotonic(),
            progress=self._session.progress,
            trigger_id=trigger or None,
            capture=capture,
            protee=True,
        )
        job.footage.set()
        logger.info(
            "coaching_job_queued",
            extra={
                "shot_id": str(strike),
                "coaching_strike_id": str(strike),
                "shot_trigger_id": trigger,
                "coaching_trigger": TRIGGER_PROTEE,
                "swing_capture_id": str(capture.capture_id),
            },
        )
        await self._put(job)

    async def on_target_distance(self, event: TargetDistanceObserved) -> None:
        self._target_distance_m = event.distance_raw

    def _job_for_trigger(self, trigger_id: str) -> _Job | None:
        if not trigger_id:
            return None
        for job in self._jobs.values():
            if job.wants_frames and job.trigger_id == trigger_id:
                return job
        return None

    async def _enqueue(
        self,
        diagnosis: Diagnosis,
        progress: SessionProgress,
        trigger_id: str | None = None,
    ) -> None:
        """Take a job, displacing an older one if the queue is full."""

        job = _Job(
            diagnosis=diagnosis,
            queued_monotonic=self._monotonic(),
            progress=progress,
            # Frames for a good shot as well as a bad one. Seeing what produced
            # a strike the player should repeat is the whole value of a
            # reinforcement, and a camera that only filmed the bad ones would
            # make the coach's memory of the session one-sided.
            wants_frames=(
                self._send_frames
                and diagnosis.is_worth_saying
                and diagnosis.club is not ClubKind.PUTTER
            ),
            wants_clip=self._frames is not None,
            trigger_id=trigger_id,
        )
        if (
            job.wants_frames
            and trigger_id
            and self._capture is not None
            and self._capture.trigger_id == trigger_id
        ):
            # Already on disk and already this swing's. Attached now so a bay
            # with no camera does not wait out the clip budget for footage
            # nothing is going to produce.
            job.offer_capture(self._capture)
        await self._put(job)

    async def _put(self, job: _Job) -> None:
        """Queue a job, displacing the oldest waiting one if the queue is full."""

        while True:
            try:
                self._queue.put_nowait(job)
            except asyncio.QueueFull:
                try:
                    stale = self._queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover - racing with nobody
                    continue
                if stale is _STOP:
                    # Shutdown got here first. Putting the marker back — there is
                    # room, this loop just took something out — because eating it
                    # would leave the worker waiting for a stop that never comes
                    # and shutdown burning its whole drain budget before giving
                    # up on it.
                    self._queue.put_nowait(_STOP)
                    return
                if isinstance(stale, _Job):
                    self.dropped += 1
                    self._log_request_skipped(
                        stale,
                        "superseded",
                        "displaced by a newer shot before it was analysed",
                    )
                    await self._fail(
                        stale,
                        AdviceFailure.SUPERSEDED,
                        "displaced by a newer shot before it was analysed",
                        # This runs on the bus worker. Writing the row here
                        # means awaiting `record_advice`, which waits for the
                        # shot to reach the disk — up to the history drain
                        # timeout — from a handler documented as touching no
                        # disk at all. The row is worth keeping, so it is
                        # handed to the worker instead of being dropped.
                        store=False,
                    )
                continue
            self._jobs[job.shot_id] = job
            return

    # --- the worker --------------------------------------------------------

    async def _work(self) -> None:
        """Take jobs one at a time until told to stop. Must not raise."""

        while True:
            item = await self._queue.get()
            await self._flush_unrecorded()
            if item is _STOP:
                return
            if not isinstance(item, _Job):
                continue
            try:
                await self._process(item)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # A bug in here must not reach the supervisor, which would read
                # it as grounds to shut CaddieJ down over a coaching line.
                logger.exception(
                    "coaching_worker_error", extra={"shot_id": str(item.shot_id)}
                )
                try:
                    await self._fail(item, AdviceFailure.PROVIDER_ERROR, error)
                except Exception:
                    # Failing to report a failure is still not worth raising
                    # from the last handler there is. It happens when the bus
                    # has already stopped, which is to say during shutdown.
                    logger.exception(
                        "coaching_worker_error_unreported",
                        extra={"shot_id": str(item.shot_id)},
                    )
            finally:
                self._jobs.pop(item.shot_id, None)

    async def _process(self, job: _Job) -> None:
        if job.protee:
            await self._process_protee(job)
            return
        diagnosis = job.diagnosis
        template = from_diagnosis(diagnosis, self._now())

        if not diagnosis.is_worth_saying:
            # Nothing about this shot could be measured, so there is nothing to
            # ask about and nothing to pay for. Not the same as a good shot,
            # which is a reinforcement and does go to the model.
            detail = _insufficient_detail(diagnosis)
            self._log_evidence(job)
            self._log_request_skipped(job, "insufficient_evidence", detail)
            self._log_advice_suppressed(job, detail)
            self._log_speech_not_sent(job, "insufficient_evidence")
            await self._settle(job, template, AdviceStatus.TEMPLATE)
            return

        if self._provider is None:
            detail = "no provider is configured; the templated floor line stands"
            self._log_evidence(job)
            self._log_request_skipped(job, "no_provider", detail)
            self._log_speech_not_sent(job, "template_only")
            await self._settle(job, template, AdviceStatus.TEMPLATE)
            return

        if job.wants_frames:
            await self._wait_for_footage(job)

        if self._is_stale(job):
            self._log_evidence(job)
            self._log_request_skipped(
                job, "superseded", "a newer shot arrived before analysis"
            )
            await self._fail(job, AdviceFailure.SUPERSEDED, "a newer shot arrived")
            return

        images, offsets = await self._stills(job)
        self._log_evidence(job, images=len(images))
        allowed = admissible_cues(
            diagnosis,
            has_frames=bool(images),
            capture=job.capture,
            visual_cues=self._visual_cues,
            fine=self._fine,
        )
        if not allowed:
            # Pictures did not unlock a visual cue and the numbers did not
            # support a measured one. Silence rather than an invented lesson.
            # The templated floor line from ShotDiagnosed still stands.
            detail = (
                "no admissible cue; measured evidence did not support one "
                "and pictures did not unlock a visual one"
            )
            self._log_request_skipped(job, "no_admissible_cue", detail)
            self._log_speech_not_sent(job, "no_admissible_cue")
            await self._settle(job, template, AdviceStatus.TEMPLATE)
            return
        job.diagnosis = replace(
            diagnosis,
            admissible_cues=allowed,
            cue=preferred_cue(diagnosis, allowed) or diagnosis.cue,
        )
        diagnosis = job.diagnosis
        key = fingerprint(
            fingerprint_parts(diagnosis, self._provider.model, bool(images))
        )
        cached = self._cache.get(key, diagnosis.shot_id, self._now())
        if cached is not None:
            self._log_request_skipped(
                job,
                "cache_hit",
                "a validated line for this evidence was already stored",
            )
            await self._settle(
                job, cached, AdviceStatus.OK, cache_hit=True, fingerprint_value=key
            )
            return

        if self._dry_run:
            self._log_request_skipped(job, _DRY_RUN, _DRY_RUN_DETAIL)
            self._log_speech_not_sent(job, _DRY_RUN)
            await self._settle(job, template, AdviceStatus.TEMPLATE)
            return

        refusal = self._blocked()
        if refusal is not None:
            self._log_request_skipped(job, refusal.value, "no call was made")
            await self._fail(job, refusal, "no call was made")
            return

        await self._ask(job, key, images, offsets)

    # --- ProTee-only coaching ----------------------------------------------

    async def _process_protee(self, job: _Job) -> None:
        """One request: swing frames plus the All Values still, read and coached.

        Every exit says why in the log and writes a row, so "no coach and no
        explanation" cannot happen.
        """

        capture = job.capture
        assert capture is not None
        if self._provider is None:
            await self._no_coach(
                job,
                "no_provider",
                "ProTee-only coaching reads the All Values still through the "
                "model, and no provider is configured",
            )
            return
        if self._is_stale(job):
            self._log_request_skipped(
                job, "superseded", "a newer strike arrived before analysis"
            )
            await self._fail(job, AdviceFailure.SUPERSEDED, "a newer strike arrived")
            return

        # Every picture travels as one cell of one Swing Evidence Sheet. The
        # originals on disk stay the evidence; the sheet is how they travel.
        cells = self._protee_cells(job)
        sheet = await self._protee_sheet(job, cells)
        if self._is_stale(job):
            self._log_request_skipped(
                job, "superseded", "a newer strike arrived before analysis"
            )
            await self._fail(job, AdviceFailure.SUPERSEDED, "a newer strike arrived")
            return
        images: list[ImageInput] = []
        listed: tuple[SheetCell, ...] = ()
        sent: tuple[tuple[str, str], ...] = ()
        if sheet is not None:
            images.append(ImageInput(data=sheet.data, label=SHEET_LABEL, detail="high"))
            listed = sheet.cells
            sent = ((SHEET_PANEL_ID, SHEET_LABEL),)
            metrics_sent = sheet.has_metrics
        else:
            # No sheet could be drawn. The numbers can still be read, so the
            # All Values still goes alone; no swing picture means every
            # checkpoint is null, which is the honest consequence.
            metrics_data = await read_metrics_still(capture)
            metrics_cell = next(
                (cell for cell in cells if cell.kind is CellKind.METRICS), None
            )
            metrics_sent = metrics_data is not None and metrics_cell is not None
            if metrics_data is not None and metrics_cell is not None:
                images.append(
                    ImageInput(
                        data=metrics_data,
                        label=METRICS_LABEL,
                        detail="high",
                        frame_id=metrics_cell.cell_id,
                    )
                )
                listed = (metrics_cell,)
                sent = ((METRICS_PANEL_ID, METRICS_LABEL),)
        pictures = tuple(cell for cell in listed if cell.kind is not CellKind.METRICS)
        job.cell_map = tuple((cell.cell_id, cell.frame_id) for cell in listed)
        job.frame_ids = tuple(cell.frame_id for cell in listed)
        job.swing_images = len(pictures)
        metrics_detail = _metrics_detail(capture, metrics_sent)
        job.metrics_status = (
            "not_configured"
            if capture.metrics is None
            else ("captured" if metrics_sent else "unavailable")
        )
        self._log_protee_evidence(job, len(pictures), metrics_sent, metrics_detail)
        self._log_sheet(job, cells, sheet)
        if not images:
            await self._no_coach(
                job,
                "no_evidence",
                f"no swing frame and no All Values still; {metrics_detail}",
            )
            return

        # The schema offers measured cues only. A mechanical lesson is
        # CaddieJ's, chosen from the checkpoint scores after the reply, so the
        # model is never asked to name one.
        cues = vx.possible_cues(job.diagnosis.club, has_metrics=metrics_sent)
        views = frame_views(capture)
        body = any(
            views.get(cell.frame_id) in CHECKPOINT_VIEW_WORDS for cell in pictures
        )
        if not cues and not body:
            await self._no_coach(
                job,
                "no_admissible_cue",
                "the All Values still was not captured and no body camera sent "
                "a frame to score",
            )
            return
        if self._dry_run:
            note_provider_images(
                capture,
                (),
                metadata={
                    "evidence_sheet": (
                        {"ok": False}
                        if sheet is None
                        else {"ok": True, **sheet.metadata()}
                    ),
                    "coach_request": {
                        "strike_id": str(job.shot_id),
                        "diagnostic_dry_run": True,
                        "sent": False,
                        "would_send_images": len(images),
                        "swing_frames": len(pictures),
                        "metrics_image": metrics_sent,
                        "evidence_cells": len(listed),
                    },
                },
            )
            self._log_request_skipped(job, _DRY_RUN, _DRY_RUN_DETAIL)
            await self._no_coach(job, _DRY_RUN, _DRY_RUN_DETAIL)
            return
        refusal = self._blocked()
        if refusal is not None:
            self._log_request_skipped(job, refusal.value, "no call was made")
            await self._fail(job, refusal, "no call was made")
            return

        assert self._provider is not None
        progress = job.progress
        cell_views = {
            cell.cell_id: cell.view for cell in pictures if cell.kind is CellKind.BODY
        }
        request = vx.build_request(
            evidence=vx.render_evidence(
                club=job.diagnosis.club,
                handedness=job.diagnosis.handedness,
                has_metrics=metrics_sent,
                swing_images=tuple(cell.label or cell.title for cell in pictures),
                cues=cues,
                session_focus=self._preferred_for(job.diagnosis.club),
                focus_shots=progress.focus_shots,
                recent_lines=recent_lines(tuple(self._recent)),
                frames=tuple(
                    (cell.cell_id, cell.label or cell.title) for cell in listed
                ),
                sheet=sheet is not None,
                cell_views=cell_views,
            ),
            images=images,
            cues=cues,
            max_output_tokens=self._protee_max_output_tokens,
            timeout_seconds=self._protee_timeout,
            reasoning_effort=self._reasoning_effort,
            frame_ids=tuple(cell.cell_id for cell in listed),
            body_frame_ids=tuple(cell.cell_id for cell in pictures),
            cell_views=cell_views,
        )
        self.images_sent += len(images)
        note_provider_images(
            capture,
            sent,
            frame_ids=job.frame_ids,
            metadata={
                "provider_swing_frame_count": len(pictures),
                "evidence_sheet": (
                    {"ok": False} if sheet is None else {"ok": True, **sheet.metadata()}
                ),
                "coach_request": {
                    "strike_id": str(job.shot_id),
                    "prompt_version": vx.PROTEE_PROMPT_VERSION,
                    "transport": (
                        "evidence_sheet" if sheet is not None else "all_values_only"
                    ),
                    "swing_frames": len(pictures),
                    "metrics_image": metrics_sent,
                    "evidence_cells": len(listed),
                    "images": len(images),
                },
            },
        )
        focus = self._preferred_for(job.diagnosis.club) or CoachingFocus.NONE
        await self._publish(
            AnalysisStarted(
                shot_id=job.shot_id,
                focus=focus,
                frames=len(images),
                model=self._provider.model,
            )
        )
        logger.info(
            "coaching_request_started",
            extra={
                "shot_id": str(job.shot_id),
                "coaching_strike_id": str(job.shot_id),
                "shot_trigger_id": job.trigger_id or "",
                "coaching_trigger": TRIGGER_PROTEE,
                "coaching_model": self._provider.model,
                "coaching_frames": len(pictures),
                "coaching_images": len(images),
                "coaching_timeout_seconds": self._protee_timeout,
                "coaching_budget_seconds": round(self._remaining(job), 2),
                "coach_metrics_status": job.metrics_status,
                "coaching_detail": (
                    f"{_images_sent_detail(job, tuple(images))}; "
                    f"cues={len(cues)}; one request, no separate OCR call"
                ),
            },
        )
        await self._progress(job, CoachingStage.ANALYZING)
        reply = await self._call(job, request)
        if reply is None:
            return
        await self._progress(job, CoachingStage.COACHING)
        self._absorb(reply)
        # From here on the reply names original frames, never sheet cells.
        body_cells = [cell for cell, frame in job.cell_map if frame != METRICS_FRAME_ID]
        named = vx.decode_wire(reply.payload, body_cells)
        reply = replace(reply, payload=vx.canonical_reply(named, dict(job.cell_map)))
        await self._conclude_protee(job, reply, len(images), metrics_sent)

    def _protee_cells(self, job: _Job) -> tuple[SheetCell, ...]:
        """The pictures this shot's sheet carries: the swing and panel stills
        within `max_images`, and the All Values still on top of them."""

        capture = job.capture
        assert capture is not None
        swing = (
            self._send_frames
            and self._capture_stills is not None
            and job.diagnosis.club is not ClubKind.PUTTER
        )
        return plan_cells(capture, self._max_images if swing else 0)

    async def _protee_sheet(
        self, job: _Job, cells: tuple[SheetCell, ...]
    ) -> EvidenceSheet | None:
        """The composed sheet, or None. A failure here never costs the shot."""

        capture = job.capture
        if capture is None or not cells:
            return None
        try:
            return await self._render_sheet(capture, cells)
        except Exception as error:
            self.last_error = f"{type(error).__name__}: {error}"
            logger.warning(
                "coaching_frames_unavailable",
                extra={"shot_id": str(job.shot_id)},
                exc_info=error,
            )
            return None

    async def _conclude_protee(
        self, job: _Job, reply: ProviderReply, images: int, metrics_sent: bool
    ) -> None:
        """Read the metrics, diagnose them, and accept or refuse the coach line."""

        capture = job.capture
        assert capture is not None
        still = capture.metrics
        metrics = vx.parse_metrics(
            reply.payload.get("metrics") if metrics_sent else None,
            image=(
                None if still is None or still.path is None else still.path.as_posix()
            ),
            captured_at=None if still is None else still.captured_at,
        )
        job.metrics = metrics if metrics_sent else None
        if metrics_sent:
            job.metrics_status = "read" if metrics.available else "unreadable"
        logger.info(
            "coach_metrics_read",
            extra={
                "shot_id": str(job.shot_id),
                "coaching_strike_id": str(job.shot_id),
                "shot_trigger_id": job.trigger_id or "",
                "coach_metrics_status": job.metrics_status,
                **vx.metrics_summary(metrics),
            },
        )
        frame_phases = vx.parse_frames(reply.payload.get("frames"))
        assessments, rejected = vx.parse_checkpoints(
            reply.payload.get("checkpoints"),
            body_frames=tuple(
                frame for frame in job.frame_ids if frame != METRICS_FRAME_ID
            ),
            frame_phases=frame_phases,
            frame_views=frame_views(capture),
        )
        job.checkpoints = assessments
        logger.info(
            "coach_frames_identified",
            extra={
                "shot_id": str(job.shot_id),
                "coaching_strike_id": str(job.shot_id),
                "shot_trigger_id": job.trigger_id or "",
                # What the model says it was looking at, against what CaddieJ
                # aimed at. A camera calibration that has drifted shows up
                # here as disagreement, which is the whole point of asking.
                "coach_frames_seen": ",".join(
                    f"{frame}={phase.value}" for frame, phase in frame_phases.items()
                ),
            },
        )
        if metrics_sent or assessments:
            job.swing_score = score_swing(
                metrics,
                club=None if self._club is None else self._club.kind,
                handedness=job.diagnosis.handedness,
                checkpoints=assessments,
            )
            self._log_swing_score(job, job.swing_score)
        mechanics = None if job.swing_score is None else job.swing_score.mechanics
        self._log_checkpoints(job, assessments, rejected, mechanics)

        diagnosis = job.diagnosis
        shot = vx.shot_from_metrics(
            metrics,
            shot_id=job.shot_id,
            club=self._club,
            handedness=diagnosis.handedness,
            observed_at=self._now(),
            observed_monotonic=self._monotonic(),
        )
        if shot is not None:
            club = club_of(shot)
            diagnosis = diagnose(
                shot,
                self._baseline.for_club(club),
                self._preferred_for(club),
                self._fine,
                job.progress,
            )
            self._baseline.remember(shot)
        allowed: tuple[CueId, ...] = ()
        if diagnosis.kind is not AdviceKind.NONE:
            allowed = admissible_cues(
                diagnosis,
                has_frames=job.swing_images > 0,
                capture=capture,
                visual_cues=False,
                fine=self._fine,
            )
        signs = _protee_signs(metrics if metrics_sent else None, diagnosis)
        faults = corroborate(infer_faults(mechanics), signs)
        tracker = self._session.lesson
        root = rank_root_cause(faults, session_fault=tracker.fault)
        step = (
            LessonStep(progress=None)
            if mechanics is None or not self._visual_cues
            else tracker.step(mechanics, faults, root)
        )
        if step.cue is not None and self._visual_cues:
            if cue_supported(step.cue, capture):
                allowed = (*allowed, step.cue)
            else:
                self._log_lesson_unsupported(job, step.cue)
        authority = authority_cue(diagnosis, allowed, step, root)
        review = SwingReview(
            faults=faults,
            root=root,
            progress=step.progress,
            deltas=tracker.deltas(mechanics),
            protee=signs,
            source=(
                LessonSource.NONE
                if authority is None
                else LessonSource.MECHANICAL
                if authority.from_visual
                else LessonSource.MEASURED
            ),
            previous_score=tracker.previous_score,
        )
        tracker.remember_swing(
            mechanics, None if job.swing_score is None else job.swing_score.value
        )
        job.review = review if mechanics is not None else None
        job.authority = authority
        self._log_review(job, review, mechanics is not None)
        if authority is not None and authority.from_visual:
            self._log_cause_preferred(job, diagnosis, authority)
        diagnosis = replace(diagnosis, admissible_cues=allowed)
        if authority is not None:
            diagnosis = promoted_diagnosis(diagnosis, authority)
        job.diagnosis = diagnosis
        self.cues_admitted = len(allowed)
        self._session.remember(diagnosis)
        self.shots_diagnosed += 1
        if diagnosis.kind is AdviceKind.CORRECTION:
            self.corrections += 1
        elif diagnosis.kind is AdviceKind.REINFORCEMENT:
            self.reinforcements += 1
        logger.info(
            "coaching_shot_diagnosed",
            extra={
                "shot_id": str(job.shot_id),
                "coaching_strike_id": str(job.shot_id),
                "shot_trigger_id": job.trigger_id or "",
                "coaching_stage": STAGE_CANDIDATE,
                "coaching_focus": diagnosis.focus.value,
                "coaching_kind": diagnosis.kind.value,
                "coaching_severity": round(diagnosis.severity, 3),
                "coaching_baseline_shots": diagnosis.baseline_shots,
                "coaching_cue": "" if diagnosis.cue is None else diagnosis.cue.value,
                "coaching_detail": f"admissible={len(allowed)}",
            },
        )
        session = self._session.progress
        logger.info(
            "coaching_session_updated",
            extra={
                "shot_id": str(job.shot_id),
                "shot_trigger_id": job.trigger_id or "",
                "coaching_focus": (
                    "none" if session.focus is None else session.focus.value
                ),
                "coaching_focus_shots": session.focus_shots,
                "coaching_kind": diagnosis.kind.value,
            },
        )

        coach = vx.coach_payload(reply.payload)
        if (
            coach is None
            or coach.get("kind") == AdviceKind.NONE.value
            or not coach.get("cue")
        ):
            await self._protee_fallback(
                job,
                reply,
                images,
                "model_declined",
                "the model judged the evidence too thin for a cue",
                authority=authority,
            )
            return
        chosen = str(coach.get("cue"))
        if _needs_reconciling(chosen, diagnosis, allowed, authority):
            await self._reconcile_cue(
                job, reply, images, coach, diagnosis, chosen, authority
            )
            return
        try:
            advice = validate(
                coach,
                diagnosis,
                self._now(),
                frames_sent=job.swing_images > 0,
                fine=self._fine,
                frame_ids=job.frame_ids,
            )
        except SpeechRejected as error:
            self.rejected += 1
            await self._cue_speech_fallback(
                job, reply, images, coach, diagnosis, allowed, str(error), authority
            )
            return
        except AdviceRejected as error:
            self.rejected += 1
            await self._protee_fallback(
                job,
                reply,
                images,
                AdviceFailure.INVALID_REPLY.value,
                str(error),
                authority=authority,
            )
            return
        if advice.cue is None or advice.cue not in allowed:
            self.rejected += 1
            await self._protee_fallback(
                job,
                reply,
                images,
                AdviceFailure.INVALID_REPLY.value,
                f"cue {advice.cue} is not supported by the numbers read",
                authority=authority,
            )
            return
        if self._is_stale(job):
            await self._fail(
                job,
                AdviceFailure.SUPERSEDED,
                "answered after the next strike",
                reply=reply,
            )
            return
        self._recent.append(advice)
        await self._settle(job, advice, AdviceStatus.OK, reply=reply, images=images)

    def _log_cause_preferred(
        self, job: _Job, diagnosis: Diagnosis, authority: CueAuthority
    ) -> None:
        """One line for the moment a seen cause outranked a measured result.

        Worth its own event rather than a detail string: "the player was told
        about their plane when every number was inside its band" is the single
        behaviour Coach V3 exists to produce, and it has to be greppable in a
        hardware log without reading the surrounding twenty lines.
        """

        logger.info(
            "coaching_cause_preferred",
            extra={
                "shot_id": str(job.shot_id),
                "coaching_strike_id": str(job.shot_id),
                "shot_trigger_id": job.trigger_id or "",
                "coaching_cue": authority.cue.value,
                "coaching_focus": CUES_BY_ID[authority.cue].focus.value,
                "coaching_kind": diagnosis.kind.value,
                "coach_lesson_fault": (
                    "" if authority.fault is None else authority.fault.value
                ),
                "coach_visual_frame": authority.frame_id or "",
                "coaching_detail": (
                    "a seen swing fault outranked the measured line "
                    f"({diagnosis.kind.value}, severity "
                    f"{round(diagnosis.severity, 2)})"
                ),
            },
        )

    async def _reconcile_cue(
        self,
        job: _Job,
        reply: ProviderReply,
        images: int,
        coach: Mapping[str, Any],
        diagnosis: Diagnosis,
        chosen: str,
        authority: CueAuthority | None = None,
    ) -> None:
        """CaddieJ's cue, when the model chose one the numbers did not admit.

        The request cannot offer only the admissible cues, because there are
        none yet: the model reads the All Values grid in the same reply that
        coaches it, and CaddieJ diagnoses the readings only once they are
        back. So the model chooses from every cue the grid could support and is
        then judged against the few these numbers do. A cue outside them is
        not a broken reply. It is a second opinion that lost, and the lesson
        is CaddieJ's — focus, cue, headline and observation.

        **The words do not survive.** They used to, when they could have been
        written for CaddieJ's cue: the sentence went through the same checks a
        reply naming that cue would have faced, and was kept if it passed.
        That was wrong in a way the checks could not catch. A sentence written
        about an out-to-in path and filed under scatter passes every rule --
        it is one instruction, it is on a family the focus owns, it does not
        state the opposite of the cue -- and it is still a sentence about the
        wrong lesson. "Feel the club come more from the inside." under the
        heading REPEAT YOUR TEMPO is two different pieces of coaching stapled
        together, and the player gets to decide which one CaddieJ meant.

        So once the deterministic authority has chosen the cue, the cue owns
        all of it: display, observation, and the spoken line, every one of
        them reviewed and written beside each other in the registry. The
        model's contribution to a reconciled shot is its evidence -- the
        metrics it read and the criteria it rated -- which is the part it is
        actually better at than CaddieJ.

        The other way in is a cue the numbers *do* admit that is nonetheless
        not the lesson: the model confirming a centred strike while the
        pictures it was given show a move worth changing. That is the same
        substitution for the same reason — CaddieJ has decided what this shot
        is about — and it takes the same path.

        `validate` itself still refuses the model's cue; this only decides
        what happens next.
        """

        cue = diagnosis.cue
        assert cue is not None
        refused = (
            f"cue {chosen[:40]} was outranked by a seen swing fault"
            if authority is not None and authority.from_visual
            else f"cue {chosen[:40]} was not admissible on this shot"
        )
        line = cue_speech(cue)
        if line is None:
            self.rejected += 1
            self._log_cue_reconciled(
                job, chosen, cue, "reviewed_words", f"{refused}; no reviewed line"
            )
            await self._protee_fallback(
                job,
                reply,
                images,
                "cue_reconciled",
                f"{refused}; cue {cue.value} has no reviewed line",
                authority=authority,
            )
            return
        try:
            advice = validate(
                {
                    **coach,
                    "cue": cue.value,
                    # The reviewed sentence, and no observation in front of
                    # it: the registry's line stands alone and the model's
                    # description was written about a different fault.
                    "speech": line,
                    "observation": None,
                    "evidence_frame_id": _cue_frame(
                        job, cue, None if authority is None else authority.frame_id
                    ),
                    **_promoted_confidence(cue, authority),
                },
                diagnosis,
                self._now(),
                frames_sent=job.swing_images > 0,
                fine=self._fine,
                frame_ids=job.frame_ids,
            )
        except AdviceRejected as error:
            self.rejected += 1
            self._log_cue_reconciled(
                job, chosen, cue, "reviewed_words", f"{refused}; {error}"
            )
            await self._protee_fallback(
                job,
                reply,
                images,
                "cue_reconciled",
                f"{refused}; {error}",
                authority=authority,
            )
            return
        advice = replace(
            advice,
            source=AdviceSource.TEMPLATE,
            spoken_observation=CUES_BY_ID[cue].explanation,
        )
        detail = f"{refused}; {cue.value} said in its own reviewed words"
        self._log_cue_reconciled(job, chosen, cue, "reviewed_words", detail)
        if self._is_stale(job):
            await self._fail(
                job,
                AdviceFailure.SUPERSEDED,
                "answered after the next strike",
                reply=reply,
            )
            return
        self._recent.append(advice)
        await self._settle(
            job,
            advice,
            AdviceStatus.OK,
            reply=reply,
            images=images,
            detail=f"cue_reconciled: {detail}"[:300],
        )

    def _log_cue_reconciled(
        self, job: _Job, chosen: str, authority: CueId, words: str, detail: str
    ) -> None:
        logger.info(
            "coaching_cue_reconciled",
            extra={
                "shot_id": str(job.shot_id),
                "coaching_strike_id": str(job.shot_id),
                "shot_trigger_id": job.trigger_id or "",
                "coaching_model_cue": chosen[:40],
                "coaching_cue": authority.value,
                "coaching_focus": CUES_BY_ID[authority].focus.value,
                "coaching_reason": words,
                "coaching_detail": detail[:300],
            },
        )

    async def _cue_speech_fallback(
        self,
        job: _Job,
        reply: ProviderReply,
        images: int,
        coach: Mapping[str, Any],
        diagnosis: Diagnosis,
        allowed: Sequence[CueId],
        detail: str,
        authority: CueAuthority | None = None,
    ) -> None:
        """The model's cue with that cue's reviewed sentence, when only the words
        were refused. Anything else wrong with the cue is the floor-only path."""

        reason = AdviceFailure.INVALID_REPLY.value
        try:
            line = cue_speech(CueId(str(coach.get("cue"))))
        except ValueError:
            line = None
        advice: Advice | None = None
        if line is not None:
            try:
                # The reviewed sentence stands alone, so the model's own
                # observation is not carried in front of it.
                advice = validate(
                    {**coach, "speech": line, "observation": None},
                    diagnosis,
                    self._now(),
                    frames_sent=job.swing_images > 0,
                    fine=self._fine,
                    frame_ids=job.frame_ids,
                )
            except AdviceRejected:
                advice = None
        if advice is None or advice.cue not in allowed:
            await self._protee_fallback(
                job, reply, images, reason, detail, authority=authority
            )
            return
        if self._is_stale(job):
            await self._fail(
                job,
                AdviceFailure.SUPERSEDED,
                "answered after the next strike",
                reply=reply,
            )
            return
        advice = _scored(
            replace(advice, source=AdviceSource.TEMPLATE, swing_score=job.swing_score),
            job,
        )
        logger.info(
            "coaching_speech_fallback",
            extra={
                "shot_id": str(job.shot_id),
                "coaching_strike_id": str(job.shot_id),
                "shot_trigger_id": job.trigger_id or "",
                "coaching_reason": reason,
                "coaching_detail": detail[:300],
                "coaching_cue": "" if advice.cue is None else advice.cue.value,
                "coaching_focus": advice.focus.value,
                "coaching_display": advice.display,
                "coaching_evidence_frame_id": advice.evidence_frame_id or "",
            },
        )
        self._recent.append(advice)
        await self._publish(
            AnalysisCompleted(
                shot_id=job.shot_id, advice=advice, evidence=_evidence(job, advice)
            )
        )
        await self._settle(
            job,
            advice,
            AdviceStatus.TEMPLATE,
            reply=reply,
            images=images,
            detail=f"cue_speech: {detail}"[:300],
        )

    async def _protee_fallback(
        self,
        job: _Job,
        reply: ProviderReply,
        images: int,
        reason: str,
        detail: str,
        authority: CueAuthority | None = None,
    ) -> None:
        """CaddieJ's own lesson from the numbers read, in full, or nothing.

        The model's words were not usable, and that is all that failed. The
        readings, the diagnosis and the cue are CaddieJ's and are as sound as
        they would have been with a perfect reply, so the player is given the
        whole of the cue's reviewed lesson — what was seen, the one thing to
        do, the still it came from — and George says the cue's sentence.
        Leaving the instruction alone on the floor and the room silent told a
        player who had just watched twenty-six numbers appear that the coach
        had nothing to say, which was never true.

        Only a cue with no reviewed sentence, or one this shot does not admit,
        falls back to the bare floor line. That is the one case with nothing
        reviewed to say, and saying nothing is better than inventing it.
        """

        diagnosis = job.diagnosis
        if self._is_stale(job):
            await self._fail(job, AdviceFailure.SUPERSEDED, "a newer strike arrived")
            return
        if not diagnosis.is_worth_saying or not diagnosis.display:
            await self._no_coach(job, reason, detail, reply=reply, images=images)
            return
        advice = from_cue(
            diagnosis,
            self._now(),
            frames_sent=job.swing_images > 0,
            fine=self._fine,
            frame_ids=job.frame_ids,
            evidence_frame_id=(
                None
                if diagnosis.cue is None
                else _cue_frame(
                    job,
                    diagnosis.cue,
                    None if authority is None else authority.frame_id,
                )
            ),
            visual_confidence=(None if authority is None else authority.confidence),
        )
        logger.info(
            "coaching_advice_fallback",
            extra={
                "shot_id": str(job.shot_id),
                "coaching_strike_id": str(job.shot_id),
                "shot_trigger_id": job.trigger_id or "",
                "coaching_reason": reason,
                "coaching_detail": detail[:300],
                "coaching_cue": "" if diagnosis.cue is None else diagnosis.cue.value,
                "coaching_display": (
                    diagnosis.display if advice is None else advice.display
                ),
                "coaching_observed": "" if advice is None else advice.observation,
                "coaching_evidence_frame_id": (
                    "" if advice is None else advice.evidence_frame_id or ""
                ),
            },
        )
        if advice is None:
            await self._publish(
                ShotDiagnosed(
                    diagnosis=diagnosis,
                    swing_score=job.swing_score,
                    metrics=strip_metrics(job.metrics),
                )
            )
            self._log_speech_not_sent(job, "no_reviewed_cue_line")
            await self._settle(
                job,
                from_diagnosis(diagnosis, self._now()),
                AdviceStatus.TEMPLATE,
                reply=reply,
                images=images,
                detail=f"{reason}: {detail}"[:300],
            )
            return
        advice = _scored(advice, job)
        self._recent.append(advice)
        await self._publish(
            AnalysisCompleted(
                shot_id=job.shot_id, advice=advice, evidence=_evidence(job, advice)
            )
        )
        await self._settle(
            job,
            advice,
            AdviceStatus.TEMPLATE,
            reply=reply,
            images=images,
            detail=f"{reason}: {detail}"[:300],
        )

    async def _no_coach(
        self,
        job: _Job,
        reason: str,
        detail: str,
        reply: ProviderReply | None = None,
        images: int = 0,
    ) -> None:
        """Nothing is shown and nothing is said, and the log says why."""

        logger.info(
            "coaching_no_advice",
            extra={
                "shot_id": str(job.shot_id),
                "coaching_strike_id": str(job.shot_id),
                "shot_trigger_id": job.trigger_id or "",
                "coaching_reason": reason,
                "coaching_detail": detail[:300],
            },
        )
        self._log_speech_not_sent(job, reason)
        if not self._is_stale(job):
            # Lets the floor take down a "thinking" placeholder, and log why,
            # and still shows the score when the readings produced one.
            await self._publish(
                ShotDiagnosed(
                    diagnosis=job.diagnosis,
                    swing_score=job.swing_score,
                    metrics=strip_metrics(job.metrics),
                )
            )
        self.lines_from_template += 1
        await self._store(
            job,
            from_diagnosis(job.diagnosis, self._now()),
            AdviceStatus.TEMPLATE,
            reply=reply,
            images=images,
            detail=f"{reason}: {detail}"[:300],
        )

    async def _progress(self, job: _Job, stage: CoachingStage) -> None:
        """Tell the floor where this job is. Only ever on the way past.

        The bus delivers this without waiting for anyone, so it cannot delay
        the request it announces; a stale job announcing itself is the floor's
        to ignore, by the same currency it applies to the line.
        """

        await self._publish(
            CoachingProgress(
                stage=stage, trigger_id=job.trigger_id or "", shot_id=job.shot_id
            )
        )

    def _log_swing_score(self, job: _Job, score: SwingScore) -> None:
        logger.info(
            "coaching_swing_score",
            extra={
                "shot_id": str(job.shot_id),
                "coaching_strike_id": str(job.shot_id),
                "shot_trigger_id": job.trigger_id or "",
                "swing_score": score.value,
                "swing_score_rubric": score.rubric_version,
                "swing_score_weight": score.available_weight,
                "swing_score_club": "" if score.club is None else score.club.value,
                "swing_score_components": _component_summary(score),
                "swing_score_absent": (
                    ",".join(item.component.value for item in score.absent) or "none"
                ),
            },
        )

    def _ids(self, job: _Job) -> dict[str, object]:
        return {
            "shot_id": str(job.shot_id),
            "coaching_strike_id": str(job.shot_id),
            "shot_trigger_id": job.trigger_id or "",
        }

    def _log_checkpoints(
        self,
        job: _Job,
        assessments: Sequence[CheckpointAssessment],
        rejected: Sequence[tuple[str, str]],
        mechanics: MechanicalScore | None,
    ) -> None:
        """`coach_checkpoints_assessed` and `coach_phase_scores`. Keys, numbers
        and reasons only, never the model's observations."""

        logger.info(
            "coach_checkpoints_assessed",
            extra={
                **self._ids(job),
                **vx.checkpoints_summary(assessments, rejected),
                "coach_checkpoints_capped": (
                    "none"
                    if mechanics is None or not mechanics.capped
                    else ",".join(item.value for item in mechanics.capped)
                ),
                "coach_club_motion": ",".join(
                    f"{key}={value}" for key, value in club_motion(mechanics).items()
                ),
            },
        )
        if mechanics is None:
            return
        logger.info(
            "coach_phase_scores",
            extra={
                **self._ids(job),
                "swing_score": mechanics.value,
                "coach_phase_scores": phase_summary(mechanics),
                "coach_swing_coverage": mechanics.coverage,
                "coach_swing_confidence": mechanics.confidence,
                "coach_swing_ceiling": (
                    "none"
                    if mechanics.ceiling is None
                    else f"{mechanics.ceiling}:{'+'.join(mechanics.ceiling_reasons)}"
                ),
            },
        )

    def _log_review(self, job: _Job, review: SwingReview, scored: bool) -> None:
        """`coach_faults_inferred`, `coach_root_cause_selected` and
        `coach_lesson_progress`, each one bounded line."""

        if not scored:
            return
        logger.info(
            "coach_faults_inferred",
            extra={
                **self._ids(job),
                "coach_fault_count": len(review.faults),
                "coach_faults": _faults_summary(review.faults),
                "coach_protee_signs": ",".join(item.value for item in review.protee)
                or "none",
            },
        )
        root = review.root
        logger.info(
            "coach_root_cause_selected",
            extra={
                **self._ids(job),
                "coach_root_fault": (
                    "none" if root is None or root.fault is None else root.fault.value
                ),
                "coach_root_reason": "none" if root is None else root.reason,
                "coach_root_ranking": _ranking_summary(root),
                "coach_root_explains": (
                    "none"
                    if root is None or not root.explains
                    else ",".join(item.value for item in root.explains)
                ),
                "coach_lesson_cue": (
                    "" if job.authority is None else job.authority.cue.value
                ),
                "coaching_reason": review.source.value,
            },
        )
        progress = review.progress
        if progress is None:
            return
        logger.info(
            "coach_lesson_progress",
            extra={
                **self._ids(job),
                "coach_lesson_fault": progress.fault.value,
                "coach_lesson_cue": progress.cue.value,
                "coach_lesson_verdict": progress.verdict.value,
                "coach_lesson_mode": progress.mode.value,
                "coach_lesson_score": progress.score,
                "coach_lesson_baseline": progress.baseline,
                "coach_lesson_delta": progress.delta,
                "coach_lesson_improved_shots": progress.improved_shots,
                "coach_lesson_next": (
                    "" if progress.next_fault is None else progress.next_fault.value
                ),
            },
        )

    def _log_lesson_unsupported(self, job: _Job, cue: CueId) -> None:
        logger.info(
            "coach_lesson_unsupported",
            extra={
                **self._ids(job),
                "coach_lesson_cue": cue.value,
                "coaching_detail": (
                    "no configured camera claims to show this lesson, so the "
                    "measured line decides"
                ),
            },
        )

    def _log_sheet(
        self,
        job: _Job,
        cells: Sequence[SheetCell],
        sheet: EvidenceSheet | None,
    ) -> None:
        """`coach_evidence_sheet`: what the one image is, or why there is none."""

        logger.info(
            "coach_evidence_sheet",
            extra={
                **self._ids(job),
                "coach_sheet_status": (
                    "none" if not cells else "ok" if sheet is not None else "failed"
                ),
                "coach_sheet_width": None if sheet is None else sheet.width,
                "coach_sheet_height": None if sheet is None else sheet.height,
                "coach_sheet_bytes": None if sheet is None else len(sheet.data),
                "coach_sheet_cells": (0 if sheet is None else len(sheet.cells)),
                "coach_sheet_dropped": (
                    "none"
                    if sheet is None or not sheet.dropped
                    else ",".join(sheet.dropped)
                ),
                "coach_sheet_render_ms": (
                    None if sheet is None else round(sheet.render_seconds * 1000)
                ),
                "coach_sheet_stored": False if sheet is None else sheet.stored,
            },
        )

    def _log_protee_evidence(
        self, job: _Job, swing: int, metrics: bool, metrics_detail: str
    ) -> None:
        capture = job.capture
        assert capture is not None
        panels = ",".join(
            camera.camera_id for camera in capture.cameras if camera.frames
        )
        logger.info(
            "coaching_evidence_ready",
            extra={
                "shot_id": str(job.shot_id),
                "coaching_strike_id": str(job.shot_id),
                "shot_trigger_id": job.trigger_id or "",
                "coaching_trigger": TRIGGER_PROTEE,
                "evidence_grades": evidence_grade_summary(
                    measured=0, derived=0, trend=0, visual=swing, absent=0
                ),
                "swing_capture_directory": str(capture.directory),
                "coach_metrics_status": job.metrics_status,
                "coaching_images": swing + (1 if metrics else 0),
                "coaching_detail": (
                    f"swing_frames={swing}; panels={panels or 'none'}; "
                    f"all_values={'yes' if metrics else 'no'} ({metrics_detail}); "
                    "gspro=not required"
                ),
            },
        )

    def _log_evidence(self, job: _Job, images: int = 0) -> None:
        """Which evidence this shot actually has, and where it came from."""

        diagnosis = job.diagnosis
        observed = (
            ", ".join(
                f"{item.focus.value}:{item.label}" for item in diagnosis.observations
            )
            or "none"
        )
        missing = ", ".join(diagnosis.unmeasured) or "none"
        if job.capture is None:
            capture = "none"
            directory = ""
            sources = "none"
        else:
            panels = ",".join(
                camera.camera_id for camera in job.capture.cameras if camera.frames
            )
            capture = panels or "empty"
            directory = str(job.capture.directory)
            sources = (
                ", ".join(
                    f"{camera.camera_id}={camera.source}:kept={len(camera.frames)}"
                    for camera in job.capture.cameras
                )
                or "none"
            )
        clip = "none" if job.clip is None else "present"
        logger.info(
            "coaching_evidence_ready",
            extra={
                "shot_id": str(job.shot_id),
                "shot_trigger_id": job.trigger_id or "",
                "evidence_grades": evidence_grade_summary(
                    measured=len(diagnosis.observations),
                    derived=1 if diagnosis.target_distance_m is not None else 0,
                    trend=1 if job.progress.trends else 0,
                    visual=images,
                    absent=len(diagnosis.unmeasured),
                ),
                "coaching_baseline_shots": diagnosis.baseline_shots,
                "swing_capture_directory": directory,
                "swing_capture_sources": sources,
                "coaching_detail": (
                    f"observations={observed}; missing={missing}; "
                    f"baseline_shots={diagnosis.baseline_shots}; "
                    f"capture={capture}; sources={sources}; "
                    f"directory={directory or 'none'}; clip={clip}; "
                    f"images={images}"
                ),
            },
        )

    def _log_request_skipped(self, job: _Job, reason: str, detail: str) -> None:
        logger.info(
            "coaching_request_skipped",
            extra={
                "shot_id": str(job.shot_id),
                "shot_trigger_id": job.trigger_id or "",
                "coaching_reason": reason,
                "coaching_detail": detail,
            },
        )

    def _log_advice_suppressed(self, job: _Job, detail: str) -> None:
        logger.info(
            "coaching_advice_suppressed",
            extra={
                "shot_id": str(job.shot_id),
                "shot_trigger_id": job.trigger_id or "",
                "coaching_reason": "insufficient_evidence",
                "coaching_focus": job.diagnosis.focus.value,
                "coaching_baseline_shots": job.diagnosis.baseline_shots,
                "coaching_detail": detail,
            },
        )

    def _log_speech_not_sent(self, job: _Job, reason: str) -> None:
        """Speech speaks a validated line or a cue's reviewed one. Say when
        neither is coming."""

        logger.info(
            "speech_suppressed",
            extra={
                "shot_id": str(job.shot_id),
                "shot_trigger_id": job.trigger_id or "",
                "coaching_reason": reason,
                "speech_detail": reason,
            },
        )

    async def _wait_for_footage(self, job: _Job) -> None:
        """Wait a bounded time for the clip of this swing.

        The clip does not exist when the shot arrives — the post-impact footage
        has not been recorded yet — so waiting is unavoidable. Doing it here,
        in the worker, is what keeps it from being in the way of anything.
        """

        try:
            async with asyncio.timeout(self._clip_wait):
                await job.footage.wait()
        except TimeoutError:
            logger.info(
                "coaching_footage_not_ready",
                extra={
                    "shot_id": str(job.shot_id),
                    "shot_trigger_id": job.trigger_id or "",
                    "coaching_detail": (
                        "capture and clip did not arrive within the wait; "
                        "coaching continues from the measurements"
                    ),
                },
            )

    async def _stills(
        self, job: _Job
    ) -> tuple[tuple[ImageInput, ...], tuple[float, ...]]:
        """Pull pictures from whichever pipeline produced some for this swing.

        The clip wins when there is one. It is footage from a camera somebody
        placed, so its angle is known, its frame rate is known and impact has
        a position in it; a capture is screenshots of the launch monitor's
        replay and supports a smaller claim. A bay with no camera of its own
        — which is how this one runs — has only the capture, and a smaller
        claim is worth more than none.
        """

        if not self._send_frames:
            return (), ()

        if job.clip is not None and self._frames is not None:
            offsets = offsets_for(job.clip, self._frame_offsets)
            raw = await self._read(job, self._frames.frames(job.clip, offsets))
            images = image_inputs(raw)
            self.images_sent += len(images)
            return images, offsets

        if job.capture is not None and self._capture_stills is not None:
            captured = await self._read_capture(
                job, self._capture_stills(job.capture, self._max_images)
            )
            images, sent = _images_from_capture(captured)
            images = images[: self._max_images]
            sent = sent[: self._max_images]
            self.images_sent += len(images)
            job.frame_ids = vx.sent_frame_ids(images)
            if sent:
                note_provider_images(job.capture, sent, frame_ids=job.frame_ids)
            # No offsets: a replay has no impact time to be relative to, and
            # inventing one is precisely what `_capture_note` tells the model
            # not to do.
            return images, ()

        return (), ()

    async def _read_capture(self, job: _Job, reading: Awaitable[object]) -> object:
        """Await swing-capture stills. Bytes or labeled triples; failure is none."""

        try:
            return await reading
        except Exception as error:
            self.last_error = f"{type(error).__name__}: {error}"
            logger.warning(
                "coaching_frames_unavailable",
                extra={"shot_id": str(job.shot_id)},
                exc_info=error,
            )
            return ()

    async def _read(
        self, job: _Job, reading: Awaitable[tuple[bytes, ...]]
    ) -> tuple[bytes, ...]:
        """Await a picture source, treating any failure as "no pictures"."""

        try:
            return await reading
        except Exception as error:
            # A frame source that fails is a reason to coach from the numbers,
            # not a reason to abandon the shot.
            self.last_error = f"{type(error).__name__}: {error}"
            logger.warning(
                "coaching_frames_unavailable",
                extra={"shot_id": str(job.shot_id)},
                exc_info=error,
            )
            return ()

    async def _ask(
        self,
        job: _Job,
        key: str,
        images: tuple[ImageInput, ...],
        offsets: tuple[float, ...],
    ) -> None:
        """Ask a model for the words, retrying only what a retry could fix."""

        assert self._provider is not None
        diagnosis = job.diagnosis
        evidence = render(
            diagnosis,
            recent_lines=recent_lines(tuple(self._recent)),
            preferred=self._preferred_for(diagnosis.club),
            clip=job.clip,
            frames=len(images),
            frame_offsets=offsets,
            progress=job.progress,
            camera_view=self._camera_view,
            capture=job.capture,
        )
        request = build_request(
            diagnosis,
            evidence,
            images=images,
            max_output_tokens=self._max_output_tokens,
            timeout_seconds=self._timeout,
            cues=diagnosis.admissible_cues,
        )
        await self._publish(
            AnalysisStarted(
                shot_id=diagnosis.shot_id,
                focus=diagnosis.focus,
                frames=len(images),
                model=self._provider.model,
            )
        )
        # How many stills and never what is in them. This is the line that says
        # whether the vision path is actually running on a real machine, which
        # cannot otherwise be told apart from a model doing well on the numbers.
        logger.info(
            "coaching_request_started",
            extra={
                "shot_id": str(diagnosis.shot_id),
                "shot_trigger_id": job.trigger_id or "",
                "coaching_model": self._provider.model,
                "coaching_frames": len(images),
                "coaching_images": len(images),
                "coaching_timeout_seconds": self._timeout,
                "coaching_cue": "" if diagnosis.cue is None else diagnosis.cue.value,
                "coaching_detail": _images_sent_detail(job, images),
            },
        )

        await self._progress(job, CoachingStage.ANALYZING)
        reply = await self._call(job, request)
        if reply is None:
            return
        await self._progress(job, CoachingStage.COACHING)

        try:
            advice = validate(
                reply.payload,
                diagnosis,
                self._now(),
                frames_sent=bool(images),
                fine=self._fine,
                frame_ids=job.frame_ids,
            )
        except AdviceRejected as error:
            fallback = self._measured_fallback(job.diagnosis, error)
            if fallback is not None:
                self._log_speech_not_sent(job, "model_reply_unused")
                await self._settle(
                    job,
                    fallback,
                    AdviceStatus.TEMPLATE,
                    reply=reply,
                    images=len(images),
                    fingerprint_value=key,
                )
                return
            self.rejected += 1
            await self._fail(job, AdviceFailure.INVALID_REPLY, error, reply=reply)
            return

        if self._is_stale(job):
            await self._fail(
                job,
                AdviceFailure.SUPERSEDED,
                "answered after the next shot",
                reply=reply,
            )
            return

        self._cache.put(key, advice)
        self._recent.append(advice)
        self._absorb(reply)
        await self._settle(
            job,
            advice,
            AdviceStatus.OK,
            reply=reply,
            images=len(images),
            fingerprint_value=key,
        )

    async def _call(self, job: _Job, request: AdviceRequest) -> ProviderReply | None:
        """Ask, retrying only what a retry could fix. None means `_fail` ran."""

        assert self._provider is not None
        # Built on the first retry rather than here, so a shot that succeeds or
        # fails outright never depends on the retry policy being constructible.
        backoff: Backoff | None = None
        attempt = 0
        while True:
            # Checked before every attempt, including the first. The gate before
            # `_ask` answers "may this shot be coached at all"; this answers "may
            # this call be made", and a retry is a call. Without it a shot with
            # one call left in the session budget can spend two.
            if self.calls >= self._call_limit:
                await self._fail(
                    job,
                    AdviceFailure.BUDGET_EXHAUSTED,
                    "the session call limit was reached",
                )
                return None
            # The budget is the whole of what this shot gets, so it bounds the
            # call as well as the wait before a retry. A provider that ignores
            # the timeout in the request — the port allows any implementation —
            # would otherwise hold the worker for as long as it liked, and the
            # answer would be worthless by the time it arrived anyway.
            remaining = self._remaining(job)
            if remaining <= 0:
                await self._fail(
                    job,
                    AdviceFailure.TIMED_OUT,
                    "the time worth spending on this shot ran out",
                )
                return None

            if attempt and self._is_stale(job):
                await self._fail(
                    job,
                    AdviceFailure.SUPERSEDED,
                    "a newer strike arrived before the retry",
                )
                return None
            attempt += 1
            self.calls += 1
            self._last_call_monotonic = self._monotonic()
            # One timeout, never two. The provider is given no more than the
            # budget has left, so the SDK's own timeout is what normally ends
            # a slow attempt, and the budget guard below is only a backstop
            # for a provider that ignores it.
            limit = self._timeout_for(job)
            attempt_timeout = max(0.001, min(limit, remaining))
            started = self._monotonic()
            attempt_request = replace(request, timeout_seconds=attempt_timeout)
            try:
                async with asyncio.timeout(remaining) as scope:
                    # The next strike ends this scope early; see
                    # `on_shot_detected`.
                    self._in_flight = (job.shot_id, scope)
                    reply = await self._provider.complete(attempt_request)
                self._log_attempt_completed(
                    job, attempt, started, attempt_timeout, reply
                )
                break
            except TimeoutError as error:
                if self._is_stale(job):
                    await self._fail(
                        job,
                        AdviceFailure.SUPERSEDED,
                        "a newer strike arrived while the model was answering",
                    )
                    return None
                # The budget, not the provider's own timeout: there is by
                # definition nothing left to retry with.
                self._log_attempt_failed(
                    job, attempt, started, attempt_timeout, AdviceFailure.TIMED_OUT
                )
                await self._fail(job, AdviceFailure.TIMED_OUT, error)
                return None
            except ProviderRefusedError as error:
                # Wrong key, unknown model, a refusal. Identical next time.
                await self._fail(job, AdviceFailure.PROVIDER_ERROR, error)
                return None
            except (
                ProviderUnavailableError,
                ProviderRateLimitedError,
                ProviderTimeoutError,
            ) as error:
                reason = _RETRYABLE[type(error)]
                self._log_attempt_failed(job, attempt, started, attempt_timeout, reason)
                remaining = self._remaining(job)
                if attempt > self._retry_limit or self._is_stale(job) or remaining <= 0:
                    await self._fail(job, reason, error)
                    return None
                if (
                    job.protee
                    and isinstance(error, ProviderTimeoutError)
                    and remaining < limit
                ):
                    # A request that already had its full time is not retried
                    # with less. A short second attempt at a slow multimodal
                    # request mostly times out again and spends a call doing it.
                    await self._fail(
                        job,
                        reason,
                        f"{error}; {remaining:.1f}s left is less than one "
                        f"{limit:g}s attempt, so it was not retried",
                    )
                    return None
                after = getattr(error, "retry_after_seconds", None)
                if backoff is None:
                    backoff = Backoff(self._backoff_initial, self._backoff_max)
                delay = min(after or backoff.next_delay(), remaining)
                self.retries += 1
                logger.info(
                    "coaching_retrying",
                    extra={
                        "shot_id": str(job.shot_id),
                        "coaching_reason": reason.value,
                        "coaching_attempt": attempt,
                        "coaching_elapsed_seconds": round(
                            self._monotonic() - started, 2
                        ),
                        "coaching_timeout_seconds": round(attempt_timeout, 2),
                        "coaching_backoff_seconds": round(delay, 2),
                    },
                )
                await self._sleep(delay)
            except ProviderError as error:
                await self._fail(job, AdviceFailure.PROVIDER_ERROR, error)
                return None
            finally:
                self._in_flight = None
        return reply

    def _timeout_for(self, job: _Job) -> float:
        return self._protee_timeout if job.protee else self._timeout

    def _log_attempt_completed(
        self,
        job: _Job,
        attempt: int,
        started: float,
        timeout: float,
        reply: ProviderReply,
    ) -> None:
        logger.info(
            "coaching_request_completed",
            extra={
                "shot_id": str(job.shot_id),
                "coaching_strike_id": str(job.shot_id),
                "coaching_attempt": attempt,
                "coaching_elapsed_seconds": round(self._monotonic() - started, 2),
                "coaching_timeout_seconds": round(timeout, 2),
                "coaching_usage_input": reply.input_tokens,
                "coaching_usage_output": reply.output_tokens,
                "coaching_usage_reasoning": reply.reasoning_tokens,
            },
        )

    def _log_attempt_failed(
        self,
        job: _Job,
        attempt: int,
        started: float,
        timeout: float,
        reason: AdviceFailure,
    ) -> None:
        logger.warning(
            "coaching_attempt_failed",
            extra={
                "shot_id": str(job.shot_id),
                "coaching_strike_id": str(job.shot_id),
                "coaching_reason": reason.value,
                "coaching_attempt": attempt,
                "coaching_elapsed_seconds": round(self._monotonic() - started, 2),
                "coaching_timeout_seconds": round(timeout, 2),
            },
        )

    def _displays(self, diagnosis: Diagnosis, has_frames: bool) -> set[str]:
        """Every display a reply is allowed to carry, for this shot.

        Per candidate rather than per focus, because which words are legal
        depends on whether that candidate is a fault on this swing: the same
        focus offers "FACE OPEN" when the measurement is outside its band and
        "GOOD FACE" when it is inside one, and offering both would let a reply
        praise a fault.
        """

        permitted: set[str] = set()
        labels = {
            observation.focus: observation.label
            for observation in diagnosis.observations
        }
        for candidate in diagnosis.candidates:
            kind = (
                AdviceKind.CORRECTION
                if candidate.severity >= self._fine
                else AdviceKind.REINFORCEMENT
            )
            permitted |= legal_displays(
                candidate.focus, labels.get(candidate.focus), kind
            )
        if has_frames:
            # Video-only faults, which no measurement ranked. A model that was
            # given pictures may name one; one that was not may not.
            for focus in OBSERVED_FOCUSES:
                permitted |= legal_displays(focus)
        return permitted or {diagnosis.display}

    def _measured_fallback(
        self, diagnosis: Diagnosis, error: AdviceRejected
    ) -> Advice | None:
        """A measured cue when a visual one was chosen without enough certainty."""

        if "visual confidence" not in str(error):
            return None
        measured = tuple(
            cue
            for cue in diagnosis.admissible_cues
            if CUES_BY_ID[cue].grade is not EvidenceGrade.VISUAL
        )
        if not measured:
            return None
        return from_diagnosis(
            replace(
                diagnosis,
                admissible_cues=measured,
                cue=preferred_cue(diagnosis, measured),
            ),
            self._now(),
        )

    def _preferred_for(self, club: ClubKind) -> CoachingFocus | None:
        """What this club should lean towards. Putting is a separate lesson."""

        if club is ClubKind.PUTTER:
            return self._session.putting_focus
        return self._preferred or self._session.focus

    def _current_focus(self) -> CoachingFocus | None:
        """What full-swing coaching should lean towards on the next shot.

        What the player said they are working on, if they said anything, and
        otherwise whatever the session itself has settled on. Configuration
        wins because it is a person stating an intention, and the session's own
        focus is only ever a conclusion drawn from severities.
        """

        return self._preferred or self._session.focus

    # --- outcomes ----------------------------------------------------------

    async def _settle(
        self,
        job: _Job,
        advice: Advice,
        status: AdviceStatus,
        reply: ProviderReply | None = None,
        images: int = 0,
        cache_hit: bool = False,
        fingerprint_value: str | None = None,
        detail: str | None = None,
    ) -> None:
        """Publish the line, if a model wrote it, and record what happened.

        Also where the line becomes the lesson. Whatever the player is shown
        here is what the session follows from now on, whichever candidate the
        diagnosis started from.
        """

        candidate = job.diagnosis
        advice = _scored(advice, job)
        self._reconcile(job, advice)
        if status is AdviceStatus.OK:
            # Counted by who wrote the sentence rather than by which path
            # settled it: a reconciled shot used the reply's evidence but
            # says the registry's words, and belongs on the template side.
            if advice.source is AdviceSource.MODEL:
                self.lines_from_model += 1
            else:
                self.lines_from_template += 1
            if (
                advice.cue is not None
                and advice.cue in CUES_BY_ID
                and CUES_BY_ID[advice.cue].grade is EvidenceGrade.VISUAL
            ):
                self.visual_cues_chosen += 1
            await self._publish(
                AnalysisCompleted(
                    shot_id=job.shot_id, advice=advice, evidence=_evidence(job, advice)
                )
            )
            logger.info(
                "coaching_advice_ready",
                extra={
                    "shot_id": str(job.shot_id),
                    "shot_trigger_id": job.trigger_id or "",
                    "coaching_stage": STAGE_FINAL,
                    "coaching_focus": advice.focus.value,
                    "coaching_candidate_focus": candidate.focus.value,
                    "coaching_candidate_cue": (
                        "" if candidate.cue is None else candidate.cue.value
                    ),
                    "coaching_kind": advice.kind.value,
                    "coaching_observed": advice.observation,
                    "coaching_display": advice.display,
                    "coaching_evidence_frame_id": advice.evidence_frame_id or "",
                    "coaching_cue": "" if advice.cue is None else advice.cue.value,
                    "coaching_confidence": advice.confidence,
                    "coaching_confidence_source": (
                        None
                        if advice.confidence_source is None
                        else advice.confidence_source.value
                    ),
                    "coaching_cache_hit": cache_hit,
                    "coaching_latency_ms": (
                        None if reply is None else round(reply.latency_seconds * 1000)
                    ),
                },
            )
        else:
            self.lines_from_template += 1

        if (
            job.trigger_id
            and self._capture is not None
            and self._capture.trigger_id == job.trigger_id
        ):
            self._capture = None

        await self._store(
            job,
            advice,
            status,
            reply=reply,
            images=images,
            cache_hit=cache_hit,
            fingerprint_value=fingerprint_value,
            detail=detail,
        )

    def _reconcile(self, job: _Job, advice: Advice) -> None:
        """Point the session at the focus the player was actually coached on.

        The candidate and the final line can differ honestly: a slice admits
        both a path cue and a curve cue, and the model chose the one the
        evidence showed best. Narrowing the choice to the candidate would
        throw that judgement away, so instead the session follows the choice,
        and the log says when it had to.
        """

        candidate = job.diagnosis
        moved = self._session.settle(advice)
        if advice.focus is candidate.focus and advice.cue is candidate.cue:
            return
        session = self._session.progress
        logger.info(
            "coaching_focus_reconciled",
            extra={
                "shot_id": str(job.shot_id),
                "shot_trigger_id": job.trigger_id or "",
                "coaching_candidate_focus": candidate.focus.value,
                "coaching_candidate_cue": (
                    "" if candidate.cue is None else candidate.cue.value
                ),
                "coaching_final_focus": advice.focus.value,
                "coaching_final_cue": "" if advice.cue is None else advice.cue.value,
                "coaching_focus": (
                    "none" if session.focus is None else session.focus.value
                ),
                "coaching_focus_shots": session.focus_shots,
                "coaching_detail": (
                    "session focus follows the final line"
                    if moved
                    else "session focus unchanged by the final line"
                ),
            },
        )

    async def _fail(
        self,
        job: _Job,
        reason: AdviceFailure,
        detail: object,
        reply: ProviderReply | None = None,
        store: bool = True,
    ) -> None:
        """Say there is no coached line, and why. The templated one stands."""

        self.failures += 1
        text = str(detail)[:300]
        self.last_error = f"{reason.value}: {text}"
        logger.warning(
            "coaching_unavailable",
            extra={
                "shot_id": str(job.shot_id),
                "coaching_reason": reason.value,
                "coaching_detail": text,
            },
        )
        self._log_speech_not_sent(job, reason.value)
        await self._publish(
            AnalysisFailed(shot_id=job.shot_id, reason=reason, detail=text)
        )
        outcome = (job, reason, text, reply)
        if not store:
            self._unrecorded.append(outcome)
            return
        await self._record_failure(outcome)

    async def _record_failure(self, outcome: _Unrecorded) -> None:
        job, reason, text, reply = outcome
        await self._store(
            job,
            from_diagnosis(job.diagnosis, self._now()),
            AdviceStatus.UNAVAILABLE,
            reply=reply,
            failure=reason,
            detail=text,
        )

    async def _flush_unrecorded(self) -> None:
        """Write the rows the event path was not allowed to wait for.

        Drained by the worker, which is the one place in here that is allowed
        to be slow. Bounded: a session that displaces more shots than this has
        bigger problems than a missing row, and the log already said so.
        """

        while self._unrecorded:
            await self._record_failure(self._unrecorded.popleft())

    async def _store(
        self,
        job: _Job,
        advice: Advice,
        status: AdviceStatus,
        reply: ProviderReply | None = None,
        images: int = 0,
        cache_hit: bool = False,
        failure: AdviceFailure | None = None,
        detail: str | None = None,
        fingerprint_value: str | None = None,
    ) -> None:
        """Hand the outcome to whatever is keeping records, if anything is."""

        advice = _scored(advice, job)
        if job.protee:
            await self._store_coach(job, advice, status, reply, images, failure, detail)
            return
        if self._record is None:
            return

        # A cached line was written by a model, and the fingerprint it was filed
        # under includes that model's name — so the attribution is known even
        # though no call was made for this shot. Dropping it would make the row
        # unreadable next month for the sake of a technicality.
        from_a_model = reply is not None or cache_hit
        model = reply.model if reply is not None else self._model_name(cache_hit)

        outcome = AdviceOutcome(
            advice=advice,
            status=status,
            baseline_shots=job.diagnosis.baseline_shots,
            provider=None if self._provider is None else self._provider.name,
            model=model,
            prompt_version=PROMPT_VERSION if from_a_model else None,
            latency_ms=(None if reply is None else round(reply.latency_seconds * 1000)),
            input_images=images,
            input_tokens=None if reply is None else reply.input_tokens,
            output_tokens=None if reply is None else reply.output_tokens,
            cache_hit=cache_hit,
            failure=failure,
            detail=detail,
            clip_path=None if job.clip is None else str(job.clip.path),
            fingerprint=fingerprint_value,
            capture_id=(None if job.capture is None else str(job.capture.capture_id)),
            panels_used=(
                None
                if job.capture is None
                else ",".join(
                    camera.camera_id for camera in job.capture.cameras if camera.frames
                )
            ),
            evidence_grades=evidence_grade_summary(
                measured=len(job.diagnosis.observations),
                derived=1 if job.diagnosis.target_distance_m is not None else 0,
                trend=1 if job.progress.trends else 0,
                visual=images,
                absent=len(job.diagnosis.unmeasured),
            ),
            evidence_frame_id=advice.evidence_frame_id,
        )
        try:
            await self._record(outcome)
        except Exception as error:
            # Records are useful, not load-bearing. Losing one must not cost the
            # line the player already read.
            self.last_error = f"{type(error).__name__}: {error}"
            logger.warning(
                "coaching_record_failed",
                extra={"shot_id": str(job.shot_id)},
                exc_info=error,
            )

    async def _store_coach(
        self,
        job: _Job,
        advice: Advice,
        status: AdviceStatus,
        reply: ProviderReply | None,
        images: int,
        failure: AdviceFailure | None,
        detail: str | None,
    ) -> None:
        """One `coach_results` row per strike, whatever happened to it."""

        if self._record_coach is None:
            return
        capture = job.capture
        still = None if capture is None else capture.metrics
        if status is AdviceStatus.OK:
            outcome = "ok"
        elif status is AdviceStatus.UNAVAILABLE:
            outcome = "unavailable"
        else:
            outcome = "template" if advice.display else "none"
        shown = outcome in ("ok", "template")
        result = CoachResult(
            strike_id=job.shot_id,
            trigger_id=job.trigger_id,
            capture_id=None if capture is None else capture.capture_id,
            created_at=self._now(),
            status=outcome,
            kind=advice.kind.value if shown else AdviceKind.NONE.value,
            focus=advice.focus.value,
            cue_id=None if advice.cue is None or not shown else advice.cue.value,
            display=advice.display or None if shown else None,
            speech=advice.speech or None if outcome == "ok" else None,
            severity=advice.severity if shown else None,
            confidence=advice.confidence if shown else None,
            confidence_source=(
                None
                if advice.confidence_source is None or not shown
                else advice.confidence_source.value
            ),
            provider=None if self._provider is None else self._provider.name,
            model=None if reply is None else reply.model,
            prompt_version=None if reply is None else vx.PROTEE_PROMPT_VERSION,
            latency_ms=None if reply is None else round(reply.latency_seconds * 1000),
            input_images=images,
            swing_images=job.swing_images,
            input_tokens=None if reply is None else reply.input_tokens,
            output_tokens=None if reply is None else reply.output_tokens,
            metrics_source=None if still is None else still.source,
            metrics_status=job.metrics_status,
            metrics=job.metrics,
            metrics_image=(
                None if still is None or still.path is None else still.path.as_posix()
            ),
            metrics_captured_at=None if still is None else still.captured_at,
            panels_used=(
                None
                if capture is None
                else ",".join(c.camera_id for c in capture.cameras if c.frames)
            ),
            evidence_grades=evidence_grade_summary(
                measured=len(job.diagnosis.observations),
                derived=0,
                trend=1 if job.progress.trends else 0,
                visual=job.swing_images,
                absent=len(job.diagnosis.unmeasured),
            ),
            failure_reason=None if failure is None else failure.value,
            detail=detail,
            swing_score=advice.swing_score,
            review=advice.review,
        )
        try:
            await self._record_coach(result)
        except Exception as error:
            self.last_error = f"{type(error).__name__}: {error}"
            logger.warning(
                "coaching_record_failed",
                extra={"shot_id": str(job.shot_id)},
                exc_info=error,
            )

    def _model_name(self, cache_hit: bool) -> str | None:
        if not cache_hit or self._provider is None:
            return None
        return self._provider.model

    # --- limits ------------------------------------------------------------

    def _blocked(self) -> AdviceFailure | None:
        """Whether a call may be made at all right now."""

        if self.calls >= self._call_limit:
            return AdviceFailure.BUDGET_EXHAUSTED
        last = self._last_call_monotonic
        if last is not None and self._monotonic() - last < self._min_interval:
            # Only reachable from a replay or a bug: nobody hits two balls this
            # close together. It is here so that neither can spend money fast.
            return AdviceFailure.THROTTLED
        return None

    def _is_stale(self, job: _Job) -> bool:
        return self._current is not None and self._current != job.shot_id

    def _remaining(self, job: _Job) -> float:
        budget = self._protee_budget if job.protee else self._budget
        return budget - (self._monotonic() - job.queued_monotonic)

    def _absorb(self, reply: ProviderReply) -> None:
        self.input_tokens += reply.input_tokens or 0
        self.output_tokens += reply.output_tokens or 0

    def _remember_seen(self, shot_id: UUID) -> None:
        self._seen[shot_id] = None
        while len(self._seen) > _SEEN_LIMIT:
            self._seen.popitem(last=False)

    # --- reporting ---------------------------------------------------------

    def health(self) -> AnalysisHealth:
        focus = self._current_focus()
        return AnalysisHealth(
            enabled=self._enabled,
            available=self.is_available,
            provider=None if self._provider is None else self._provider.name,
            model=None if self._provider is None else self._provider.model,
            focus=None if focus is None else focus.value,
            shots_diagnosed=self.shots_diagnosed,
            corrections=self.corrections,
            reinforcements=self.reinforcements,
            lines_from_model=self.lines_from_model,
            lines_from_template=self.lines_from_template,
            calls=self.calls,
            retries=self.retries,
            calls_remaining=max(0, self._call_limit - self.calls),
            rejected=self.rejected,
            failures=self.failures,
            dropped=self.dropped,
            cache_hits=self._cache.hits,
            images_sent=self.images_sent,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            queued=self._queue.qsize(),
            last_error=self.last_error,
            visual_cues_chosen=self.visual_cues_chosen,
            cues_admitted=self.cues_admitted,
        )
