"""Simulated vision and wording delays. These numbers are not GPT latency."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from tests.unit.reference_support import IRON, SHOT_DIR, reference
from tests.unit.test_swing_reference_live import (
    _capture,
    _package_dir,
    _Provider,
    _stored,
)

from caddiej.domain.coaching import AdviceFailure
from caddiej.domain.events import (
    AnalysisCompleted,
    AnalysisFailed,
    ShotCompleted,
    SpeechCompleted,
    SpeechStarted,
    SwingFramesReady,
)
from caddiej.domain.shot import BallLaunch, Shot, ShotSource
from caddiej.domain.speech import SpeechOutcome, SpeechPriority
from caddiej.swing_reference.feedback import FeedbackStrategy
from caddiej.swing_reference.live import ReferenceLiveCoach
from caddiej.swing_reference.timing import budget_for

_OLD = (
    "Keep the posture you started with.",
    "EARLY_EXTENSION",
    "Path more in",
)


class SimClock:
    """A monotonic clock that moves only when a test advances it."""

    def __init__(self) -> None:
        self.ms = 0

    def monotonic(self) -> float:
        return self.ms / 1000

    def advance_ms(self, milliseconds: int) -> None:
        self.ms += milliseconds

    async def sleep_ms(self, milliseconds: int) -> None:
        await asyncio.sleep(0)
        self.advance_ms(milliseconds)


class DelayedProvider(_Provider):
    def __init__(
        self,
        book: object,
        clock: SimClock,
        *,
        vision_ms: int,
        wording_ms: int,
        hang_on_wording: bool = False,
    ) -> None:
        super().__init__(book)  # type: ignore[arg-type]
        self.clock = clock
        self.vision_ms = vision_ms
        self.wording_ms = wording_ms
        self.hang_on_wording = hang_on_wording
        self.input_tokens = 1200
        self.output_tokens = 40
        self.cached_tokens = 900

    async def complete(self, request: object):
        from caddiej.coaching.provider import ProviderReply

        kind = request.schema_name  # type: ignore[attr-defined]
        if kind == "swing_feedback_v1":
            self.requests.append(request)
            if self.hang_on_wording:
                await asyncio.Event().wait()
            await self.clock.sleep_ms(self.wording_ms)
            return ProviderReply(
                payload={
                    "personal_observation": "The clubhead is inside the hands.",
                    "spoken_feedback": "Keep your trail elbow tucked and swing slower.",
                    "short_feedback": "Tuck the elbow",
                    "referenced_rule_id": "takeaway.clubhead_position.keep_head_in_front",
                    "referenced_checkpoint_id": "takeaway.clubhead_position",
                },
                model=self.model,
                latency_seconds=self.wording_ms / 1000,
                input_tokens=80,
                output_tokens=20,
                cached_tokens=0,
            )
        await self.clock.sleep_ms(self.vision_ms)
        reply = await super().complete(request)
        return replace(
            reply,
            latency_seconds=self.vision_ms / 1000,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cached_tokens=self.cached_tokens,
        )


def _shot(shot_id: object | None = None) -> Shot:
    return Shot(
        shot_id=shot_id or uuid4(),  # type: ignore[arg-type]
        source=ShotSource(
            kind="synthetic",
            device_session_id=uuid4(),
            observed_at=datetime.now(UTC),
            observed_monotonic=0.0,
        ),
        ball=BallLaunch(speed_mph=140.0),
    )


async def _run(
    tmp: Path,
    *,
    strategy: FeedbackStrategy,
    vision_ms: int,
    wording_ms: int,
    name: str,
    hang_on_wording: bool = False,
) -> tuple[ReferenceLiveCoach, list[object], object]:
    book = reference()
    clock = SimClock()
    provider = DelayedProvider(
        book,
        clock,
        vision_ms=vision_ms,
        wording_ms=wording_ms,
        hang_on_wording=hang_on_wording,
    )
    events: list[object] = []

    async def collect(event: object) -> None:
        events.append(event)

    coach = ReferenceLiveCoach(
        collect,
        provider,
        vision_calls=True,
        reference=book,
        context=IRON,
        feedback=strategy,
        clock=clock.monotonic,
        simulated=True,
    )
    await coach.open()
    capture = _capture(_package_dir(tmp, name))
    await coach.on_shot(ShotCompleted(shot=_shot(capture.coaching_id), trigger_id="1"))
    clock.advance_ms(100)
    await coach.on_frames(SwingFramesReady(capture=capture))
    await asyncio.wait_for(coach.settle(), timeout=2)
    clock.advance_ms(80)
    await coach.on_speech_started(
        SpeechStarted(
            utterance_id=uuid4(),
            text="line",
            voice="test",
            priority=SpeechPriority.COACHING,
            duration_seconds=1.0,
            shot_id=capture.coaching_id,
        )
    )
    return coach, events, provider


@pytest.mark.asyncio
async def test_simulated_matrix_labels_itself_and_keeps_the_correction(
    tmp_path: Path,
) -> None:
    """Every cell is a simulated delay. None of them is a GPT measurement."""

    book = reference()
    stored = _stored(book)
    visions = (500, 1500, 3000, 5000)
    wordings = (200, 500, 1000, 2000)
    strategies: tuple[FeedbackStrategy, ...] = ("exact", "observed", "worded")
    for strategy in strategies:
        wording_cases = wordings if strategy == "worded" else (wordings[0],)
        for vision_ms in visions:
            for wording_ms in wording_cases:
                coach, events, provider = await _run(
                    tmp_path,
                    strategy=strategy,
                    vision_ms=vision_ms,
                    wording_ms=wording_ms,
                    name=f"{strategy}-{vision_ms}-{wording_ms}",
                )
                completed = [
                    item for item in events if isinstance(item, AnalysisCompleted)
                ]
                assert len(completed) == 1
                speech = completed[0].advice.speech
                assert speech.endswith(stored)
                assert completed[0].advice.display == speech
                assert all(line not in speech for line in _OLD)
                assert "trail elbow" not in speech
                trace = coach.timing(completed[0].shot_id)
                assert trace is not None
                report = trace.durations()
                assert trace.simulated is True
                assert report["capture_ms"] == 100
                assert report["vision_ms"] == vision_ms
                assert report["tts_startup_ms"] == 80
                expected_wording = wording_ms if strategy == "worded" else None
                assert report["wording_ms"] == expected_wording
                extra = wording_ms if strategy == "worded" else 0
                assert report["shot_to_hud_ms"] == 100 + vision_ms + extra
                assert report["shot_to_audio_ms"] == 100 + vision_ms + extra + 80
                assert budget_for(report["shot_to_audio_ms"]) == _band(
                    100 + vision_ms + extra + 80
                )
                schemas = [item.schema_name for item in provider.requests]
                if strategy == "worded":
                    assert schemas == ["swing_assessment_v2", "swing_feedback_v1"]
                    assert trace.wording_used is True
                else:
                    assert schemas == ["swing_assessment_v2"]
                    assert trace.wording_used is False
                assert trace.input_tokens == 1200
                assert trace.cached_tokens == 900
                assert trace.image_detail == "high"
                assert trace.delivered is True
                if strategy == "exact":
                    assert speech == stored
                    assert trace.observation_included is False
                if strategy == "observed":
                    assert speech.startswith("Visible inside at ")
                    assert trace.observation_included is True
                if strategy == "worded":
                    assert speech.startswith("The clubhead is inside the hands.")
                    assert trace.observation_included is True


def _band(milliseconds: int) -> str:
    return budget_for(milliseconds)


@pytest.mark.asyncio
async def test_a_real_wait_is_measured_and_is_not_called_gpt(tmp_path: Path) -> None:
    book = reference()
    provider = _Provider(book)

    async def complete(request: object):
        if request.schema_name == "swing_assessment_v2":  # type: ignore[attr-defined]
            await asyncio.sleep(0.05)
        return await _Provider.complete(provider, request)

    provider.complete = complete  # type: ignore[method-assign]
    events: list[object] = []

    async def collect(event: object) -> None:
        events.append(event)

    coach = ReferenceLiveCoach(
        collect,
        provider,
        vision_calls=True,
        reference=book,
        context=IRON,
        feedback="exact",
        simulated=False,
    )
    await coach.open()
    capture = _capture(_package_dir(tmp_path, "real-wait"))
    started = time.monotonic()
    await coach.on_frames(SwingFramesReady(capture=capture))
    await coach.settle()
    elapsed_ms = (time.monotonic() - started) * 1000
    completed = [item for item in events if isinstance(item, AnalysisCompleted)]
    assert len(completed) == 1
    trace = coach.timing(completed[0].shot_id)
    assert trace is not None and trace.simulated is False
    assert trace.durations()["vision_ms"] is not None
    assert 40 <= trace.durations()["vision_ms"] <= 250
    assert elapsed_ms >= 40
    assert [item.schema_name for item in provider.requests] == ["swing_assessment_v2"]


@pytest.mark.asyncio
async def test_one_call_modes_do_not_wait_on_a_wording_hang(tmp_path: Path) -> None:
    _coach, events, provider = await _run(
        tmp_path,
        strategy="exact",
        vision_ms=500,
        wording_ms=2000,
        name="no-hang",
        hang_on_wording=True,
    )
    assert any(isinstance(item, AnalysisCompleted) for item in events)
    assert [item.schema_name for item in provider.requests] == ["swing_assessment_v2"]


@pytest.mark.asyncio
async def test_a_newer_shot_cancels_the_one_in_flight(tmp_path: Path) -> None:
    book = reference()
    stored = _stored(book)
    first_gate = asyncio.Event()
    release_first = asyncio.Event()
    calls = {"n": 0}

    class _Racing(_Provider):
        async def complete(self, request: object):
            if request.schema_name != "swing_assessment_v2":  # type: ignore[attr-defined]
                return await super().complete(request)
            calls["n"] += 1
            if calls["n"] == 1:
                first_gate.set()
                await release_first.wait()
            return await super().complete(request)

    events: list[object] = []

    async def collect(event: object) -> None:
        events.append(event)

    coach = ReferenceLiveCoach(
        collect,
        _Racing(book),
        vision_calls=True,
        reference=book,
        context=IRON,
        feedback="exact",
    )
    await coach.open()
    older = _capture(_package_dir(tmp_path, "older"))
    newer = _capture(_package_dir(tmp_path, "newer"))
    await coach.on_frames(SwingFramesReady(capture=older))
    await first_gate.wait()
    await coach.on_frames(SwingFramesReady(capture=newer))
    await coach.settle()
    completed = [item for item in events if isinstance(item, AnalysisCompleted)]
    assert [item.shot_id for item in completed] == [newer.coaching_id]
    assert completed[0].advice.speech == stored
    failed = [item for item in events if isinstance(item, AnalysisFailed)]
    assert any(item.shot_id == older.coaching_id for item in failed)
    assert coach.health().shots == 1
    assert coach.health().active_focus == "takeaway.clubhead_position"
    release_first.set()
    await coach.settle()
    assert [item.shot_id for item in events if isinstance(item, AnalysisCompleted)] == [
        newer.coaching_id
    ]


@pytest.mark.asyncio
async def test_session_progression_is_the_same_in_each_strategy(tmp_path: Path) -> None:
    book = reference()
    strategies: tuple[FeedbackStrategy, ...] = ("exact", "observed", "worded")
    for strategy in strategies:
        provider = _Provider(book)
        if strategy == "worded":
            provider = DelayedProvider(
                book, SimClock(), vision_ms=0, wording_ms=0
            )
        events: list[object] = []

        async def collect(event: object) -> None:
            events.append(event)

        coach = ReferenceLiveCoach(
            collect,
            provider,
            vision_calls=True,
            reference=book,
            context=IRON,
            feedback=strategy,
        )
        await coach.open()
        first = _capture(_package_dir(tmp_path, f"prog-{strategy}-1"))
        await coach.on_frames(SwingFramesReady(capture=first))
        await coach.settle()
        await coach.on_speech_started(
            SpeechStarted(
                utterance_id=uuid4(),
                text="line",
                voice="test",
                priority=SpeechPriority.COACHING,
                duration_seconds=1.0,
                shot_id=first.coaching_id,
            )
        )
        await coach.on_speech(
            SpeechCompleted(
                utterance_id=uuid4(),
                outcome=SpeechOutcome.SPOKEN,
                shot_id=first.coaching_id,
            )
        )
        await coach.on_frames(
            SwingFramesReady(capture=_capture(_package_dir(tmp_path, f"prog-{strategy}-2")))
        )
        await coach.settle()
        assert coach.health().shots == 2
        assert coach.health().active_focus == "takeaway.clubhead_position"
        follow = [
            item
            for item in provider.requests
            if item.schema_name == "swing_assessment_v2"
        ]
        assert len(follow) == 2
        context = json.loads(follow[1].evidence)["previous_context"]
        assert context["delivery_status"] == "delivered"
        assert context["active_focus"] == "takeaway.clubhead_position"
        events.clear()


@pytest.mark.asyncio
async def test_live_stage_detection_without_a_model_does_not_invent_stages(
    tmp_path: Path,
) -> None:
    """The pose file is absent here. Empty labels are not a verified swing."""

    model = Path("research/direct_visual_coach/models/pose_landmarker_full.task")
    assert model.is_file() is False
    book = reference()
    provider = _Provider(book, score_unstaged=True)
    events: list[object] = []

    async def collect(event: object) -> None:
        events.append(event)

    coach = ReferenceLiveCoach(
        collect,
        provider,
        vision_calls=True,
        reference=book,
        context=IRON,
        feedback="exact",
    )
    await coach.open()
    bare = tmp_path / "bare-stages"
    bare.mkdir()
    (bare / "frame.jpg").write_bytes(
        (SHOT_DIR / "diagnostic/swing_behind/frame_023.jpg").read_bytes()
    )
    capture = _capture(bare, relative="frame.jpg")
    await coach.on_frames(SwingFramesReady(capture=capture))
    await coach.settle()
    trace = coach.timing(capture.coaching_id)
    assert trace is not None
    assert "stage_started" not in trace.marks
    assert provider.requests == []
    assert events[-1].reason is AdviceFailure.INVALID_REPLY
    assert not any(isinstance(item, AnalysisCompleted) for item in events)


def test_budget_bands_are_targets() -> None:
    assert budget_for(1999) == "excellent"
    assert budget_for(2000) == "good"
    assert budget_for(4000) == "good"
    assert budget_for(4001) == "needs_optimization"
    assert budget_for(6000) == "needs_optimization"
    assert budget_for(6001) == "too_slow"
    assert budget_for(None) == "unmeasured"


def test_feedback_modules_do_not_import_legacy_coaching() -> None:
    for name in (
        "src/caddiej/swing_reference/feedback.py",
        "src/caddiej/swing_reference/live.py",
        "src/caddiej/swing_reference/timing.py",
    ):
        text = Path(name).read_text(encoding="utf-8")
        assert "caddiej.coaching.templates" not in text
        assert "caddiej.coaching.cues" not in text
        assert "rules_synthetic" not in text
