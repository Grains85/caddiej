"""Live shots use the same Ideal Swing path as a saved package.

A capture is not coached until vision calls are explicitly enabled. A failure
publishes no line. The measured fault catalog is not consulted.

The approved correction is copied from the selected rule. A feedback strategy
may place a checked observation in front of that sentence. It does not rewrite
the sentence. The wording call exists only for the `worded` strategy.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from caddiej.coaching.provider import AdviceProvider, ProviderError
from caddiej.domain.coaching import (
    Advice,
    AdviceFailure,
    AdviceKind,
    AdviceSource,
    CoachingFocus,
    SeveritySource,
)
from caddiej.domain.events import (
    AnalysisCompleted,
    AnalysisFailed,
    AnalysisStarted,
    ClubSelected,
    HandednessChanged,
    ShotCompleted,
    SpeechCompleted,
    SpeechStarted,
    SwingFramesReady,
)
from caddiej.domain.speech import SpeechOutcome
from caddiej.swing_capture.store import pin_evidence, unpin_evidence
from caddiej.swing_evidence.ready import readiness
from caddiej.swing_reference.evidence import (
    ShotSuperseded,
    build_live_package,
    release_unselected,
    same_capture,
    timeline_available,
    write_package,
)
from caddiej.swing_reference.feedback import (
    FeedbackBrief,
    FeedbackStrategy,
    comparison_supported,
    compose_feedback,
    parse_feedback,
    uses_wording_call,
)
from caddiej.swing_reference.model import SwingReference, load_reference
from caddiej.swing_reference.package import load_evidence_package
from caddiej.swing_reference.pipeline import AnalysisResult, coach_prepared
from caddiej.swing_reference.request import (
    PlayerContext,
    PreparedRequest,
    PreviousNote,
    build_request,
)
from caddiej.swing_reference.session import new_session
from caddiej.swing_reference.timing import CoachingTrace, latency_report

logger = logging.getLogger(__name__)

_SEVERITY = {"minor": 0.4, "clear": 0.6, "large": 0.8}


@dataclass(frozen=True, slots=True)
class ReferenceCoachHealth:
    enabled: bool
    available: bool
    vision_calls: bool
    provider: str | None
    model: str | None
    image_detail: str
    active_focus: str | None
    shots: int
    presented: int
    failures: int
    last_error: str | None
    feedback: str
    evidence_status: str
    evidence_detail: str


class ReferenceLiveCoach:
    """One coaching implementation for a round.

    `publish` is the bus. Vision calls stay off until `vision_calls` is set,
    and nothing is substituted for them.
    """

    def __init__(
        self,
        publish: Any,
        provider: AdviceProvider | None,
        *,
        enabled: bool = True,
        vision_calls: bool = False,
        image_detail: str = "high",
        evidence_budget: int = 24,
        evidence_allocation: str = "dtl_primary",
        timeout_seconds: float = 30.0,
        call_limit: int = 60,
        reference: SwingReference | None = None,
        context: PlayerContext | None = None,
        feedback: FeedbackStrategy = "observed",
        clock: Callable[[], float] | None = None,
        simulated: bool = False,
        diagnostics: Any = None,
    ) -> None:
        self._publish = publish
        self._provider = provider
        self._enabled = enabled
        self._vision_calls = vision_calls
        self._image_detail = image_detail
        self._evidence_budget = evidence_budget
        self._evidence_allocation = evidence_allocation
        self._timeout = timeout_seconds
        self._call_limit = call_limit
        self._reference = reference
        self._context = context or PlayerContext(evidence_context="player_swing")
        self._feedback: FeedbackStrategy = feedback
        self._monotonic = clock or time.monotonic
        self._simulated = simulated
        self._diagnostics = diagnostics
        self._state = new_session("live")
        self._previous: PreviousNote | None = None
        self._latest: UUID | None = None
        self._calls = 0
        self._presented = 0
        self._failures = 0
        self._last_error: str | None = None
        self._selected: dict[UUID, str] = {}
        self._opened = False
        self._job: asyncio.Task[None] | None = None
        self._running: set[UUID] = set()
        self._side: set[asyncio.Task[None]] = set()
        self._traces: dict[UUID, CoachingTrace] = {}
        self._completed_at: dict[str, float] = {}
        self._trigger_shots: dict[str, UUID] = {}
        self._closed: set[UUID] = set()

    @property
    def is_coaching(self) -> bool:
        return self._enabled and self._vision_calls and self._opened

    @property
    def preferred_focus(self) -> CoachingFocus | None:
        """There is no operator-selected fault. The session focus is a checkpoint."""

        return None

    def set_preferred_focus(self, focus: CoachingFocus | None) -> None:
        """Old fault names are not accepted as a coaching focus."""

        del focus

    async def open(self) -> None:
        self._opened = True
        if self._reference is None:
            self._reference = load_reference()
        if not self._vision_calls:
            logger.info(
                "coaching_vision_calls_disabled",
                extra={
                    "coaching_detail": (
                        "COACHING_VISION_CALLS is false, so a shot is not assessed "
                        "and no other coaching line is spoken"
                    )
                },
            )

    async def aclose(self) -> None:
        self._opened = False
        job = self._job
        if job is not None and not job.done():
            job.cancel()
        await self.settle()
        if self._provider is not None:
            await self._provider.aclose()

    async def settle(self) -> None:
        """Wait until the shot in flight, and any superseded notice, has finished."""

        while True:
            pending = [
                task
                for task in (self._job, *tuple(self._side))
                if task is not None and not task.done()
            ]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    def subscribe(self, bus: Any) -> None:
        bus.subscribe(SwingFramesReady, self.on_frames, name="reference-coach")
        bus.subscribe(SpeechStarted, self.on_speech_started, name="reference-coach")
        bus.subscribe(SpeechCompleted, self.on_speech, name="reference-coach")
        bus.subscribe(ClubSelected, self.on_club, name="reference-coach")
        bus.subscribe(HandednessChanged, self.on_handedness, name="reference-coach")
        bus.subscribe(ShotCompleted, self.on_shot, name="reference-coach")

    def health(self) -> ReferenceCoachHealth:
        provider = self._provider
        focus = self._state.active_focus
        evidence_status, evidence_detail = self._evidence_readiness()
        return ReferenceCoachHealth(
            enabled=self._enabled,
            available=self.is_coaching and provider is not None and provider.is_configured,
            vision_calls=self._vision_calls,
            provider=None if provider is None else provider.name,
            model=None if provider is None else provider.model,
            image_detail=self._image_detail,
            active_focus=None if focus is None else str(focus),
            shots=len(self._state.assessment_shot_ids),
            presented=self._presented,
            failures=self._failures,
            last_error=self._last_error,
            feedback=self._feedback,
            evidence_status=evidence_status,
            evidence_detail=evidence_detail,
        )

    def _evidence_readiness(self) -> tuple[str, str]:
        if not self._enabled or not self._vision_calls:
            return (
                "not_requested",
                "COACHING_VISION_CALLS is false, so the evidence pipeline is not loaded",
            )
        state = readiness()
        return state.status, state.detail

    async def on_frames(self, event: SwingFramesReady) -> None:
        if not self._enabled or not self._vision_calls or self._reference is None:
            return
        capture = event.capture
        shot_id = capture.coaching_id
        if shot_id in self._running or str(shot_id) in self._state.assessment_shot_ids:
            return
        self._latest = shot_id
        self._note_frames(shot_id, capture)
        self._diag("note_capture", capture)
        self._running.add(shot_id)
        previous = self._job
        self._job = asyncio.create_task(
            self._coach_capture(shot_id, capture),
            name=f"reference-coach-{shot_id}",
        )
        if previous is not None and previous is not self._job and not previous.done():
            previous.cancel()

    async def _coach_capture(self, shot_id: UUID, capture: Any) -> None:
        try:
            await self._coach_capture_body(shot_id, capture)
        except asyncio.CancelledError:
            if self._latest != shot_id:
                self._note_superseded(shot_id)
            raise
        finally:
            self._running.discard(shot_id)

    async def _coach_capture_body(self, shot_id: UUID, capture: Any) -> None:
        if self._provider is None or not self._provider.is_configured:
            await self._fail(
                shot_id, AdviceFailure.NO_CREDENTIALS, "model provider is not configured"
            )
            return
        if self._calls >= self._call_limit:
            await self._fail(
                shot_id, AdviceFailure.BUDGET_EXHAUSTED, "session call limit reached"
            )
            return
        try:
            prepared = await asyncio.to_thread(self._prepare, capture)
        except Exception as error:
            detail = f"{type(error).__name__}: {error}"
            logger.warning(
                "reference_evidence_failed",
                extra={"coaching_detail": detail},
            )
            if self._latest != shot_id:
                await self._fail(shot_id, AdviceFailure.SUPERSEDED, "a newer shot arrived")
                return
            await self._fail(shot_id, AdviceFailure.INVALID_REPLY, detail)
            return
        if self._latest != shot_id:
            await self._fail(shot_id, AdviceFailure.SUPERSEDED, "a newer shot arrived")
            return
        if prepared is None:
            if self._latest != shot_id and shot_id not in self._closed:
                await self._fail(shot_id, AdviceFailure.SUPERSEDED, "a newer shot arrived")
            return
        trace = self._trace(shot_id)
        trace.image_count = len(prepared.images)
        trace.image_detail = self._image_detail
        trace.request_type = "vision+wording" if uses_wording_call(self._feedback) else "vision"
        if not prepared.images:
            await self._fail(
                shot_id, AdviceFailure.INVALID_REPLY, "no DTL or FO frames were attached"
            )
            return
        await self._emit(
            AnalysisStarted(
                shot_id=shot_id,
                focus=CoachingFocus.NONE,
                frames=len(prepared.images),
                model=self._provider.model,
            )
        )
        self._calls += 1
        self._mark(shot_id, "vision_started")
        try:
            reply = await self._provider.complete(
                prepared.advice_request(
                    timeout_seconds=self._timeout,
                    image_detail=self._image_detail,
                )
            )
        except ProviderError as error:
            self._mark(shot_id, "vision_received")
            self._diag(
                "note_gpt",
                str(shot_id),
                prepared=prepared,
                reply=None,
                provider=None if self._provider is None else self._provider.name,
                simulated=self._simulated,
                checkpoints=self._visual_checkpoints(),
                image_detail=self._image_detail,
                error=f"{type(error).__name__}: {error}"[:300],
            )
            await self._fail(shot_id, _failure(error), type(error).__name__)
            return
        except Exception:
            self._mark(shot_id, "vision_received")
            logger.exception("reference_coach_failed")
            self._diag(
                "note_gpt",
                str(shot_id),
                prepared=prepared,
                reply=None,
                provider=None if self._provider is None else self._provider.name,
                simulated=self._simulated,
                checkpoints=self._visual_checkpoints(),
                image_detail=self._image_detail,
                error="model request failed",
            )
            await self._fail(shot_id, AdviceFailure.PROVIDER_ERROR, "model request failed")
            return
        self._mark(shot_id, "vision_received")
        self._diag(
            "note_gpt",
            str(shot_id),
            prepared=prepared,
            reply=reply,
            provider=None if self._provider is None else self._provider.name,
            simulated=self._simulated,
            checkpoints=self._visual_checkpoints(),
            image_detail=self._image_detail,
        )
        trace.model = reply.model
        trace.input_tokens = reply.input_tokens
        trace.output_tokens = reply.output_tokens
        trace.cached_tokens = reply.cached_tokens
        if self._latest != shot_id:
            await self._fail(shot_id, AdviceFailure.SUPERSEDED, "a newer shot arrived")
            return
        result = coach_prepared(
            prepared,
            dict(reply.payload),
            self._state,
            sequence=len(self._state.assessment_shot_ids) + 1,
            reference=self._reference,
            on_mark=lambda name: self._mark(shot_id, name),
        )
        if self._latest != shot_id:
            await self._fail(shot_id, AdviceFailure.SUPERSEDED, "a newer shot arrived")
            return
        prior = self._previous
        previous_focus = self._state.active_focus
        approved = result.speech_text
        self._note_decision(shot_id, result)
        if not result.presented:
            self._commit(result)
            self._failures += 1
            self._last_error = result.errors[0] if result.errors else result.reason
            await self._fail(
                shot_id,
                AdviceFailure.INVALID_REPLY,
                self._last_error or "no approved line",
            )
            self._diag_assessment(
                shot_id,
                result,
                approved_correction=approved,
                previous_focus=previous_focus,
                previous=prior,
                observation_included=False,
            )
            return
        result = await self._phrase(shot_id, result, prior)
        if self._latest != shot_id:
            await self._fail(shot_id, AdviceFailure.SUPERSEDED, "a newer shot arrived")
            return
        self._commit(result)
        self._diag_assessment(
            shot_id,
            result,
            approved_correction=approved,
            previous_focus=previous_focus,
            previous=prior,
            observation_included=self._trace(shot_id).observation_included,
        )
        self._closed.add(shot_id)
        self._presented += 1
        self._selected[shot_id] = result.speech_text
        await self._emit(
            AnalysisCompleted(
                shot_id=shot_id,
                advice=_advice(shot_id, result, prepared),
            )
        )
        self._mark(shot_id, "hud_published")
        trace.delivered = True
        self._log_trace(shot_id)

    def _prepare(self, capture: Any) -> PreparedRequest | None:
        reference = self._reference
        if reference is None:
            raise RuntimeError("coach is not open")
        shot_id = capture.coaching_id
        directory = Path(capture.directory)
        self._mark(shot_id, "evidence_started")
        pinned = timeline_available(directory)
        if pinned:
            pin_evidence(directory)
        try:
            return self._prepare_body(capture, reference)
        finally:
            if pinned:
                unpin_evidence(directory)
            self._mark(shot_id, "evidence_completed")

    def _prepare_body(self, capture: Any, reference: SwingReference) -> PreparedRequest | None:
        directory = Path(capture.directory)
        package_path = directory / "evidence_package.json"
        if package_path.is_file():
            package = load_evidence_package(package_path)
            if str(package.shot_id) in self._state.assessment_shot_ids:
                return None
            self._diag("note_evidence", str(capture.coaching_id), package, directory)
            return self._request_from_package(capture, directory, package, reference)
        if not timeline_available(directory):
            raise ValueError("swing evidence needs a diagnostic timeline")
        timings: dict[str, float] = {}
        try:
            package = build_live_package(
                directory,
                shot_id=str(capture.coaching_id),
                budget=self._evidence_budget,
                preset=self._evidence_allocation,
                timings=timings,
                cancelled=lambda: self._latest != capture.coaching_id,
            )
        except ShotSuperseded:
            return None
        self._record_evidence(capture.coaching_id, timings)
        self._diag("note_evidence_timing", str(capture.coaching_id), timings)
        self._note_package(capture, package)
        self._diag("note_evidence", str(capture.coaching_id), package, directory)
        write_package(directory, package)
        prepared = self._request_from_package(capture, directory, package, reference)
        release_unselected(directory, package)
        mirror = getattr(capture, "published", None)
        if (
            isinstance(mirror, Path)
            and mirror.is_dir()
            and mirror.resolve() != directory.resolve()
            and same_capture(mirror, str(capture.capture_id))
        ):
            release_unselected(mirror, package)
        return prepared

    def _request_from_package(
        self,
        capture: Any,
        directory: Path,
        package: Any,
        reference: SwingReference,
    ) -> PreparedRequest:
        self._mark(capture.coaching_id, "request_prepare_started")
        try:
            return build_request(
                package,
                directory,
                reference,
                context=self._context,
                previous=self._previous,
            )
        finally:
            self._mark(capture.coaching_id, "request_prepare_completed")

    def _note_package(self, capture: Any, package: Any) -> None:
        """Record which frames, stages, and impact this shot will be coached from."""

        trace = self._trace(capture.coaching_id)
        trace.capture_id = str(getattr(capture, "capture_id", "") or "")
        trace.selected = tuple(
            f"{frame.reference.camera_id}:{frame.reference.capture_tick}:"
            f"{frame.selection_reason}:{frame.stage or '-'}"
            for frame in package.selected_frames
        )
        trace.stages = tuple(
            f"{stage.camera_id}:{stage.status}:"
            + ",".join(boundary.stage for boundary in stage.boundaries)
            for stage in package.stage_estimates
        )
        trace.unresolved = tuple(
            f"{stage.camera_id}:{name}"
            for stage in package.stage_estimates
            for name in stage.unresolved_stages
        )
        impact = package.impact_estimate
        trace.impact_status = str(impact.status)
        span = impact.contact_interval_ticks
        trace.impact_interval = "" if span is None else f"{span[0]}-{span[1]}"
        logger.info(
            "swing_evidence_ready",
            extra={
                "shot_id": str(capture.coaching_id),
                "swing_capture_id": trace.capture_id,
                "coaching_capture_id": trace.capture_id,
                "coaching_images": len(trace.selected),
                "coaching_selected": list(trace.selected),
                "coaching_stages": list(trace.stages),
                "coaching_unresolved": list(trace.unresolved),
                "coaching_impact": trace.impact_status,
                "coaching_impact_interval": trace.impact_interval,
                "coaching_detail": str(package.fallback),
            },
        )

    def _note_decision(self, shot_id: UUID, result: AnalysisResult) -> None:
        trace = self._trace(shot_id)
        trace.decision = result.decision_type
        trace.rule_id = result.rule_id or ""
        trace.errors = result.errors

    def _record_evidence(self, shot_id: UUID, timings: dict[str, float]) -> None:
        trace = self._trace(shot_id)
        trace.evidence_stages = {
            "timeline_ms": _ms(timings.get("timeline_seconds")),
            "decode_ms": _ms(timings.get("decode_seconds")),
            "motion_ms": _ms(timings.get("motion_seconds")),
            "image_loading_ms": _ms(timings.get("image_loading_seconds")),
            "frame_difference_ms": _ms(timings.get("frame_difference_seconds")),
            "optical_flow_ms": _ms(timings.get("optical_flow_seconds")),
            "sampler_selection_ms": _ms(timings.get("sampler_selection_seconds")),
            "pose_ms": _ms(timings.get("pose_seconds")),
            "impact_ms": _ms(timings.get("impact_seconds")),
            "selection_ms": _ms(timings.get("selection_seconds")),
            "package_ms": _ms(timings.get("package_seconds")),
        }

    async def on_club(self, event: ClubSelected) -> None:
        kind = event.club.kind.value
        if kind in {"iron", "driver", "wedge"}:
            self._context = replace(self._context, club_type=kind)

    async def on_handedness(self, event: HandednessChanged) -> None:
        value = event.handedness.value
        if value in {"right", "left"}:
            self._context = replace(self._context, handedness=value)

    async def on_shot(self, event: ShotCompleted) -> None:
        shot = event.shot
        club = self._context.club_type
        if shot.club is not None and shot.club.kind.value in {"iron", "driver", "wedge"}:
            club = shot.club.kind.value
        handed = self._context.handedness
        if shot.handedness.value in {"right", "left"}:
            handed = shot.handedness.value
        distance = self._context.distance_yards
        if shot.ball is not None and shot.ball.carry_yards is not None:
            distance = shot.ball.carry_yards
        self._context = replace(
            self._context,
            club_type=club,
            handedness=handed,
            distance_yards=distance,
        )
        stamp = self._monotonic()
        self._completed_at[str(shot.shot_id)] = stamp
        if event.trigger_id:
            self._completed_at[event.trigger_id] = stamp
        self._attach_shot_completed(shot.shot_id, event.trigger_id, stamp)
        self._diag(
            "note_simulator_shot",
            shot,
            event.trigger_id,
            monotonic_seconds=stamp,
            occurred_at=event.occurred_at,
        )

    def _commit(self, result: AnalysisResult) -> None:
        self._state = result.state
        if result.next_context is not None:
            self._previous = result.next_context

    async def _phrase(
        self,
        shot_id: UUID,
        result: AnalysisResult,
        prior: PreviousNote | None,
    ) -> AnalysisResult:
        if self._reference is None or not result.rule_id or not result.active_focus:
            return result
        finding = _focus_finding(result)
        observation = ""
        if finding is not None and finding.get("evidence_frames"):
            observation = str(finding.get("observation") or "")
        brief = FeedbackBrief(
            checkpoint_id=str(result.active_focus),
            rule_id=str(result.rule_id),
            approved_text=result.speech_text,
            observation=observation,
            previous_observation=None if prior is None else prior.previous_observation,
            previous_feedback=None if prior is None else prior.previous_cue,
            progress=result.decision_type,
            comparison_supported=comparison_supported(
                prior, finding, str(result.active_focus)
            ),
        )
        draft = None
        trace = self._trace(shot_id)
        if uses_wording_call(self._feedback) and self._provider is not None:
            self._mark(shot_id, "wording_started")
            trace.wording_used = True
            try:
                reply = await self._provider.complete(brief.request())
                draft = parse_feedback(dict(reply.payload))
                trace.wording_input_tokens = reply.input_tokens
                trace.wording_output_tokens = reply.output_tokens
                trace.wording_cached_tokens = reply.cached_tokens
            except Exception:
                logger.info(
                    "reference_feedback_fallback",
                    extra={"shot_id": str(shot_id), "coaching_reason": "feedback_failed"},
                )
            finally:
                self._mark(shot_id, "wording_completed")
        wording = compose_feedback(
            self._reference, brief, draft, strategy=self._feedback
        )
        trace.observation_included = wording.observation_included
        return replace(result, speech_text=wording.speech, hud_text=wording.hud)

    async def on_speech_started(self, event: SpeechStarted) -> None:
        if event.shot_id is None:
            return
        self._mark(event.shot_id, "tts_started")
        self._log_trace(event.shot_id)
        self._diag("note_playback_started", str(event.shot_id))

    async def on_speech(self, event: SpeechCompleted) -> None:
        if event.shot_id is not None:
            self._mark(event.shot_id, "tts_completed")
            self._log_trace(event.shot_id)
            self._diag(
                "note_playback_finished",
                str(event.shot_id),
                outcome=event.outcome.value,
                spoken_seconds=event.spoken_seconds,
                detail=event.detail,
            )
        if event.outcome is not SpeechOutcome.SPOKEN or event.shot_id is None:
            return
        cue = self._selected.get(event.shot_id)
        focus = self._state.active_focus
        if not cue or not focus or self._previous is None:
            return
        if self._previous.active_focus != focus:
            return
        self._previous = PreviousNote(
            active_focus=str(focus),
            previous_cue=cue,
            previous_observation=self._previous.previous_observation,
            delivery_status="delivered",
        )

    def timing(self, shot_id: UUID) -> CoachingTrace | None:
        return self._traces.get(shot_id)

    def _trace(self, shot_id: UUID) -> CoachingTrace:
        trace = self._traces.get(shot_id)
        if trace is None:
            trace = CoachingTrace(
                shot_id=str(shot_id),
                feedback=self._feedback,
                simulated=self._simulated,
            )
            self._traces[shot_id] = trace
            while len(self._traces) > 32:
                self._traces.pop(next(iter(self._traces)))
        return trace

    def _mark(self, shot_id: UUID, name: str) -> None:
        self._trace(shot_id).mark(name, self._monotonic())

    def _note_frames(self, shot_id: UUID, capture: Any) -> None:
        trace = self._trace(shot_id)
        trace.mark("swing_frames_ready", self._monotonic())
        trigger = str(getattr(capture, "trigger_id", "") or "")
        if trigger:
            self._trigger_shots[trigger] = shot_id
            completed = self._completed_at.get(trigger)
            if completed is not None:
                trace.mark("shot_completed", completed)
        completed = self._completed_at.get(str(shot_id))
        if completed is not None:
            trace.mark("shot_completed", completed)

    def _attach_shot_completed(
        self, shot_id: UUID, trigger_id: str | None, stamp: float
    ) -> None:
        keys = [shot_id]
        if trigger_id:
            linked = self._trigger_shots.get(trigger_id)
            if linked is not None:
                keys.append(linked)
        for key in keys:
            trace = self._traces.get(key)
            if trace is not None:
                trace.mark("shot_completed", stamp)
                self._log_trace(key)

    def _note_superseded(self, shot_id: UUID) -> None:
        if shot_id in self._closed:
            return
        self._closed.add(shot_id)
        self._failures += 1
        self._last_error = "a newer shot arrived"
        self._log_trace(shot_id)
        self._diag("note_failure", str(shot_id), "superseded", "a newer shot arrived")
        task = asyncio.create_task(
            self._emit(
                AnalysisFailed(
                    shot_id=shot_id,
                    reason=AdviceFailure.SUPERSEDED,
                    detail="a newer shot arrived",
                )
            )
        )
        self._side.add(task)
        task.add_done_callback(self._side.discard)

    def _log_trace(self, shot_id: UUID) -> None:
        trace = self._traces.get(shot_id)
        if trace is None:
            return
        report = latency_report(trace)
        logger.info(
            "coaching_feedback_timing",
            extra={
                "shot_id": trace.shot_id,
                "coaching_feedback": trace.feedback,
                "coaching_request": trace.request_type,
                "coaching_model": trace.model,
                "coaching_images": trace.image_count,
                "coaching_image_detail": trace.image_detail,
                "coaching_usage_input": trace.input_tokens,
                "coaching_usage_output": trace.output_tokens,
                "coaching_cached_input": trace.cached_tokens,
                "coaching_wording_used": trace.wording_used,
                "coaching_delivered": trace.delivered,
                "coaching_observation_used": trace.observation_included,
                "coaching_capture_ms": report["capture_ms"],
                "coaching_evidence_ms": report["evidence_ms"],
                "coaching_timeline_ms": report["timeline_ms"],
                "coaching_decode_ms": report["decode_ms"],
                "coaching_motion_ms": report["motion_ms"],
                "coaching_capture_id": trace.capture_id,
                "coaching_selected": list(trace.selected),
                "coaching_stages": list(trace.stages),
                "coaching_unresolved": list(trace.unresolved),
                "coaching_impact": trace.impact_status,
                "coaching_impact_interval": trace.impact_interval,
                "coaching_decision": trace.decision,
                "coaching_rule": trace.rule_id,
                "coaching_errors": list(trace.errors),
                "coaching_image_loading_ms": report["image_loading_ms"],
                "coaching_frame_difference_ms": report["frame_difference_ms"],
                "coaching_optical_flow_ms": report["optical_flow_ms"],
                "coaching_sampler_selection_ms": report["sampler_selection_ms"],
                "coaching_pose_ms": report["pose_ms"],
                "coaching_impact_ms": report["impact_ms"],
                "coaching_selection_ms": report["selection_ms"],
                "coaching_package_ms": report["package_ms"],
                "coaching_request_prepare_ms": report["request_prepare_ms"],
                "coaching_stage_ms": report["stage_ms"],
                "coaching_vision_ms": report["vision_ms"],
                "coaching_validation_ms": report["validation_ms"],
                "coaching_decision_ms": report["decision_ms"],
                "coaching_wording_ms": report["wording_ms"],
                "coaching_tts_startup_ms": report["tts_startup_ms"],
                "coaching_hud_ms": report["shot_to_hud_ms"],
                "coaching_audio_ms": report["shot_to_audio_ms"],
                "coaching_hud_budget": report["hud_budget"],
                "coaching_audio_budget": report["audio_budget"],
                "coaching_simulated": trace.simulated,
            },
        )
        self._diag("note_trace", trace)

    async def _fail(self, shot_id: UUID, reason: AdviceFailure, detail: str) -> None:
        if shot_id in self._closed:
            return
        self._closed.add(shot_id)
        self._failures += 1
        self._last_error = detail
        self._log_trace(shot_id)
        self._diag("note_failure", str(shot_id), _diagnostic_status(reason), detail)
        logger.info(
            "reference_coach_silent",
            extra={
                "shot_id": str(shot_id),
                "coaching_reason": reason.value,
                "coaching_detail": detail,
            },
        )
        await self._emit(AnalysisFailed(shot_id=shot_id, reason=reason, detail=detail))

    def _diag(self, method: str, /, *args: Any, **kwargs: Any) -> None:
        journal = self._diagnostics
        if journal is None:
            return
        try:
            getattr(journal, method)(*args, **kwargs)
        except Exception:
            logger.exception("diagnostics_note_failed")

    def _diag_assessment(
        self,
        shot_id: UUID,
        result: AnalysisResult,
        *,
        approved_correction: str,
        previous_focus: str | None,
        previous: PreviousNote | None,
        observation_included: bool,
    ) -> None:
        finding = _focus_finding(result)
        observation = "" if finding is None else str(finding.get("observation") or "")
        self._diag(
            "note_assessment",
            str(shot_id),
            result=result,
            approved_correction=approved_correction,
            previous_focus=None if previous_focus is None else str(previous_focus),
            previous=previous,
            reference=self._reference,
            observation=observation,
            observation_included=observation_included,
        )

    def _visual_checkpoints(self) -> list[str]:
        reference = self._reference
        if reference is None:
            return []
        return sorted(
            checkpoint_id
            for checkpoint_id, item in reference.checkpoints.items()
            if item.mode == "visual"
        )

    async def _emit(self, event: Any) -> None:
        try:
            await self._publish(event)
        except Exception:
            logger.exception("reference_coach_delivery_failed")


def _diagnostic_status(reason: AdviceFailure) -> str:
    if reason is AdviceFailure.SUPERSEDED:
        return "superseded"
    return "failed"


def _ms(seconds: float | None) -> int:
    if seconds is None:
        return 0
    return int(round(seconds * 1000))


def _advice(shot_id: UUID, result: AnalysisResult, prepared: PreparedRequest) -> Advice:
    from datetime import UTC, datetime

    finding = _focus_finding(result)
    severity_name = None if finding is None else finding.get("severity")
    kind = (
        AdviceKind.REINFORCEMENT
        if result.decision_type == "REINFORCE_PROGRESS"
        else AdviceKind.CORRECTION
    )
    return Advice(
        shot_id=shot_id,
        focus=CoachingFocus.NONE,
        display=result.hud_text,
        speech=result.speech_text,
        observation=result.feedback,
        severity=_SEVERITY.get(str(severity_name), 0.2 if kind is AdviceKind.REINFORCEMENT else 0.6),
        created_at=datetime.now(UTC),
        severity_source=SeveritySource.OBSERVED,
        source=AdviceSource.TEMPLATE,
        kind=kind,
        evidence_frame_id=None if not prepared.images else prepared.images[0].image_id,
    )


def _focus_finding(result: AnalysisResult) -> dict[str, Any] | None:
    assessment = result.assessment
    focus = result.active_focus
    if not assessment or not focus:
        return None
    for item in assessment.get("findings") or []:
        if item.get("checkpoint_id") == focus:
            return cast(dict[str, Any], item)
    return None


def _failure(error: ProviderError) -> AdviceFailure:
    name = type(error).__name__
    if "Timeout" in name:
        return AdviceFailure.TIMED_OUT
    if "Rate" in name:
        return AdviceFailure.RATE_LIMITED
    if "Reply" in name:
        return AdviceFailure.INVALID_REPLY
    return AdviceFailure.PROVIDER_ERROR
