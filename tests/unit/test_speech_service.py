"""The speech service, with the voice and the speaker replaced by fakes.

No test in this file loads a model or makes a sound, which is why the awkward
cases — an engine that hangs, a speaker that fails halfway through a word, a
warning arriving while a coaching line is being spoken, two shots inside one
sentence — can all be arranged exactly rather than hoped for.

Two rules the whole subsystem rests on, and most of this file is about them.
Nothing overlaps: the sink is never asked for a second line while it is playing
the first. And nothing here is visible: every path ends in a `SpeechCompleted`
saying what became of the sentence, and nothing raises.
"""

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from caddiej.core.events import Event
from caddiej.domain.coaching import Advice, AdviceSource, CoachingFocus
from caddiej.domain.events import (
    AnalysisCompleted,
    ShotCompleted,
    SpeechCompleted,
    SpeechStarted,
)
from caddiej.domain.shot import BallLaunch, Shot, ShotSource
from caddiej.domain.speech import SpeechOutcome, SpeechPriority, Utterance
from caddiej.speech.audio import Audio, silence
from caddiej.speech.cache import AudioCache
from caddiej.speech.fakes import (
    BrokenSink,
    BrokenSynthesizer,
    FakeSynthesizer,
    RecordingSink,
    ScriptedSynthesizer,
)
from caddiej.speech.service import SpeechService
from caddiej.speech.sink import PlaybackUnavailableError
from caddiej.speech.synth import SynthesisFailedError, SynthesisUnavailableError

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 4, 1, 18, 30, tzinfo=UTC)
LINE = "Square the face a little earlier."


