"""Saying one thing at a time, and never at the wrong moment.

Four rules shape this file, and they are the same four that shape coaching,
because the problem is the same one: something slow and fallible has to happen
near something that must never be delayed.

**The event path only queues.** `on_analysis_completed` normalises some text,
puts it on a list and returns. It renders nothing and plays nothing, so a
sentence that takes a second to synthesise cannot hold up the bus that is also
carrying shots to the floor.

**One voice.** There is a single worker and a single active task, and playback
is awaited to completion, so two utterances overlapping is not a race that is
unlikely — it is a state this file cannot represent. That is the whole of the
guarantee, and it is why it is worth the worker.

**Louder wins, immediately.** An utterance arriving with a higher priority than
the one being spoken cancels it mid-sentence. Cancellation, rather than a flag
the sink polls, because it also works when the interruption lands during
rendering, and because the sink then silences the device on its way out — so by
the time the worker starts the next line the speaker is genuinely free.

**Nothing here can affect anything else.** No engine, no cache, no sound card
and no bug in this file may reach the application. Every path ends in a counter,
a log line and a `SpeechCompleted` saying what became of the sentence, and the
worst outcome available is a quiet room.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from caddiej.core.event_bus import EventBus
from caddiej.core.events import Event
from caddiej.domain.events import (
    AnalysisCompleted,
    ShotCompleted,
    ShotDetected,
    SpeechCompleted,
    SpeechStarted,
)
from caddiej.domain.speech import SpeechOutcome, SpeechPriority, Utterance
from caddiej.speech.audio import MAX_GAIN, Audio, amplify
from caddiej.speech.cache import AudioCache, voice_key
from caddiej.speech.envelope import envelope_of
from caddiej.speech.sink import AudioSink, PlaybackError, PlaybackUnavailableError
from caddiej.speech.synth import (
    SynthesisError,
    SynthesisUnavailableError,
    Synthesizer,
)

logger = logging.getLogger(__name__)

Publish = Callable[[Event], Awaitable[None]]

DEFAULT_VOICE = "bm_george"


@dataclass(frozen=True, slots=True)
class SpeechHealth:
    """What the room has been saying, and everything that stopped it.

    The four ways a line goes unspoken are counted separately because they mean
    completely different things: superseded is the system working, dropped is
    the queue too small, failed is a broken engine, and unavailable is a machine
    that was never going to speak.
    """

    enabled: bool
    available: bool
    engine: str | None
    output: str | None
    voice: str
    speaking: bool
    queued: int
    spoken: int
    interrupted: int
    superseded: int
    dropped: int
    failed: int
    unavailable: int
    rendered: int
    cache_hits: int
    spoken_seconds: float
    last_error: str | None


# Compared by identity, never by value. Two shots can genuinely want the same
# sentence said, and a queue that treated those as one entry would remove the
# wrong job.
@dataclass(slots=True, eq=False)
class _Job:
    """One thing waiting to be said, and what has happened to it so far."""

    utterance: Utterance
    queued_monotonic: float
    interrupted: bool = False
    superseded: bool = False
    started_monotonic: float | None = None

    @property
    def priority(self) -> SpeechPriority:
        return self.utterance.priority

    @property
    def utterance_id(self) -> UUID:
        return self.utterance.utterance_id

    @property
    def is_speaking(self) -> bool:
        return self.started_monotonic is not None


class SpeechService:
    """Owns the queue, the one voice, and everything that can silence it."""

    def __init__(
        self,
        publish: Publish,
        synthesizer: Synthesizer | None = None,
        sink: AudioSink | None = None,
        cache: AudioCache | None = None,
        enabled: bool = True,
        voice: str = DEFAULT_VOICE,
        speed: float = 1.0,
        queue_size: int = 4,
        max_characters: int = 300,
        render_timeout_seconds: float = 20.0,
        open_timeout_seconds: float = 90.0,
        drain_timeout_seconds: float = 5.0,
        gain: float = 1.0,
        monotonic: Callable[[], float] = time.monotonic,
        diagnostics: Any = None,
    ) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must hold at least one utterance")
        if max_characters < 1:
            raise ValueError("max_characters must allow at least one character")

        self._publish = publish
        self._synthesizer = synthesizer
        self._sink = sink
        # Explicitly against None: a cache defines `__len__`, so an empty one is
        # falsy, and `cache or AudioCache()` would quietly throw away the cache
        # it was handed every time it mattered most.
        self._cache = AudioCache() if cache is None else cache
        self._enabled = enabled
        self._voice = voice or DEFAULT_VOICE
        self._speed = speed
        self._queue_size = queue_size
        self._max_characters = max_characters
        self._render_timeout = render_timeout_seconds
        self._open_timeout = open_timeout_seconds
        self._drain_timeout = drain_timeout_seconds
        # Checked here rather than at the first sentence, so a gain nobody
        # should be running is a startup failure and not a surprise at volume.
        if not 0.0 < gain <= MAX_GAIN:
            raise ValueError(f"gain must be above zero and at most {MAX_GAIN}")
        self._gain = gain
        self._monotonic = monotonic
        self._diagnostics = diagnostics

        self._waiting: list[_Job] = []
        self._strike_id: UUID | None = None
        self._strike_trigger: str | None = None
        self._paired_shot: UUID | None = None
        self._arrived = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._active: _Job | None = None
        self._task: asyncio.Task[object] | None = None
        self._stopping = False
        self._ready = False

        self.spoken = 0
        self.interrupted = 0
        self.superseded = 0
        self.dropped = 0
        self.failed = 0
        self.unavailable = 0
        self.rendered = 0
        self.spoken_seconds = 0.0
        self.last_error: str | None = None

    # --- lifecycle ---------------------------------------------------------

    @property
    def is_available(self) -> bool:
        """Whether anything said now would actually be heard."""

        return self._ready and self._enabled

    @property
    def is_idle(self) -> bool:
        """Nothing waiting, and nothing being rendered or spoken."""

        return not self._waiting and self._active is None

    async def open(self) -> None:
        """Load the voice, claim the speaker, start the worker. Never raises.

        A missing engine, missing weights or missing sound card are supported
        ways to run CaddieJ, not failures to start it: nothing else in the
        application depends on the room being able to talk.
        """

        if not self._enabled:
            logger.info("speech_disabled")
            return
        if self._synthesizer is None or self._sink is None:
            logger.info("speech_no_engine")
            return

        if not self._synthesizer.is_configured:
            logger.warning(
                "speech_engine_unconfigured",
                extra={"speech_engine": self._synthesizer.name},
            )
            return
        if not self._sink.is_configured:
            logger.warning(
                "speech_output_unconfigured",
                extra={"speech_output": self._sink.name},
            )
            return

        try:
            # Bounded, because everything after speech in start-up waits behind
            # it and loading a voice means downloading one on a first run. A
            # model that will not arrive leaves the room quiet, which is a
            # supported way to run CaddieJ; a start-up that never finishes is
            # not.
            async with asyncio.timeout(self._open_timeout):
                # Before the sink, because loading a voice is the slow half and
                # holding a sound card open while it happens serves nobody.
                await self._synthesizer.open()
                await self._sink.open()
            # Inside the try as well: it makes a directory, and an unwritable
            # cache root is not a reason to fail to start.
            await self._cache.open()
        except TimeoutError:
            self.last_error = f"the voice did not load within {self._open_timeout:g}s"
            logger.warning(
                "speech_open_timed_out",
                extra={
                    "speech_engine": self._synthesizer.name,
                    "speech_seconds": self._open_timeout,
                },
            )
            await self._release()
            return
        except (SynthesisUnavailableError, PlaybackUnavailableError) as error:
            self.last_error = f"{type(error).__name__}: {error}"
            logger.warning("speech_unavailable", exc_info=error)
            await self._release()
            return
        except Exception as error:
            self.last_error = f"{type(error).__name__}: {error}"
            logger.exception("speech_open_failed")
            await self._release()
            return

        self._ready = True
        self._worker = asyncio.create_task(self._work(), name="speech-worker")
        logger.info(
            "speech_ready",
            extra={
                "speech_engine": self._synthesizer.name,
                "speech_output": self._sink.name,
                "speech_voice": self._voice,
            },
        )

    async def aclose(self) -> None:
        """Stop talking and let go of the speaker.

        Whatever is mid-sentence is allowed to finish, briefly, because cutting
        a voice off at the exact moment somebody presses Ctrl-C sounds like a
        crash. Everything still waiting is abandoned: a queue drained on the way
        out would keep the room talking after it was told to stop.
        """

        self._stopping = True
        for job in self._discard_waiting():
            await self._finish(job, SpeechOutcome.DROPPED, detail="shutting down")

        worker = self._worker
        self._worker = None
        if worker is not None:
            self._arrived.set()
            try:
                await asyncio.wait_for(worker, self._drain_timeout)
            except TimeoutError:
                logger.warning("speech_drain_timed_out")
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

        await self._release()

    async def _release(self) -> None:
        """Let go of the voice and the sound card. Never raises.

        Also used when opening fails part way through, which is the case that
        makes it worth having: the voice loads before the sink is claimed, so an
        engine that worked followed by a sound card that did not would otherwise
        leave a few hundred megabytes of model loaded for a subsystem that has
        just switched itself off.
        """

        for closeable in (self._sink, self._synthesizer):
            if closeable is None:
                continue
            try:
                await closeable.aclose()
            except Exception:
                logger.warning("speech_close_failed", exc_info=True)
        self._ready = False

    # --- the event path ----------------------------------------------------

    def subscribe(self, bus: EventBus) -> None:
        bus.subscribe(AnalysisCompleted, self.on_analysis_completed, name="speech")
        bus.subscribe(ShotCompleted, self.on_shot_completed, name="speech")
        bus.subscribe(ShotDetected, self.on_shot_detected, name="speech")

    async def on_shot_detected(self, event: ShotDetected) -> None:
        """A new ball struck: anything unsaid about an earlier one is stale.

        ProTee-only coaching keys its line on the strike, so this is the event
        that makes it old. A ball placed on the mat is not.
        """

        if event.trigger_id and event.trigger_id == self._strike_trigger:
            return
        self._strike_trigger = event.trigger_id or None
        self._strike_id = event.event_id
        self._paired_shot = None
        for stale in self._perished(event.event_id):
            await self._finish(
                stale,
                SpeechOutcome.SUPERSEDED,
                detail="another ball was struck before this was said",
            )

    async def on_analysis_completed(self, event: AnalysisCompleted) -> None:
        """Speak a coaching line, once a model's words have been accepted.

        Only the coached line, and deliberately: the templated sentence is
        already on the floor and reads in less time than it takes to say, so
        speaking every one of them would turn a quiet aid into a machine that
        talks after every swing.

        A reply that arrives after a newer strike is about the wrong ball,
        even when that strike never produces frames. A repeated trigger is
        still this ball. So is the simulator result joined to this strike.
        """

        if not self._analysis_is_current(event.shot_id):
            logger.info(
                "speech_suppressed",
                extra={
                    "shot_id": str(event.shot_id),
                    "coaching_reason": "superseded",
                    "speech_detail": "a newer ball was struck before this line was ready",
                },
            )
            return
        await self.say(
            Utterance(
                text=event.advice.speech,
                priority=SpeechPriority.COACHING,
                shot_id=event.shot_id,
            )
        )

    async def on_shot_completed(self, event: ShotCompleted) -> None:
        """Another ball has been hit, so forget anything unsaid about the last.

        Supersession alone does not cover this. It replaces a coaching line with
        a newer coaching line, which is the common case, and leaves the case
        where no newer line ever arrives — the next shot was not worth a word,
        or the model failed on it, or nothing about it could be measured. The
        old sentence would then be spoken into a room where the player has
        moved on, and it would be heard as advice about the swing they just made.

        Only what is not yet audible. A sentence already coming out of the
        speaker is left to finish, for the same reason `_supersede_pending`
        draws that line: a line chopped off mid-word sounds like a fault, and
        the player has in any case heard most of it.
        """

        # GSPro writing the result of the ball just struck is not a newer
        # ball: a line keyed on that strike is still about it. The same
        # trigger joins the simulator shot id to the strike, so a later
        # reply for either id is still this ball.
        self._pair_simulator_shot(event)
        keep = (event.shot.shot_id,) + (
            () if self._strike_id is None else (self._strike_id,)
        )
        for stale in self._perished(*keep):
            await self._finish(
                stale,
                SpeechOutcome.SUPERSEDED,
                detail="another shot was hit before this was said",
            )

    def _analysis_is_current(self, shot_id: UUID) -> bool:
        """Whether this reply is still about the ball in play.

        No strike has been seen yet on paths that coach from the simulator
        shot alone. After a strike, the line may be keyed on that strike or
        on the simulator shot joined to it.
        """

        if self._strike_id is None:
            return True
        return shot_id == self._strike_id or shot_id == self._paired_shot

    def _pair_simulator_shot(self, event: ShotCompleted) -> None:
        """Remember the simulator id when it is this strike's own result."""

        if self._strike_id is None:
            return
        trigger = event.trigger_id or ""
        if trigger != (self._strike_trigger or ""):
            return
        self._paired_shot = event.shot.shot_id

    def _perished(self, *current: UUID) -> list[_Job]:
        """Unsaid perishable lines about some earlier shot.

        Lines with no shot attached are left alone: an announcement or a warning
        is not about a swing, so a swing cannot make it stale.
        """

        def about_an_earlier_shot(job: _Job) -> bool:
            return (
                job.utterance.perishable
                and job.utterance.shot_id is not None
                and job.utterance.shot_id not in current
            )

        stale = [job for job in self._waiting if about_an_earlier_shot(job)]
        for job in stale:
            job.superseded = True
        self._waiting = [
            job for job in self._waiting if not any(job is dropped for dropped in stale)
        ]

        active = self._active
        if (
            active is not None
            and not active.is_speaking
            and about_an_earlier_shot(active)
        ):
            active.superseded = True
            if self._task is not None and not self._task.done():
                self._task.cancel()
        return stale

    async def say(self, utterance: Utterance) -> bool:
        """Queue something to be said. Returns whether it was accepted.

        Public, and not only for the event handler: this is the seam a future
        announcement or warning uses, and the priority it passes is the whole
        of what it needs to know about everything else being said.
        """

        if not self.is_available:
            self.unavailable += 1
            logger.info(
                "speech_suppressed",
                extra={
                    "shot_id": (
                        None if utterance.shot_id is None else str(utterance.shot_id)
                    ),
                    "coaching_reason": "unavailable",
                    "speech_detail": "speech is not available",
                },
            )
            self._diagnostic_tts(
                utterance.shot_id,
                status="skipped" if not self._enabled else "failed",
                detail="speech is not available",
                monotonic_seconds=None,
            )
            return False

        text = self._clean(utterance.text)
        if not text:
            logger.info(
                "speech_suppressed",
                extra={
                    "shot_id": (
                        None if utterance.shot_id is None else str(utterance.shot_id)
                    ),
                    "coaching_reason": "refused",
                    "speech_detail": "empty_or_too_long",
                },
            )
            self._diagnostic_tts(
                utterance.shot_id,
                status="skipped",
                detail="empty_or_too_long",
                monotonic_seconds=None,
            )
            return False

        job = _Job(
            utterance=Utterance(
                text=text,
                priority=utterance.priority,
                utterance_id=utterance.utterance_id,
                shot_id=utterance.shot_id,
                voice=utterance.voice,
                perishable=utterance.perishable,
            ),
            queued_monotonic=self._monotonic(),
        )

        for stale in self._supersede(job):
            await self._finish(stale, SpeechOutcome.SUPERSEDED)
        self._supersede_pending(job)
        self._insert(job)
        for surplus in self._trim():
            await self._finish(
                surplus, SpeechOutcome.DROPPED, detail="the speech queue was full"
            )
        self._interrupt_for(job)
        self._arrived.set()
        accepted = any(waiting is job for waiting in self._waiting)
        self._diagnostic_tts(
            job.utterance.shot_id,
            status="success" if accepted else "failed",
            detail="queued" if accepted else "not accepted onto the speech queue",
            monotonic_seconds=job.queued_monotonic if accepted else None,
        )
        if accepted:
            # The gap between a line being ready and a line being audible is
            # where everything slow happens — a voice loading, a render, a queue
            # — so a trace that jumps from advice to `speech_started` cannot say
            # which of them a silent room is waiting on.
            logger.info(
                "speech_queued",
                extra={
                    "speech_utterance": str(job.utterance_id),
                    "speech_priority": int(job.priority),
                    "speech_queued_lines": len(self._waiting),
                    "shot_id": (
                        None
                        if job.utterance.shot_id is None
                        else str(job.utterance.shot_id)
                    ),
                },
            )
        return accepted

    def _clean(self, text: str) -> str:
        """Collapse the whitespace and bound the length.

        The cap is not about the words: it is what stops one bad sentence
        occupying the engine, the speaker and the room for a minute. Truncating
        would produce a confident half-instruction, so an oversized line is
        refused instead.
        """

        collapsed = " ".join(text.split())
        if len(collapsed) > self._max_characters:
            logger.warning(
                "speech_text_too_long",
                extra={"speech_characters": len(collapsed)},
            )
            return ""
        return collapsed

    def _diagnostic_tts(
        self,
        shot_id: UUID | None,
        *,
        status: str,
        detail: str,
        monotonic_seconds: float | None,
    ) -> None:
        journal = self._diagnostics
        if journal is None or shot_id is None:
            return
        try:
            journal.note_tts(
                str(shot_id),
                status=status,
                detail=detail,
                monotonic_seconds=monotonic_seconds,
            )
        except Exception:
            logger.exception("diagnostics_note_failed")

    def _supersede(self, arriving: _Job) -> list[_Job]:
        """Waiting lines the new one makes pointless.

        Only ever same priority and only ever when both sides are perishable: a
        newer coaching line replaces an older coaching line because being late
        is the same as being wrong, and nothing else in the queue is like that.
        """

        if not arriving.utterance.perishable:
            return []
        stale = [
            job
            for job in self._waiting
            if job.priority is arriving.priority and job.utterance.perishable
        ]
        for job in stale:
            job.superseded = True
        self._waiting = [
            job for job in self._waiting if not any(job is dropped for dropped in stale)
        ]
        return stale

    def _supersede_pending(self, arriving: _Job) -> None:
        """Abandon a line that is still being rendered, if this replaces it.

        Rendering a sentence takes about a second, which is long enough for the
        next coaching line to arrive during it. Abandoning one that has not
        started playing costs nothing and saves the room a line about the shot
        before last. Once a sentence is audible this no longer applies: cutting
        it in half to start another of equal importance sounds like a fault, and
        `_interrupt_for` is the only thing allowed to do that.
        """

        active = self._active
        if active is None or active.is_speaking:
            return
        if active.priority is not arriving.priority:
            return
        if not (arriving.utterance.perishable and active.utterance.perishable):
            return

        active.superseded = True
        if self._task is not None and not self._task.done():
            self._task.cancel()

    def _insert(self, job: _Job) -> None:
        """Place it after everything at least as urgent, and before the rest."""

        index = len(self._waiting)
        for position, existing in enumerate(self._waiting):
            if job.priority > existing.priority:
                index = position
                break
        self._waiting.insert(index, job)

    def _trim(self) -> list[_Job]:
        """Whatever no longer fits: least urgent first, newest of those first.

        The arriving utterance is a candidate like any other, which is the
        point. A coaching line turning up behind a full queue of warnings is the
        line that should be refused, and dropping from the tail says so without
        a special case.
        """

        surplus: list[_Job] = []
        while len(self._waiting) > self._queue_size:
            surplus.append(self._waiting.pop())
        return surplus

    def _interrupt_for(self, job: _Job) -> None:
        """Cut off what is being said, if this deserves the room more."""

        active = self._active
        if active is None or job.priority <= active.priority:
            return
        active.interrupted = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
        logger.info(
            "speech_interrupted",
            extra={
                "speech_utterance": str(active.utterance_id),
                "speech_priority": int(job.priority),
            },
        )

    def _discard_waiting(self) -> list[_Job]:
        waiting = list(self._waiting)
        self._waiting.clear()
        return waiting

    # --- the worker --------------------------------------------------------

    async def _work(self) -> None:
        """Say one thing at a time until told to stop. Must not raise."""

        while True:
            # Cleared before looking, so that something queued between the look
            # and the wait sets it again rather than being slept through.
            self._arrived.clear()
            if self._waiting:
                job = self._waiting.pop(0)
            elif self._stopping:
                return
            else:
                await self._arrived.wait()
                continue

            self._active = job
            try:
                await self._perform(job)
            except asyncio.CancelledError:
                # Two very different things raise this: the child task being
                # cancelled to abandon a sentence, and this worker itself being
                # cancelled at shutdown. `cancelling()` counts requests against
                # *this* task, so it is the only honest way to tell them apart —
                # and swallowing the second would leave a worker that cannot be
                # stopped.
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                if job.interrupted:
                    await self._report_interrupted(job)
                elif job.superseded:
                    await self._finish(job, SpeechOutcome.SUPERSEDED)
                else:
                    raise
            except Exception as error:
                # A bug in here must not reach the supervisor, which would read
                # it as grounds to shut CaddieJ down over a spoken line.
                logger.exception(
                    "speech_worker_error",
                    extra={"speech_utterance": str(job.utterance_id)},
                )
                await self._fail(job, error)
            finally:
                self._active = None
                self._task = None

    async def _perform(self, job: _Job) -> None:
        """Render the line if it is not already rendered, then play it."""

        audio = await self._audio(job)
        if audio is None:
            return
        if job.superseded:
            # A newer line arrived while this one was being rendered. Speaking
            # it now would be talking about the shot before last.
            await self._finish(job, SpeechOutcome.SUPERSEDED)
            return
        if job.interrupted:
            # Cut off in the gap between rendering and playing, where there was
            # no task to cancel. Checked rather than assumed unreachable,
            # because starting a sentence that has already been overruled is
            # exactly the overlap this service exists to prevent.
            await self._report_interrupted(job)
            return
        await self._speak(job, audio)

    async def _audio(self, job: _Job) -> Audio | None:
        """The samples for this line, from the cache or from the engine."""

        assert self._synthesizer is not None
        voice = job.utterance.voice or self._voice
        key = voice_key(job.utterance.text, voice, self._speed, self._synthesizer.name)

        cached = await self._cache.get(key)
        if cached is not None:
            return cached

        task = asyncio.create_task(self._synthesizer.render(job.utterance.text, voice))
        self._task = task
        try:
            async with asyncio.timeout(self._render_timeout):
                audio = await task
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await self._fail(job, f"the voice took longer than {self._render_timeout}s")
            return None
        except SynthesisError as error:
            await self._fail(job, error)
            return None
        finally:
            self._task = None

        self.rendered += 1
        await self._cache.put(key, audio)
        return audio

    async def _speak(self, job: _Job, audio: Audio) -> None:
        """Play it, start to finish, with nothing else on the speaker."""

        assert self._sink is not None
        job.started_monotonic = self._monotonic()
        shot_id = job.utterance.shot_id
        voice = job.utterance.voice or self._voice
        await self._announce(
            SpeechStarted(
                utterance_id=job.utterance_id,
                text=job.utterance.text,
                voice=voice,
                priority=job.priority,
                duration_seconds=round(audio.duration_seconds, 3),
                shot_id=shot_id,
                # Measured here, from the samples that are about to be played,
                # so that anything reacting to the voice is reacting to the
                # actual voice. A millisecond of arithmetic for a three-second
                # sentence, which is cheap enough to spend whether or not
                # anybody is listening — and much cheaper than the alternative,
                # which is handing the audio itself to every subscriber.
                envelope=envelope_of(audio),
            )
        )
        logger.info(
            "speech_started",
            extra={
                "speech_utterance": str(job.utterance_id),
                "speech_voice": voice,
                "speech_seconds": round(audio.duration_seconds, 2),
                "shot_id": None if shot_id is None else str(shot_id),
            },
        )

        # Last thing before the speaker, and after both the cache and the
        # envelope. A cached WAV stays the engine's own output, so changing the
        # gain does not retire a single file, and the lights keep reacting to
        # the shape of the words rather than to how loud the room wants them.
        loud, clipped = amplify(audio, self._gain)
        if clipped:
            logger.warning(
                "speech_gain_clipped",
                extra={
                    "speech_utterance": str(job.utterance_id),
                    "speech_gain": self._gain,
                    "speech_clipped_samples": clipped,
                },
            )

        task = asyncio.create_task(self._sink.play(loud))
        self._task = task
        try:
            await task
        except PlaybackError as error:
            await self._fail(job, error, spoken=self._elapsed(job))
            return
        finally:
            # Whether this was a cancellation, a failure or a clean finish, the
            # device must not be left running: the next line is about to claim
            # it, and two of them at once is the one thing that cannot happen.
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self._task = None

        elapsed = self._elapsed(job)
        self.spoken += 1
        self.spoken_seconds += elapsed
        await self._finish(job, SpeechOutcome.SPOKEN, spoken=elapsed)

    def _elapsed(self, job: _Job) -> float:
        """How long this line has actually been audible."""

        if job.started_monotonic is None:
            return 0.0
        return max(0.0, self._monotonic() - job.started_monotonic)

    # --- outcomes ----------------------------------------------------------

    async def _report_interrupted(self, job: _Job) -> None:
        """Record a sentence that something louder took the room from."""

        self.interrupted += 1
        elapsed = self._elapsed(job)
        self.spoken_seconds += elapsed
        await self._finish(
            job,
            SpeechOutcome.INTERRUPTED,
            spoken=elapsed,
            detail="something louder arrived",
        )

    async def _fail(self, job: _Job, detail: object, spoken: float = 0.0) -> None:
        """Say nothing, record why, and carry on. Invisible to the player."""

        self.failed += 1
        text = str(detail)[:300]
        self.last_error = text
        logger.warning(
            "speech_failed",
            extra={
                "speech_utterance": str(job.utterance_id),
                "speech_detail": text,
            },
        )
        await self._finish(job, SpeechOutcome.FAILED, spoken=spoken, detail=text)

    async def _finish(
        self,
        job: _Job,
        outcome: SpeechOutcome,
        spoken: float = 0.0,
        detail: str | None = None,
    ) -> None:
        """Publish what became of one utterance, whatever it was."""

        if outcome is SpeechOutcome.SUPERSEDED:
            self.superseded += 1
        elif outcome is SpeechOutcome.DROPPED:
            self.dropped += 1

        shot_id = job.utterance.shot_id
        if outcome is SpeechOutcome.SPOKEN:
            logger.info(
                "speech_completed",
                extra={
                    "speech_utterance": str(job.utterance_id),
                    "speech_voice": self._voice,
                    "speech_seconds": round(spoken, 2),
                    "shot_id": None if shot_id is None else str(shot_id),
                },
            )
        else:
            logger.info(
                "speech_suppressed",
                extra={
                    "speech_utterance": str(job.utterance_id),
                    "speech_voice": self._voice,
                    "shot_id": None if shot_id is None else str(shot_id),
                    "coaching_reason": outcome.value,
                    "speech_detail": detail or outcome.value,
                },
            )

        await self._announce(
            SpeechCompleted(
                utterance_id=job.utterance_id,
                outcome=outcome,
                spoken_seconds=round(spoken, 3),
                shot_id=job.utterance.shot_id,
                detail=detail,
            )
        )

    async def _announce(self, event: Event) -> None:
        """Publish, and never let publishing be the thing that breaks.

        The bus stops before some of these can be sent during shutdown, and a
        speech event failing to reach nobody is not worth an exception escaping
        the last handler there is.
        """

        try:
            await self._publish(event)
        except Exception:
            logger.warning(
                "speech_event_unpublished",
                extra={"event_type": type(event).__name__},
                exc_info=True,
            )

    # --- reporting ---------------------------------------------------------

    def health(self) -> SpeechHealth:
        return SpeechHealth(
            enabled=self._enabled,
            available=self.is_available,
            engine=None if self._synthesizer is None else self._synthesizer.name,
            output=None if self._sink is None else self._sink.name,
            voice=self._voice,
            speaking=self._active is not None and self._active.is_speaking,
            queued=len(self._waiting),
            spoken=self.spoken,
            interrupted=self.interrupted,
            superseded=self.superseded,
            dropped=self.dropped,
            failed=self.failed,
            unavailable=self.unavailable,
            rendered=self.rendered,
            cache_hits=self._cache.hits,
            spoken_seconds=round(self.spoken_seconds, 2),
            last_error=self.last_error,
        )


