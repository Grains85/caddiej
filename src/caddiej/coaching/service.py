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
field in it that could name an action.
"""

import asyncio
import logging
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from caddiej.coaching.baseline import Baseline
from caddiej.coaching.cache import AdviceCache, fingerprint
from caddiej.coaching.diagnose import diagnose
from caddiej.coaching.evidence import render
from caddiej.coaching.frames import DEFAULT_OFFSETS, FrameSource, offsets_for
from caddiej.coaching.prompt import (
    PROMPT_VERSION,
    build_request,
    fingerprint_parts,
)
from caddiej.coaching.provider import (
    AdviceProvider,
    ImageInput,
    ProviderError,
    ProviderRateLimitedError,
    ProviderRefusedError,
    ProviderReply,
    ProviderTimeoutError,
    ProviderUnavailableError,
    image_inputs,
)
from caddiej.coaching.templates import legal_displays
from caddiej.coaching.validate import (
    AdviceRejected,
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
    AdviceOutcome,
    AdviceStatus,
    CoachingFocus,
    Diagnosis,
)
from caddiej.domain.events import (
    AnalysisCompleted,
    AnalysisFailed,
    AnalysisStarted,
    ShotCompleted,
    ShotDiagnosed,
    SwingClipFailed,
    SwingClipReady,
)
from caddiej.domain.media import ClipRef
from caddiej.domain.statistics import club_of
from caddiej.runtime.backoff import Backoff

logger = logging.getLogger(__name__)

Publish = Callable[[Event], Awaitable[None]]
Record = Callable[[AdviceOutcome], Awaitable[object]]
Sleep = Callable[[float], Awaitable[None]]
Now = Callable[[], datetime]

_STOP = object()
# How many shots back the "do not repeat yourself" list reaches.
_RECENT_LINES = 3
# Bounded, so a long evening cannot grow the set of shots already handled.
_SEEN_LIMIT = 256

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
    shots_diagnosed: int
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


@dataclass(slots=True)
class _Job:
    """One shot waiting to be coached, and the clip it may be waiting for."""

    diagnosis: Diagnosis
    queued_monotonic: float
    wants_frames: bool = False
    clip: ClipRef | None = None
    footage: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def shot_id(self) -> UUID:
        return self.diagnosis.shot_id


class AnalysisService:
    """Owns the diagnosis, the queue behind it, and the model beyond that."""

    def __init__(
        self,
        publish: Publish,
        provider: AdviceProvider | None = None,
        frames: FrameSource | None = None,
        record: Record | None = None,
        enabled: bool = True,
        send_frames: bool = False,
        frame_offsets: Sequence[float] = DEFAULT_OFFSETS,
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
        max_output_tokens: int = 60,
        cache_capacity: int = 128,
        drain_timeout_seconds: float = 5.0,
        backoff_initial_seconds: float = 1.0,
        backoff_max_seconds: float = 8.0,
        monotonic: Callable[[], float] = time.monotonic,
        now: Now = lambda: datetime.now(UTC),
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._publish = publish
        self._provider = provider
        self._frames = frames
        self._record = record
        self._enabled = enabled
        self._send_frames = send_frames and frames is not None
        self._frame_offsets = tuple(frame_offsets)
        self._preferred = preferred_focus
        self._fine = severity_fine
        self._clip_wait = clip_wait_seconds
        self._timeout = timeout_seconds
        self._budget = total_budget_seconds
        self._retry_limit = retry_limit
        self._min_interval = min_interval_seconds
        self._call_limit = session_call_limit
        self._max_output_tokens = max_output_tokens
        self._drain_timeout = drain_timeout_seconds
        self._backoff_initial = backoff_initial_seconds
        self._backoff_max = backoff_max_seconds
        self._monotonic = monotonic
        self._now = now
        self._sleep = sleep

        self._baseline = Baseline(window=baseline_window)
        self._cache = AdviceCache(capacity=cache_capacity)
        self._queue: asyncio.Queue[_Job | object] = asyncio.Queue(maxsize=queue_size)
        self._jobs: dict[UUID, _Job] = {}
        self._seen: OrderedDict[UUID, None] = OrderedDict()
        self._recent: deque[Advice] = deque(maxlen=_RECENT_LINES)
        self._worker: asyncio.Task[None] | None = None
        self._closed = False
        self._current: UUID | None = None
        self._absent: AdviceFailure | None = AdviceFailure.DISABLED

        self.shots_diagnosed = 0
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

        if self._provider is None:
            self._absent = AdviceFailure.DISABLED
            logger.info("coaching_deterministic_only")
        elif not self._provider.is_configured:
            self._absent = AdviceFailure.NO_CREDENTIALS
            logger.warning(
                "coaching_provider_unconfigured",
                extra={"coaching_provider": self._provider.name},
            )
            self._provider = None
        else:
            self._absent = None
            logger.info(
                "coaching_ready",
                extra={
                    "coaching_provider": self._provider.name,
                    "coaching_model": self._provider.model,
                    "coaching_frames": self._send_frames,
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

    # --- the event path ----------------------------------------------------

    def subscribe(self, bus: EventBus) -> None:
        bus.subscribe(ShotCompleted, self.on_shot_completed, name="coaching")
        bus.subscribe(SwingClipReady, self.on_clip_ready, name="coaching")
        bus.subscribe(SwingClipFailed, self.on_clip_failed, name="coaching")

    async def on_shot_completed(self, event: ShotCompleted) -> None:
        """Diagnose the shot and publish the verdict. Touches nothing slow."""

        if not self._enabled or self._closed:
            return

        shot = event.shot
        baseline = self._baseline.for_club(club_of(shot))
        diagnosis = diagnose(shot, baseline, self._preferred, self._fine)
        # After diagnosing, never before. A shot included in its own baseline is
        # by definition average, which would make every first shot unremarkable.
        self._baseline.remember(shot)

        self.shots_diagnosed += 1
        self._current = diagnosis.shot_id
        logger.info(
            "coaching_shot_diagnosed",
            extra={
                "shot_id": str(diagnosis.shot_id),
                "coaching_focus": diagnosis.focus.value,
                "coaching_severity": round(diagnosis.severity, 3),
                "coaching_baseline_shots": diagnosis.baseline_shots,
            },
        )
        await self._publish(ShotDiagnosed(diagnosis=diagnosis))

        if diagnosis.shot_id in self._seen:
            # The same shot twice. One line, one call, one row.
            return
        self._remember_seen(diagnosis.shot_id)
        await self._enqueue(diagnosis)

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

    async def _enqueue(self, diagnosis: Diagnosis) -> None:
        """Take a job, displacing an older one if the queue is full."""

        job = _Job(
            diagnosis=diagnosis,
            queued_monotonic=self._monotonic(),
            wants_frames=self._send_frames and diagnosis.is_worth_saying,
        )
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
                    await self._fail(
                        stale,
                        AdviceFailure.SUPERSEDED,
                        "displaced by a newer shot before it was analysed",
                    )
                continue
            self._jobs[job.shot_id] = job
            return

    # --- the worker --------------------------------------------------------

    async def _work(self) -> None:
        """Take jobs one at a time until told to stop. Must not raise."""

        while True:
            item = await self._queue.get()
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
        diagnosis = job.diagnosis
        template = from_diagnosis(diagnosis, self._now())

        if not diagnosis.is_worth_saying:
            # A good shot. Nothing to say, so nothing to ask and nothing to pay.
            await self._settle(job, template, AdviceStatus.TEMPLATE)
            return

        if self._provider is None:
            await self._settle(job, template, AdviceStatus.TEMPLATE)
            return

        if job.wants_frames:
            await self._wait_for_footage(job)

        if self._is_stale(job):
            await self._fail(job, AdviceFailure.SUPERSEDED, "a newer shot arrived")
            return

        images, offsets = await self._stills(job)
        key = fingerprint(
            fingerprint_parts(diagnosis, self._provider.model, bool(images))
        )
        cached = self._cache.get(key, diagnosis.shot_id, self._now())
        if cached is not None:
            await self._settle(
                job, cached, AdviceStatus.OK, cache_hit=True, fingerprint_value=key
            )
            return

        refusal = self._blocked()
        if refusal is not None:
            await self._fail(job, refusal, "no call was made")
            return

        await self._ask(job, key, images, offsets)

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
                extra={"shot_id": str(job.shot_id)},
            )

    async def _stills(
        self, job: _Job
    ) -> tuple[tuple[ImageInput, ...], tuple[float, ...]]:
        """Pull frames out of the clip, or coach from the numbers alone."""

        if job.clip is None or self._frames is None or not self._send_frames:
            return (), ()

        offsets = offsets_for(job.clip, self._frame_offsets)
        try:
            raw = await self._frames.frames(job.clip, offsets)
        except Exception as error:
            # A frame source that fails is a reason to coach from the numbers,
            # not a reason to abandon the shot.
            self.last_error = f"{type(error).__name__}: {error}"
            logger.warning(
                "coaching_frames_unavailable",
                extra={"shot_id": str(job.shot_id)},
                exc_info=error,
            )
            return (), ()

        images = image_inputs(raw)
        self.images_sent += len(images)
        return images, offsets

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
            preferred=self._preferred,
            clip=job.clip,
            frames=len(images),
            frame_offsets=offsets,
        )
        request = build_request(
            diagnosis,
            evidence,
            displays=sorted(self._displays(diagnosis, bool(images))),
            images=images,
            max_output_tokens=self._max_output_tokens,
            timeout_seconds=self._timeout,
        )
        await self._publish(
            AnalysisStarted(
                shot_id=diagnosis.shot_id,
                focus=diagnosis.focus,
                frames=len(images),
                model=self._provider.model,
            )
        )

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
                return
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
                return

            attempt += 1
            self.calls += 1
            self._last_call_monotonic = self._monotonic()
            try:
                async with asyncio.timeout(remaining):
                    reply = await self._provider.complete(request)
                break
            except TimeoutError as error:
                # The budget, not the provider's own timeout: there is by
                # definition nothing left to retry with.
                await self._fail(job, AdviceFailure.TIMED_OUT, error)
                return
            except ProviderRefusedError as error:
                # Wrong key, unknown model, a refusal. Identical next time.
                await self._fail(job, AdviceFailure.PROVIDER_ERROR, error)
                return
            except (
                ProviderUnavailableError,
                ProviderRateLimitedError,
                ProviderTimeoutError,
            ) as error:
                reason = _RETRYABLE[type(error)]
                remaining = self._remaining(job)
                if attempt > self._retry_limit or self._is_stale(job) or remaining <= 0:
                    await self._fail(job, reason, error)
                    return
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
                        "coaching_backoff_seconds": round(delay, 2),
                    },
                )
                await self._sleep(delay)
            except ProviderError as error:
                await self._fail(job, AdviceFailure.PROVIDER_ERROR, error)
                return

        try:
            advice = validate(
                reply.payload,
                diagnosis,
                self._now(),
                frames_sent=bool(images),
            )
        except AdviceRejected as error:
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

    def _displays(self, diagnosis: Diagnosis, has_frames: bool) -> set[str]:
        """Every display a reply is allowed to carry, for this shot."""

        permitted: set[str] = set()
        labels = {
            observation.focus: observation.label
            for observation in diagnosis.observations
        }
        for candidate in diagnosis.candidates:
            permitted |= legal_displays(candidate.focus, labels.get(candidate.focus))
        if has_frames:
            for focus in OBSERVED_FOCUSES:
                permitted |= legal_displays(focus)
        return permitted or {diagnosis.display}

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
    ) -> None:
        """Publish the line, if a model wrote it, and record what happened."""

        if status is AdviceStatus.OK:
            self.lines_from_model += 1
            await self._publish(AnalysisCompleted(shot_id=job.shot_id, advice=advice))
            logger.info(
                "coaching_advice_ready",
                extra={
                    "shot_id": str(job.shot_id),
                    "coaching_focus": advice.focus.value,
                    "coaching_display": advice.display,
                    "coaching_cache_hit": cache_hit,
                    "coaching_latency_ms": (
                        None if reply is None else round(reply.latency_seconds * 1000)
                    ),
                },
            )
        else:
            self.lines_from_template += 1

        await self._store(
            job,
            advice,
            status,
            reply=reply,
            images=images,
            cache_hit=cache_hit,
            fingerprint_value=fingerprint_value,
        )

    async def _fail(
        self,
        job: _Job,
        reason: AdviceFailure,
        detail: object,
        reply: ProviderReply | None = None,
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
        await self._publish(
            AnalysisFailed(shot_id=job.shot_id, reason=reason, detail=text)
        )
        await self._store(
            job,
            from_diagnosis(job.diagnosis, self._now()),
            AdviceStatus.UNAVAILABLE,
            reply=reply,
            failure=reason,
            detail=text,
        )

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
        return self._budget - (self._monotonic() - job.queued_monotonic)

    def _absorb(self, reply: ProviderReply) -> None:
        self.input_tokens += reply.input_tokens or 0
        self.output_tokens += reply.output_tokens or 0

    def _remember_seen(self, shot_id: UUID) -> None:
        self._seen[shot_id] = None
        while len(self._seen) > _SEEN_LIMIT:
            self._seen.popitem(last=False)

    # --- reporting ---------------------------------------------------------

    def health(self) -> AnalysisHealth:
        return AnalysisHealth(
            enabled=self._enabled,
            available=self.is_available,
            provider=None if self._provider is None else self._provider.name,
            model=None if self._provider is None else self._provider.model,
            shots_diagnosed=self.shots_diagnosed,
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
        )