class Bus:
    """Collects what was published, in order."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def publish(self, event: Event) -> None:
        self.events.append(event)

    def of[EventType: Event](self, kind: type[EventType]) -> list[EventType]:
        return [event for event in self.events if isinstance(event, kind)]

    @property
    def outcomes(self) -> list[SpeechOutcome]:
        return [event.outcome for event in self.of(SpeechCompleted)]

    @property
    def envelopes(self) -> list[tuple[float, ...]]:
        return [event.envelope.values for event in self.of(SpeechStarted)]


def a_quiet_tone(frames: int = 240) -> Audio:
    """Audio well under the rail, so doubling it cannot be mistaken for
    clipping."""

    samples = (round(4000 * ((index % 20) - 10) / 10) for index in range(frames))
    return Audio(
        data=b"".join(value.to_bytes(2, "little", signed=True) for value in samples)
    )


def peak_of(audio: Audio) -> int:
    values = [
        int.from_bytes(audio.data[index : index + 2], "little", signed=True)
        for index in range(0, len(audio.data), 2)
    ]
    return max(max(values), -min(values))


class Table:
    """A speech service with fakes in place of a voice and a speaker."""

    def __init__(self, **options: object) -> None:
        self.bus = Bus()
        engine = options.pop("engine", None)
        sink = options.pop("sink", None)
        cache = options.pop("cache", None)
        # Explicitly against None throughout: a cache defines `__len__`, so an
        # empty one is falsy, and `cache or AudioCache()` would quietly discard
        # the very thing a cache test had just set up.
        self.engine: FakeSynthesizer = (
            engine if engine is not None else FakeSynthesizer()  # type: ignore[assignment]
        )
        self.sink: RecordingSink = (
            sink if sink is not None else RecordingSink()  # type: ignore[assignment]
        )
        self.service = SpeechService(
            publish=self.bus.publish,
            synthesizer=self.engine,
            sink=self.sink,
            cache=cache if cache is not None else AudioCache(),  # type: ignore[arg-type]
            **options,  # type: ignore[arg-type]
        )

    async def __aenter__(self) -> "Table":
        await self.service.open()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.service.aclose()

    async def say(self, text: str = LINE, **options: object) -> bool:
        return await self.service.say(Utterance(text=text, **options))  # type: ignore[arg-type]

    async def settled(self, timeout: float = 2.0) -> None:
        """Let the worker finish everything it has.

        Yields rather than sleeps for the first hundred turns, so a test with
        no real delays in it costs nothing, then falls back to small waits for
        the few that deliberately involve the clock.
        """

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        turns = 0
        while loop.time() < deadline:
            if self.service.is_idle:
                return
            turns += 1
            await asyncio.sleep(0 if turns < 100 else 0.001)
        raise AssertionError("the speech service never went idle")


def a_shot(shot_id: UUID) -> Shot:
    """The barest shot there is. Nothing here reads anything off it but the id."""

    return Shot(
        shot_id=shot_id,
        source=ShotSource(
            kind="protee_log",
            device_session_id=uuid4(),
            observed_at=NOW,
            observed_monotonic=1.0,
        ),
        ball=BallLaunch(speed_mph=112.0),
    )


def an_advice(text: str = LINE, shot_id: UUID | None = None) -> AnalysisCompleted:
    return AnalysisCompleted(
        shot_id=shot_id or uuid4(),
        advice=Advice(
            shot_id=uuid4(),
            focus=CoachingFocus.FACE_CONTROL,
            display="FACE OPEN",
            speech=text,
            severity=0.6,
            created_at=NOW,
            source=AdviceSource.MODEL,
        ),
    )


class TestSpeakingALine:
    async def test_a_coached_line_is_rendered_and_played(self) -> None:
        async with Table() as table:
            await table.service.on_analysis_completed(an_advice())
            await table.settled()

        assert table.engine.calls == [(LINE, "bm_george")]
        assert len(table.sink.played) == 1

    async def test_starting_and_finishing_are_both_announced(self) -> None:
        async with Table() as table:
            await table.service.on_analysis_completed(an_advice())
            await table.settled()

        started = table.bus.of(SpeechStarted)
        completed = table.bus.of(SpeechCompleted)
        assert len(started) == 1
        assert len(completed) == 1
        assert started[0].utterance_id == completed[0].utterance_id
        assert completed[0].outcome is SpeechOutcome.SPOKEN

    async def test_the_shot_a_line_is_about_travels_with_it(self) -> None:
        """So a listener can tie what is being said to what is on the floor."""

        shot_id = uuid4()
        async with Table() as table:
            await table.service.on_analysis_completed(an_advice(shot_id=shot_id))
            await table.settled()

        assert table.bus.of(SpeechStarted)[0].shot_id == shot_id
        assert table.bus.of(SpeechCompleted)[0].shot_id == shot_id

    async def test_how_long_the_line_runs_is_known_before_it_starts(self) -> None:
        """Published up front so a listener can plan around the whole sentence
        rather than discovering its length when it ends."""

        async with Table() as table:
            await table.say("Keep the tempo smooth.")
            await table.settled()

        started = table.bus.of(SpeechStarted)[0]
        assert started.duration_seconds > 0
        assert started.duration_seconds == pytest.approx(
            table.sink.played[0].duration_seconds
        )

    async def test_untidy_whitespace_is_collapsed_before_it_is_spoken(self) -> None:
        async with Table() as table:
            await table.say("  Square   the face.\n")
            await table.settled()

        assert table.engine.calls == [("Square the face.", "bm_george")]

    async def test_an_empty_line_is_not_spoken_and_is_not_an_event(self) -> None:
        async with Table() as table:
            assert await table.say("   ") is False
            await table.settled()

        assert table.engine.calls == []
        assert table.bus.events == []

    async def test_an_oversized_line_is_refused_rather_than_truncated(self) -> None:
        """Truncating would produce a confident half-instruction. The cap is
        about occupying the room, not about the words."""

        async with Table(max_characters=40) as table:
            assert await table.say("x" * 60) is False
            await table.settled()

        assert table.engine.calls == []

    async def test_the_configured_voice_is_used_unless_one_is_named(self) -> None:
        async with Table(voice="bm_lewis") as table:
            await table.say("First.", perishable=False)
            await table.say("Second.", voice="bf_emma", perishable=False)
            await table.settled()

        assert table.engine.calls == [("First.", "bm_lewis"), ("Second.", "bf_emma")]


class TestOneVoiceAtATime:
    async def test_a_second_line_waits_for_the_first_to_finish(self) -> None:
        """The guarantee the whole worker exists for: the sink is never handed a
        second sentence while it is playing the first."""

        gate = asyncio.Event()
        async with Table(sink=RecordingSink(gate=gate)) as table:
            await table.say("First.", perishable=False)
            await table.say("Second.", perishable=False)

            for _ in range(50):
                await asyncio.sleep(0)
            assert len(table.sink.started) == 1

            gate.set()
            await table.settled()

        assert [audio.duration_seconds for audio in table.sink.played] == [
            pytest.approx(len("First.") * 0.05),
            pytest.approx(len("Second.") * 0.05),
        ]

    async def test_lines_are_played_in_the_order_they_were_accepted(self) -> None:
        async with Table() as table:
            for index in range(4):
                await table.say(f"Line number {index}.", perishable=False)
            await table.settled()

        assert table.engine.calls == [
            (f"Line number {index}.", "bm_george") for index in range(4)
        ]


class TestSupersedingAndDropping:
    async def test_a_newer_coaching_line_replaces_an_older_waiting_one(self) -> None:
        """Advice for the shot before last is worse than silence."""

        gate = asyncio.Event()
        async with Table(sink=RecordingSink(gate=gate)) as table:
            await table.say("Playing now.")
            for _ in range(20):
                await asyncio.sleep(0)

            await table.say("Waiting.")
            await table.say("Newer, and the only one worth saying.")
            gate.set()
            await table.settled()

        assert SpeechOutcome.SUPERSEDED in table.bus.outcomes
        assert "Waiting." not in [text for text, _ in table.engine.calls]
        assert table.service.health().superseded == 1

    async def test_a_newer_line_abandons_one_still_being_rendered(self) -> None:
        """A sentence takes about a second to render, which is long enough for
        the next shot. Abandoning one that has not started playing costs nothing
        and saves the room a line about the shot before last."""

        async with Table(engine=FakeSynthesizer(delay=0.05)) as table:
            await table.say("About the shot before last.")
            await asyncio.sleep(0.01)
            await table.say("About the shot just hit.")
            await table.settled()

        assert table.bus.outcomes == [SpeechOutcome.SUPERSEDED, SpeechOutcome.SPOKEN]
        assert len(table.sink.started) == 1
        assert table.bus.of(SpeechStarted)[0].text == "About the shot just hit."

    async def test_a_line_already_being_spoken_is_allowed_to_finish(self) -> None:
        """The other half of the same rule: cutting an audible sentence in half
        to start another of equal importance sounds like a fault."""

        gate = asyncio.Event()
        async with Table(sink=RecordingSink(gate=gate)) as table:
            await table.say("Playing now.")
            for _ in range(20):
                await asyncio.sleep(0)

            await table.say("A newer coaching line.")
            gate.set()
            await table.settled()

        assert table.sink.interrupted == 0
        assert table.bus.outcomes == [SpeechOutcome.SPOKEN, SpeechOutcome.SPOKEN]

    async def test_another_swing_forgets_a_line_nobody_has_heard_yet(self) -> None:
        """The case supersession cannot reach.

        A newer coaching line replaces an older one, but there is not always a
        newer one: the next shot may be worth no words at all, or the model may
        fail on it, or nothing about it may have been measured. Without this the
        old sentence is still queued, and it would be spoken to somebody who has
        already hit again — who would hear it as advice about the swing they
        just made.
        """

        gate = asyncio.Event()
        first, second = uuid4(), uuid4()
        async with Table(sink=RecordingSink(gate=gate)) as table:
            await table.say("Playing now.", perishable=False)
            for _ in range(20):
                await asyncio.sleep(0)

            await table.say("About the shot before last.", shot_id=first)
            await table.service.on_shot_completed(ShotCompleted(shot=a_shot(second)))
            gate.set()
            await table.settled()

        spoken = [text for text, _ in table.engine.calls]
        assert "About the shot before last." not in spoken
        assert SpeechOutcome.SUPERSEDED in table.bus.outcomes

    async def test_the_shot_that_was_just_hit_keeps_its_own_line(self) -> None:
        """The obvious other half, and the reason the shot is compared rather
        than the queue simply being emptied: coaching about a shot arrives
        after that shot was completed, so a blanket clear would silence
        every line CaddieJ ever had."""

        shot_id = uuid4()
        async with Table() as table:
            await table.service.on_shot_completed(ShotCompleted(shot=a_shot(shot_id)))
            await table.service.on_analysis_completed(an_advice(shot_id=shot_id))
            await table.settled()

        assert table.engine.calls == [(LINE, "bm_george")]

    async def test_the_current_shot_does_not_cancel_its_own_waiting_line(
        self,
    ) -> None:
        """ShotCompleted for this swing must not eat the line still waiting.

        The line is queued after the shot, but a late copy of the same
        completion — or the completion being handled while the line is only
        queued — has to leave it alone. Cancelling here is how the current
        shot goes silent before the speaker starts.
        """

        shot_id = uuid4()
        gate = asyncio.Event()
        async with Table(sink=RecordingSink(gate=gate)) as table:
            await table.say("About this shot.", shot_id=shot_id)
            await table.service.on_shot_completed(ShotCompleted(shot=a_shot(shot_id)))
            gate.set()
            await table.settled()

        assert table.engine.calls
        assert SpeechOutcome.SPOKEN in table.bus.outcomes
        assert SpeechOutcome.SUPERSEDED not in table.bus.outcomes

    async def test_a_spoken_line_logs_speech_completed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO):
            async with Table() as table:
                await table.say(LINE)
                await table.settled()

        events = [record.getMessage() for record in caplog.records]
        assert "speech_started" in events
        assert "speech_completed" in events

    async def test_unavailable_speech_says_why(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO):
            async with Table(enabled=False) as table:
                assert await table.say(LINE) is False

        assert any(
            record.getMessage() == "speech_suppressed"
            and getattr(record, "speech_detail", "") == "speech is not available"
            for record in caplog.records
        )

    async def test_another_swing_leaves_a_warning_alone(self) -> None:
        """A line with no shot attached is not about a swing, so a swing cannot
        make it stale."""

        gate = asyncio.Event()
        async with Table(sink=RecordingSink(gate=gate)) as table:
            await table.say("Playing now.", perishable=False)
            for _ in range(20):
                await asyncio.sleep(0)

            await table.say("Mind the club behind you.", priority=SpeechPriority.ALERT)
            await table.service.on_shot_completed(ShotCompleted(shot=a_shot(uuid4())))
            gate.set()
            await table.settled()

        spoken = [text for text, _ in table.engine.calls]
        assert "Mind the club behind you." in spoken

    async def test_a_line_that_does_not_go_stale_is_not_replaced(self) -> None:
        """Two warnings are two things worth hearing, unlike two coaching
        lines about two different swings."""

        gate = asyncio.Event()
        async with Table(sink=RecordingSink(gate=gate)) as table:
            await table.say("Playing now.", perishable=False)
            for _ in range(20):
                await asyncio.sleep(0)

            await table.say("First warning.", perishable=False)
            await table.say("Second warning.", perishable=False)
            gate.set()
            await table.settled()

        assert SpeechOutcome.SUPERSEDED not in table.bus.outcomes
        assert len(table.sink.played) == 3

    async def test_the_queue_is_bounded_and_says_what_it_dropped(self) -> None:
        gate = asyncio.Event()
        async with Table(sink=RecordingSink(gate=gate), queue_size=2) as table:
            await table.say("Playing now.", perishable=False)
            for _ in range(20):
                await asyncio.sleep(0)

            for index in range(5):
                await table.say(f"Waiting {index}.", perishable=False)
            gate.set()
            await table.settled()

        assert table.service.health().dropped == 3
        assert SpeechOutcome.DROPPED in table.bus.outcomes

    async def test_a_full_queue_refuses_the_arriving_line_not_an_urgent_waiting_one(
        self,
    ) -> None:
        """Dropping from the tail is what makes this true without a special
        case: the tail is the least urgent, most recent thing there is."""

        gate = asyncio.Event()
        async with Table(sink=RecordingSink(gate=gate), queue_size=1) as table:
            await table.say("Playing now.", perishable=False)
            for _ in range(20):
                await asyncio.sleep(0)

            assert await table.say(
                "An alert.", priority=SpeechPriority.ALERT, perishable=False
            )
            assert (
                await table.say("Coaching.", priority=SpeechPriority.COACHING) is False
            )
            gate.set()
            await table.settled()

        spoken = [text for text, _ in table.engine.calls]
        assert "An alert." in spoken
        assert "Coaching." not in spoken


class TestPriority:
    async def test_something_urgent_jumps_the_queue(self) -> None:
        gate = asyncio.Event()
        async with Table(sink=RecordingSink(gate=gate)) as table:
            await table.say("Playing now.", perishable=False)
            for _ in range(20):
                await asyncio.sleep(0)

            await table.say("Ordinary.", perishable=False)
            await table.say("Urgent.", priority=SpeechPriority.ALERT, perishable=False)
            gate.set()
            await table.settled()

        spoken = [text for text, _ in table.engine.calls]
        assert spoken == ["Playing now.", "Urgent.", "Ordinary."]

    async def test_equally_urgent_lines_keep_the_order_they_arrived(self) -> None:
        gate = asyncio.Event()
        async with Table(sink=RecordingSink(gate=gate)) as table:
            await table.say("Playing now.", perishable=False)
            for _ in range(20):
                await asyncio.sleep(0)

            for index in range(3):
                await table.say(
                    f"Alert {index}.",
                    priority=SpeechPriority.ALERT,
                    perishable=False,
                )
            gate.set()
            await table.settled()

        spoken = [text for text, _ in table.engine.calls]
        assert spoken == ["Playing now.", "Alert 0.", "Alert 1.", "Alert 2."]


class TestInterruption:
    async def test_something_louder_cuts_a_sentence_short(self) -> None:
        gate = asyncio.Event()
        async with Table(sink=RecordingSink(gate=gate)) as table:
            await table.say("A long coaching line that is going to be cut off.")
            for _ in range(20):
                await asyncio.sleep(0)
            assert len(table.sink.started) == 1

            await table.say("Urgent.", priority=SpeechPriority.ALERT, perishable=False)
            gate.set()
            await table.settled()

        # Started twice, finished once: the tail of the first never reached the
        # room, which is exactly what being interrupted means.
        assert len(table.sink.started) == 2
        assert len(table.sink.played) == 1
        assert table.sink.interrupted == 1
        assert table.bus.outcomes == [SpeechOutcome.INTERRUPTED, SpeechOutcome.SPOKEN]

    async def test_an_interrupted_line_reports_how_much_was_heard(self) -> None:
        gate = asyncio.Event()
        async with Table(sink=RecordingSink(gate=gate)) as table:
            await table.say("Cut me off.")
            for _ in range(20):
                await asyncio.sleep(0)
            await table.say("Now.", priority=SpeechPriority.ALERT, perishable=False)
            gate.set()
            await table.settled()

        interrupted = table.bus.of(SpeechCompleted)[0]
        assert interrupted.outcome is SpeechOutcome.INTERRUPTED
        assert interrupted.spoken_seconds >= 0.0
        assert table.service.health().interrupted == 1

    async def test_a_coaching_line_never_interrupts_another_one(self) -> None:
        """Cutting a two-second sentence in half to start another two-second
        sentence would sound broken. The waiting one is replaced instead."""

        gate = asyncio.Event()
        async with Table(sink=RecordingSink(gate=gate)) as table:
            await table.say("Playing now.")
            for _ in range(20):
                await asyncio.sleep(0)

            await table.say("A newer coaching line.")
            gate.set()
            await table.settled()

        assert table.sink.interrupted == 0
        assert len(table.sink.played) == 2

    async def test_a_line_overruled_while_it_was_still_being_rendered_is_not_spoken(
        self,
    ) -> None:
        """The gap between rendering and playing is the one place there is no
        task to cancel, so starting the loser there is the overlap this service
        exists to prevent."""

        engine = FakeSynthesizer(delay=0.05)
        async with Table(engine=engine, sink=RecordingSink()) as table:
            await table.say("Being rendered.")
            await asyncio.sleep(0.01)
            await table.say("Now.", priority=SpeechPriority.ALERT, perishable=False)
            await table.settled()

        # The loser never reached the speaker at all, so there is exactly one
        # `SpeechStarted` and it belongs to the line that won.
        assert len(table.sink.started) == 1
        assert len(table.sink.played) == 1
        assert table.sink.interrupted == 0
        assert table.bus.outcomes == [SpeechOutcome.INTERRUPTED, SpeechOutcome.SPOKEN]
        assert len(table.bus.of(SpeechStarted)) == 1
        assert table.bus.of(SpeechStarted)[0].text == "Now."


class TestCache:
    async def test_the_same_sentence_is_rendered_once(self) -> None:
        async with Table() as table:
            await table.say("Square the face.", perishable=False)
            await table.settled()
            await table.say("Square the face.", perishable=False)
            await table.settled()

        assert len(table.engine.calls) == 1
        assert len(table.sink.played) == 2
        assert table.service.health().cache_hits == 1

    async def test_a_cached_line_still_announces_itself_the_same_way(self) -> None:
        async with Table() as table:
            for _ in range(2):
                await table.say("Square the face.", perishable=False)
                await table.settled()

        assert len(table.bus.of(SpeechStarted)) == 2
        assert table.bus.outcomes == [SpeechOutcome.SPOKEN, SpeechOutcome.SPOKEN]

    async def test_a_line_rendered_last_time_survives_a_restart(
        self, tmp_path: Path
    ) -> None:
        """The first coaching line after starting CaddieJ is the one somebody
        is actually waiting for."""

        first = Table(cache=AudioCache(directory=tmp_path))
        async with first:
            await first.say("Square the face.")
            await first.settled()

        second = Table(cache=AudioCache(directory=tmp_path))
        async with second:
            await second.say("Square the face.")
            await second.settled()

        assert second.engine.calls == []
        assert len(second.sink.played) == 1

    async def test_a_different_voice_is_a_different_recording(self) -> None:
        async with Table() as table:
            await table.say("Square the face.", perishable=False)
            await table.settled()
            await table.say("Square the face.", voice="bf_emma", perishable=False)
            await table.settled()

        assert len(table.engine.calls) == 2


class TestFailures:
    async def test_a_voice_that_breaks_says_so_and_keeps_the_service_alive(
        self,
    ) -> None:
        engine = FakeSynthesizer(error=SynthesisFailedError("the model fell over"))
        async with Table(engine=engine) as table:
            await table.say("This will not be spoken.")
            await table.settled()

            engine.error = None
            await table.say("This one will.", perishable=False)
            await table.settled()

        assert table.bus.outcomes == [SpeechOutcome.FAILED, SpeechOutcome.SPOKEN]
        assert table.service.health().failed == 1
        assert len(table.sink.played) == 1

    async def test_a_voice_that_never_answers_is_given_up_on(self) -> None:
        async with Table(
            engine=FakeSynthesizer(delay=5.0), render_timeout_seconds=0.02
        ) as table:
            await table.say("This will hang.")
            await table.settled()

        assert table.bus.outcomes == [SpeechOutcome.FAILED]
        assert "longer than" in (table.service.health().last_error or "")
        assert table.sink.started == []

    async def test_a_speaker_that_fails_mid_word_is_survivable(self) -> None:
        async with Table(sink=BrokenSink()) as table:  # type: ignore[arg-type]
            await table.say("The speaker is about to go away.")
            await table.settled()

        assert table.bus.outcomes == [SpeechOutcome.FAILED]
        assert table.service.health().failed == 1

    async def test_an_engine_that_breaks_in_an_unexpected_way_is_still_survivable(
        self,
    ) -> None:
        """The last handler there is. A bug in the subsystem must not reach the
        supervisor, which would read it as grounds to shut CaddieJ down over a
        spoken line."""

        async with Table(engine=BrokenSynthesizer(error=RuntimeError("boom"))) as table:  # type: ignore[arg-type]
            await table.say("Something unexpected happens now.")
            await table.settled()

        assert table.bus.outcomes == [SpeechOutcome.FAILED]

    async def test_a_bus_that_has_gone_away_does_not_take_the_worker_with_it(
        self,
    ) -> None:
        table = Table()

        async def refuse(event: Event) -> None:
            raise RuntimeError("Event bus is not running")

        table.service._publish = refuse  # noqa: SLF001 - simulating a stopped bus
        async with table:
            await table.say("Nobody will hear about this.")
            await table.settled()

        assert len(table.sink.played) == 1


class TestAvailability:
    async def test_speech_switched_off_never_touches_the_engine(self) -> None:
        table = Table(enabled=False)
        async with table:
            assert await table.say("Nothing happens.") is False

        assert table.engine.opened == 0
        assert table.bus.events == []

    async def test_a_voice_that_will_not_load_leaves_the_room_quiet(self) -> None:
        engine = FakeSynthesizer(
            open_error=SynthesisUnavailableError("no model on this machine")
        )
        table = Table(engine=engine)
        async with table:
            assert table.service.is_available is False
            assert await table.say("Nothing happens.") is False

        health = table.service.health()
        assert health.enabled is True
        assert health.available is False
        assert "no model" in (health.last_error or "")

    async def test_a_machine_with_no_speaker_leaves_the_room_quiet(self) -> None:
        sink = RecordingSink(open_error=PlaybackUnavailableError("no audio output"))
        table = Table(sink=sink)
        async with table:
            assert table.service.is_available is False

        assert table.service.health().available is False

    async def test_a_voice_that_never_arrives_does_not_hold_up_start_up(self) -> None:
        """Loading a voice means downloading one on a first run, and everything
        after speech in start-up waits behind it. A quiet room is a supported way
        to run CaddieJ; a start-up that never finishes is not."""

        engine = FakeSynthesizer(open_delay=30.0)
        table = Table(engine=engine, open_timeout_seconds=0.05)

        await asyncio.wait_for(table.service.open(), timeout=2.0)
        try:
            assert table.service.is_available is False
            assert await table.say("Nothing happens.") is False
        finally:
            await table.service.aclose()

        assert "did not load" in (table.service.health().last_error or "")

    async def test_a_speaker_that_fails_gives_the_voice_back(self) -> None:
        """The voice loads first, and it is a few hundred megabytes of model. A
        sound card that then refuses must not leave that loaded for a subsystem
        which has just switched itself off."""

        engine = FakeSynthesizer()
        sink = RecordingSink(open_error=PlaybackUnavailableError("no audio output"))
        table = Table(engine=engine, sink=sink)

        await table.service.open()

        assert engine.opened == 1
        assert engine.closed == 1, "the model was left loaded"

    async def test_an_unwritable_cache_is_not_a_reason_not_to_speak(
        self, tmp_path: Path
    ) -> None:
        """The cache saves a second of CPU per repeated line, and opening it
        touches a disk — which is real work inside a method that promises not to
        raise. Nothing about it is worth a room that stays quiet."""

        blocked = tmp_path / "in-the-way"
        blocked.write_text("a file where a directory should be", encoding="utf-8")
        table = Table(cache=AudioCache(directory=blocked / "speech"))

        async with table:
            assert table.service.is_available is True
            assert await table.say("Something happens.") is True
            await table.settled()

        assert table.sink.played

    async def test_an_unconfigured_engine_is_never_opened(self) -> None:
        engine = FakeSynthesizer(configured=False)
        table = Table(engine=engine)
        async with table:
            assert table.service.is_available is False

        assert engine.opened == 0

    async def test_lines_that_could_not_be_said_are_counted(self) -> None:
        table = Table(enabled=False)
        async with table:
            for _ in range(3):
                await table.say("Nothing happens.")

        assert table.service.health().unavailable == 3


class TestLifecycle:
    async def test_a_service_that_was_never_opened_closes_cleanly(self) -> None:
        table = Table(enabled=False)
        await table.service.aclose()

    async def test_closing_abandons_what_is_still_waiting(self) -> None:
        """A queue drained on the way out would keep the room talking after it
        was told to stop."""

        gate = asyncio.Event()
        table = Table(sink=RecordingSink(gate=gate))
        await table.service.open()
        await table.say("Playing now.", perishable=False)
        for _ in range(20):
            await asyncio.sleep(0)
        await table.say("Never said.", perishable=False)

        gate.set()
        await table.service.aclose()

        assert "Never said." not in [text for text, _ in table.engine.calls]
        assert SpeechOutcome.DROPPED in table.bus.outcomes

    async def test_closing_lets_the_current_sentence_finish(self) -> None:
        """Cutting a voice off at the exact moment somebody presses Ctrl-C
        sounds like a crash."""

        table = Table(sink=RecordingSink(realtime=True, speed=50.0))
        await table.service.open()
        await table.say("A line that is midway through.")
        for _ in range(20):
            await asyncio.sleep(0)

        await table.service.aclose()

        assert len(table.sink.played) == 1
        assert table.bus.outcomes == [SpeechOutcome.SPOKEN]

    async def test_a_sentence_that_will_not_end_does_not_hold_up_shutdown(self) -> None:
        gate = asyncio.Event()
        table = Table(sink=RecordingSink(gate=gate), drain_timeout_seconds=0.02)
        await table.service.open()
        await table.say("This never finishes.")
        for _ in range(20):
            await asyncio.sleep(0)

        await table.service.aclose()

        assert table.sink.closed == 1
        assert table.service.is_available is False

    async def test_closing_releases_the_speaker_and_the_voice(self) -> None:
        table = Table()
        async with table:
            await table.say("Anything.")
            await table.settled()

        assert table.sink.closed == 1

    async def test_nothing_is_said_after_the_service_has_been_closed(self) -> None:
        table = Table()
        async with table:
            pass

        assert await table.say("Too late.") is False


class TestScriptedVoice:
    async def test_a_scripted_voice_plays_exactly_what_it_was_given(self) -> None:
        engine = ScriptedSynthesizer(replies=(silence(0.5), silence(1.25)))
        table = Table(engine=engine)  # type: ignore[arg-type]
        async with table:
            await table.say("First.", perishable=False)
            await table.say("Second.", perishable=False)
            await table.settled()

        assert [audio.duration_seconds for audio in table.sink.played] == [
            pytest.approx(0.5),
            pytest.approx(1.25),
        ]


class TestVolume:
    """Gain is a property of the speaker, not of the sentence.

    It is applied after the cache and after the envelope, which is what lets
    the bay be turned up without retiring a single rendered file and without
    the lamps reacting to anything but the shape of the words.
    """

    async def test_the_speaker_is_handed_the_amplified_samples(self) -> None:
        engine = ScriptedSynthesizer(replies=(a_quiet_tone(),))
        table = Table(engine=engine, gain=2.0)  # type: ignore[arg-type]
        async with table:
            await table.say("Turn it up.", perishable=False)
            await table.settled()

        assert peak_of(table.sink.played[0]) == 2 * peak_of(a_quiet_tone())

    async def test_the_cache_keeps_the_engine_s_own_level(self) -> None:
        """A cached WAV that already had the gain baked in would play at the
        wrong volume the moment the setting changed, and nothing would say so."""

        cache = AudioCache()
        engine = ScriptedSynthesizer(replies=(a_quiet_tone(), a_quiet_tone()))
        table = Table(engine=engine, cache=cache, gain=2.0)  # type: ignore[arg-type]
        async with table:
            await table.say("Said twice.", perishable=False)
            await table.settled()
            await table.say("Said twice.", perishable=False)
            await table.settled()

        assert cache.hits == 1
        played = [peak_of(audio) for audio in table.sink.played]
        assert played == [2 * peak_of(a_quiet_tone())] * 2

    async def test_the_lights_still_see_the_unamplified_shape(self) -> None:
        engine = ScriptedSynthesizer(replies=(a_quiet_tone(),))
        loud = Table(engine=engine, gain=4.0)  # type: ignore[arg-type]
        async with loud:
            await loud.say("Loud.", perishable=False)
            await loud.settled()

        engine = ScriptedSynthesizer(replies=(a_quiet_tone(),))
        quiet = Table(engine=engine, gain=1.0)  # type: ignore[arg-type]
        async with quiet:
            await quiet.say("Quiet.", perishable=False)
            await quiet.settled()

        assert loud.bus.envelopes == quiet.bus.envelopes

    async def test_a_gain_that_could_only_distort_is_refused_at_construction(
        self,
    ) -> None:
        with pytest.raises(ValueError, match="gain must be"):
            Table(gain=9.0)