def build_speech_service(
    publish: Publish,
    enabled: bool = False,
    engine: str = "kokoro",
    output: str = "sounddevice",
    voice: str = DEFAULT_VOICE,
    speed: float = 1.0,
    device: str = "",
    cache_root: Path | str | None = None,
    cache_bytes: int = 32 * 1024 * 1024,
    cache_files: int = 512,
    queue_size: int = 4,
    max_characters: int = 300,
    render_timeout_seconds: float = 20.0,
    open_timeout_seconds: float = 90.0,
    drain_timeout_seconds: float = 5.0,
    gain: float = 1.0,
    diagnostics: Any = None,
) -> SpeechService:
    """Assemble speech from plain values, so tests need no `Settings`."""

    from caddiej.speech.factory import build_sink, build_synthesizer

    # Nothing is constructed while speech is off, so a machine with no model
    # installed never even resolves the engine's import.
    synthesizer = (
        build_synthesizer(engine, voice=voice, speed=speed) if enabled else None
    )
    sink = build_sink(output, device=device) if enabled else None
    directory = None if cache_root is None else Path(cache_root)
    return SpeechService(
        publish=publish,
        synthesizer=synthesizer,
        sink=sink,
        cache=AudioCache(
            max_bytes=cache_bytes, directory=directory, max_files=cache_files
        ),
        enabled=enabled,
        voice=voice,
        speed=speed,
        queue_size=queue_size,
        max_characters=max_characters,
        render_timeout_seconds=render_timeout_seconds,
        open_timeout_seconds=open_timeout_seconds,
        drain_timeout_seconds=drain_timeout_seconds,
        gain=gain,
        diagnostics=diagnostics,
    )
