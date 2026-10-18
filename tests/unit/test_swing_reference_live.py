"""A captured shot is coached only by Ideal Swing, and only when vision calls are on."""

from __future__ import annotations

import asyncio
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from tests.unit.reference_support import (
    IRON,
    PACKAGE,
    SHOT_DIR,
    assessment,
    deviation,
    reference,
)

from caddiej.coaching.provider import ProviderError, ProviderReply
from caddiej.diagnostics.journal import DiagnosticJournal
from caddiej.diagnostics.store import shot_dir
from caddiej.domain.coaching import Advice, AdviceFailure, AdviceSource, CoachingFocus
from caddiej.domain.events import (
    AnalysisCompleted,
    AnalysisFailed,
    ClubSelected,
    HandednessChanged,
    ShotCompleted,
    ShotDetected,
    SpeechCompleted,
    SwingFramesReady,
)
from caddiej.domain.media import SwingCameraCapture, SwingCapture, SwingFrame
from caddiej.domain.shot import BallLaunch, Club, ClubKind, Handedness, Shot, ShotSource
from caddiej.domain.speech import SpeechOutcome
from caddiej.speech.fakes import FakeSynthesizer, RecordingSink
from caddiej.speech.service import SpeechService
from caddiej.swing_reference.feedback import FeedbackStrategy
from caddiej.swing_reference.live import ReferenceLiveCoach
from caddiej.swing_reference.model import SwingReference
from caddiej.swing_reference.request import PlayerContext

_CHECKPOINT = "takeaway.clubhead_position"
_RULE = "takeaway.clubhead_position.keep_head_in_front"
_FRAME = "diagnostic/swing_behind/frame_023.jpg"
_OLD = (
    "Keep the posture you started with.",
    "Keep working on the hand path.",
    "Path more in",
    "Stay in posture",
    "EARLY_EXTENSION",
)


class _Provider:
    def __init__(
        self,
        book: SwingReference,
        *,
        fail: Exception | None = None,
        score_unstaged: bool = False,
    ) -> None:
        self.book = book
        self.fail = fail
        self.score_unstaged = score_unstaged
        self.requests: list[Any] = []
        self.name = "fake"
        self.model = "fake-vision"
        self.is_configured = True

    async def complete(self, request: Any) -> ProviderReply:
        self.requests.append(request)
        if self.fail is not None:
            raise self.fail
        if request.schema_name == "swing_feedback_v1":
            return ProviderReply(
                payload={
                    "spoken_feedback": "In the backswing, keep the clubhead over your hands.",
                    "short_feedback": "Clubhead over hands",
                    "referenced_rule_id": _RULE,
                    "referenced_checkpoint_id": _CHECKPOINT,
                },
                model=self.model,
                latency_seconds=0.0,
            )
        evidence = json.loads(request.evidence)
        if self.score_unstaged:
            frame = next(item for item in evidence["frames"] if item.get("camera_id") == "swing_behind")
        else:
            frame = next(
                item
                for item in evidence["frames"]
                if item.get("camera_id") == "swing_behind" and item.get("stage") == "BACKSWING"
            )
        payload = assessment(
            self.book,
            evidence["shot_id"],
            {_CHECKPOINT: deviation(_CHECKPOINT, "inside", [frame])},
            ranking="unique_primary",
            primary=_CHECKPOINT,
            note="One supported deviation.",
        )
        return ProviderReply(payload=payload, model=self.model, latency_seconds=0.0)

    async def aclose(self) -> None:
        return None


class _HoldProvider(_Provider):
    """A vision reply that stays in flight until the test releases it."""

    def __init__(self, book: SwingReference) -> None:
        super().__init__(book)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request: Any) -> ProviderReply:
        self.started.set()
        await self.release.wait()
        return await super().complete(request)


def _stored(book: SwingReference) -> str:
    rule = next(item for item in book.checkpoint(_CHECKPOINT).rules if item["id"] == _RULE)
    return str(rule["correction"])


def _package_dir(tmp: Path, shot_id: str) -> Path:
    directory = tmp / shot_id
    target = directory / _FRAME
    target.parent.mkdir(parents=True)
    shutil.copy(SHOT_DIR / _FRAME, target)
    payload = json.loads(PACKAGE.read_text(encoding="utf-8"))
    payload["shot_id"] = shot_id
    payload["selected_frames"] = [
        frame
        for frame in payload["selected_frames"]
        if frame["reference"]["relative_file"] == _FRAME and frame.get("stage") == "BACKSWING"
    ]
    (directory / "evidence_package.json").write_text(json.dumps(payload), encoding="utf-8")
    return directory


def _capture(
    directory: Path,
    *,
    camera: str = "swing_behind",
    relative: str = _FRAME,
) -> SwingCapture:
    shot = uuid4()
    now = datetime.now(UTC)
    frame = SwingFrame(
        camera_id=camera,
        index=1,
        offset_seconds=1.0,
        path=Path(relative),
    )
    return SwingCapture(
        capture_id=shot,
        trigger_id="1",
        directory=directory,
        started_at=now,
        detected_at=now,
        cameras=(
            SwingCameraCapture(
                camera_id=camera,
                source="panel",
                ok=True,
                frames=(frame,),
                captured=1,
                failed=0,
            ),
        ),
        strike_id=shot,
    )


async def _coach(
    book: SwingReference,
    provider: _Provider | None,
    *,
    vision: bool = True,
    publish: Any | None = None,
    feedback: FeedbackStrategy = "observed",
    evidence_budget: int = 24,
    evidence_allocation: str = "dtl_primary",
    context: PlayerContext | None = None,
    diagnostics: Any = None,
) -> tuple[ReferenceLiveCoach, list[Any]]:
    events: list[Any] = []

    async def collect(event: Any) -> None:
        if publish is not None:
            await publish(event)
        events.append(event)

    coach = ReferenceLiveCoach(
        collect,
        provider,
        vision_calls=vision,
        image_detail="high",
        reference=book,
        context=IRON if context is None else context,
        feedback=feedback,
        diagnostics=diagnostics,
        evidence_budget=evidence_budget,
        evidence_allocation=evidence_allocation,
    )
    await coach.open()
    return coach, events


@pytest.mark.asyncio
async def test_live_shot_uses_the_stored_correction_and_keeps_the_focus(tmp_path: Path) -> None:
    book = reference()
    stored = _stored(book)
    provider = _Provider(book)
    coach, events = await _coach(book, provider)
    first = _capture(_package_dir(tmp_path, "live-1"))
    await coach.on_frames(SwingFramesReady(capture=first))
    await coach.settle()
    completed = [item for item in events if isinstance(item, AnalysisCompleted)]
    assert len(completed) == 1
    advice = completed[0].advice
    assert advice.speech.endswith(stored)
    assert advice.display == advice.speech
    assert advice.speech.startswith("Visible inside at takeaway.clubhead_position.")
    assert stored in advice.display
    assert [item.schema_name for item in provider.requests] == ["swing_assessment_v2"]
    assert advice.focus is CoachingFocus.NONE
    assert all(line not in advice.speech for line in _OLD)
    request = next(item for item in provider.requests if item.schema_name == "swing_assessment_v2")
    assert request.prefix.startswith("Swing Reference Model 2.9-timing-first")
    assert "setup.grip" in request.prefix
    assert request.images[0].detail == "high"
    assert request.images[0].data == (SHOT_DIR / _FRAME).read_bytes()
    evidence = json.loads(request.evidence)
    assert evidence["reference"]["placement"] == "stable_prefix"
    assert "setup.grip" not in request.evidence
    assert "base64" not in request.evidence
    assert coach.health().active_focus == _CHECKPOINT

    await coach.on_speech(
        SpeechCompleted(
            utterance_id=uuid4(),
            outcome=SpeechOutcome.SPOKEN,
            shot_id=first.coaching_id,
        )
    )
    await coach.on_frames(SwingFramesReady(capture=_capture(_package_dir(tmp_path, "live-2"))))
    await coach.settle()
    assessments = [item for item in provider.requests if item.schema_name == "swing_assessment_v2"]
    assert len(assessments) == 2
    follow = json.loads(assessments[1].evidence)["previous_context"]
    assert follow["active_focus"] == _CHECKPOINT
    assert follow["previous_cue"] == advice.speech
    assert follow["previous_cue"].endswith(stored)
    assert follow["delivery_status"] == "delivered"
    assert coach.health().active_focus == _CHECKPOINT
    assert coach.health().shots == 2


@pytest.mark.asyncio
async def test_unconfirmed_speech_does_not_claim_the_line_was_heard(tmp_path: Path) -> None:
    book = reference()
    provider = _Provider(book)
    coach, _events = await _coach(book, provider)
    await coach.on_frames(SwingFramesReady(capture=_capture(_package_dir(tmp_path, "heard-1"))))
    await coach.settle()
    await coach.on_speech(
        SpeechCompleted(
            utterance_id=uuid4(),
            outcome=SpeechOutcome.INTERRUPTED,
            shot_id=uuid4(),
        )
    )
    await coach.on_frames(SwingFramesReady(capture=_capture(_package_dir(tmp_path, "heard-2"))))
    await coach.settle()
    follow = json.loads(
        [item for item in provider.requests if item.schema_name == "swing_assessment_v2"][1].evidence
    )["previous_context"]
    assert follow["previous_cue"] is None
    assert follow["delivery_status"] == "not_delivered"
    assert follow["active_focus"] == _CHECKPOINT


@pytest.mark.asyncio
async def test_vision_calls_off_sends_nothing(tmp_path: Path) -> None:
    book = reference()
    provider = _Provider(book)
    coach, events = await _coach(book, provider, vision=False)
    await coach.on_frames(SwingFramesReady(capture=_capture(_package_dir(tmp_path, "quiet"))))
    await coach.settle()
    assert provider.requests == []
    assert events == []
    assert coach.health().shots == 0


@pytest.mark.asyncio
async def test_failed_and_invalid_replies_do_not_coach(tmp_path: Path) -> None:
    book = reference()
    failing = _Provider(book, fail=ProviderError("down"))
    coach, events = await _coach(book, failing)
    await coach.on_frames(SwingFramesReady(capture=_capture(_package_dir(tmp_path, "fail"))))
    await coach.settle()
    assert not any(isinstance(item, AnalysisCompleted) for item in events)
    assert events[-1].reason is AdviceFailure.PROVIDER_ERROR
    assert coach.health().active_focus is None

    class _Invalid(_Provider):
        async def complete(self, request: Any) -> ProviderReply:
            self.requests.append(request)
            return ProviderReply(payload={"coaching": "invented"}, model="fake", latency_seconds=0)

    invalid = _Invalid(book)
    quiet, rejected = await _coach(book, invalid)
    await quiet.on_frames(SwingFramesReady(capture=_capture(_package_dir(tmp_path, "bad"))))
    await quiet.settle()
    assert not any(isinstance(item, AnalysisCompleted) for item in rejected)
    assert quiet.health().active_focus is None
    assert quiet.health().shots == 0


@pytest.mark.asyncio
async def test_missing_images_and_a_stage_less_capture_stay_silent(tmp_path: Path) -> None:
    book = reference()
    provider = _Provider(book, score_unstaged=True)
    coach, events = await _coach(book, provider)
    empty = tmp_path / "empty"
    empty.mkdir()
    await coach.on_frames(SwingFramesReady(capture=_capture(empty, camera="impact")))
    await coach.settle()
    assert provider.requests == []
    assert events[-1].reason is AdviceFailure.INVALID_REPLY

    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "frame.jpg").write_bytes((SHOT_DIR / _FRAME).read_bytes())
    await coach.on_frames(
        SwingFramesReady(capture=_capture(bare, relative="frame.jpg"))
    )
    await coach.settle()
    assert provider.requests == []
    assert events[-1].reason is AdviceFailure.INVALID_REPLY
    assert not any(isinstance(item, AnalysisCompleted) for item in events)
    assert coach.health().shots == 0


@pytest.mark.asyncio
async def test_duplicate_and_stale_shots_do_not_move_the_session(tmp_path: Path) -> None:
    book = reference()
    provider = _Provider(book)
    coach, events = await _coach(book, provider)
    directory = _package_dir(tmp_path, "once")
    capture = _capture(directory)
    await coach.on_frames(SwingFramesReady(capture=capture))
    await coach.settle()
    await coach.on_frames(SwingFramesReady(capture=capture))
    await coach.settle()
    assert len([item for item in provider.requests if item.schema_name == "swing_assessment_v2"]) == 1
    assert coach.health().shots == 1

    class _Stale(_Provider):
        target: ReferenceLiveCoach | None = None

        async def complete(self, request: Any) -> ProviderReply:
            assert self.target is not None
            self.target._latest = uuid4()
            return await super().complete(request)

    stale_provider = _Stale(book)
    stale, stale_events = await _coach(book, stale_provider)
    stale_provider.target = stale
    await stale.on_frames(SwingFramesReady(capture=_capture(_package_dir(tmp_path, "late"))))
    await stale.settle()
    assert stale.health().shots == 0
    assert stale.health().active_focus is None
    assert stale_events[-1].reason is AdviceFailure.SUPERSEDED
    assert not any(isinstance(item, AnalysisCompleted) for item in stale_events)


@pytest.mark.asyncio
async def test_a_broken_floor_does_not_stop_the_session(tmp_path: Path) -> None:
    book = reference()

    async def fail_floor(event: Any) -> None:
        if isinstance(event, AnalysisCompleted):
            raise RuntimeError("floor down")

    coach, _events = await _coach(book, _Provider(book), publish=fail_floor)
    await coach.on_frames(SwingFramesReady(capture=_capture(_package_dir(tmp_path, "floor"))))
    await coach.settle()
    assert coach.health().active_focus == _CHECKPOINT


async def test_player_context_uses_a_known_club_and_leaves_the_rest_unknown() -> None:
    async def publish(event: object) -> None:
        return None

    coach = ReferenceLiveCoach(publish, None, vision_calls=False)
    assert coach._context.club_type == "unknown"
    assert coach._context.shot_type == "unknown"
    assert coach._context.evidence_context == "player_swing"
    await coach.on_club(ClubSelected(club=Club(kind=ClubKind.WOOD)))
    assert coach._context.club_type == "unknown"
    await coach.on_club(ClubSelected(club=Club(kind=ClubKind.IRON)))
    await coach.on_handedness(HandednessChanged(handedness=Handedness.RIGHT))
    await coach.on_shot(
        ShotCompleted(
            shot=Shot(
                shot_id=uuid4(),
                source=ShotSource(
                    kind="synthetic",
                    device_session_id=uuid4(),
                    observed_at=datetime.now(UTC),
                    observed_monotonic=0.0,
                ),
                ball=BallLaunch(speed_mph=140.0, carry_yards=155.0),
                club=Club(kind=ClubKind.IRON),
                handedness=Handedness.RIGHT,
            ),
            trigger_id="1",
        )
    )
    assert coach._context.club_type == "iron"
    assert coach._context.handedness == "right"
    assert coach._context.distance_yards == 155.0
    assert coach._context.shot_type == "unknown"


def test_the_application_starts_only_the_reference_coach() -> None:
    app = Path("src/caddiej/app.py").read_text(encoding="utf-8")
    live = Path("src/caddiej/swing_reference/live.py").read_text(encoding="utf-8")
    config = Path("src/caddiej/config.py").read_text(encoding="utf-8")
    assert "AnalysisService" not in app
    assert "COACHING_MODE" not in config.replace("COACHING_MODEL", "")
    assert "def live_legacy_coaching" not in config
    assert "def visual_catalog_enabled" not in config
    assert "caddiej.coaching.templates" not in live
    assert "caddiej.coaching.cues" not in live
    assert "rules_synthetic" not in live


@pytest.mark.asyncio
async def test_an_explicit_stock_shot_is_not_replaced_by_club_or_carry() -> None:
    async def publish(event: object) -> None:
        return None

    coach = ReferenceLiveCoach(
        publish,
        None,
        vision_calls=False,
        context=PlayerContext(shot_type="stock", evidence_context="player_swing"),
    )
    await coach.on_club(ClubSelected(club=Club(kind=ClubKind.IRON)))
    await coach.on_handedness(HandednessChanged(handedness=Handedness.RIGHT))
    await coach.on_shot(
        ShotCompleted(
            shot=Shot(
                shot_id=uuid4(),
                source=ShotSource(
                    kind="synthetic",
                    device_session_id=uuid4(),
                    observed_at=datetime.now(UTC),
                    observed_monotonic=0.0,
                ),
                ball=BallLaunch(speed_mph=140.0, carry_yards=155.0),
                club=Club(kind=ClubKind.WEDGE),
                handedness=Handedness.RIGHT,
            )
        )
    )

    assert coach._context.shot_type == "stock"
    assert coach._context.club_type == "wedge"
    assert coach._context.handedness == "right"
    assert coach._context.distance_yards == 155.0


def _measured_shot(shot_id: Any) -> Shot:
    return Shot(
        shot_id=shot_id,
        source=ShotSource(
            kind="synthetic",
            device_session_id=uuid4(),
            observed_at=datetime.now(UTC),
            observed_monotonic=0.0,
        ),
        ball=BallLaunch(speed_mph=140.0),
    )


def _late_advice(shot_id: Any) -> AnalysisCompleted:
    return AnalysisCompleted(
        shot_id=shot_id,
        advice=Advice(
            shot_id=shot_id,
            focus=CoachingFocus.NONE,
            display="Keep the clubhead in front.",
            speech="Keep the clubhead in front of your hands.",
            severity=0.6,
            created_at=datetime.now(UTC),
            source=AdviceSource.TEMPLATE,
        ),
    )


@pytest.mark.asyncio
async def test_a_new_strike_stops_a_reply_still_in_flight(tmp_path: Path) -> None:
    book = reference()
    provider = _HoldProvider(book)
    engine = FakeSynthesizer()
    speech = SpeechService(
        publish=_ignore,
        synthesizer=engine,
        sink=RecordingSink(),
    )
    await speech.open()
    try:
        coach, events = await _coach(book, provider)

        async def publish(event: Any) -> None:
            events.append(event)
            if isinstance(event, AnalysisCompleted):
                await speech.on_analysis_completed(event)

        coach._publish = publish
        capture = _capture(_package_dir(tmp_path, "in-flight"))
        await coach.on_frames(SwingFramesReady(capture=capture))
        await provider.started.wait()
        later = uuid4()
        detected = ShotDetected(trigger_id="2", event_id=later)
        await speech.on_shot_detected(detected)
        await coach.on_shot_detected(detected)
        await speech.on_analysis_completed(_late_advice(capture.coaching_id))
        provider.release.set()
        await coach.settle()
    finally:
        await speech.aclose()

    assert not any(isinstance(item, AnalysisCompleted) for item in events)
    assert events[-1].reason is AdviceFailure.SUPERSEDED
    assert isinstance(events[-1], AnalysisFailed)
    assert coach.health().shots == 0
    assert coach.health().active_focus is None
    assert engine.calls == []


@pytest.mark.asyncio
async def test_a_repeated_trigger_does_not_cancel_the_shot_in_flight(
    tmp_path: Path,
) -> None:
    book = reference()
    provider = _HoldProvider(book)
    coach, events = await _coach(book, provider)
    capture = _capture(_package_dir(tmp_path, "same-trigger"))
    await coach.on_frames(SwingFramesReady(capture=capture))
    await provider.started.wait()
    await coach.on_shot_detected(
        ShotDetected(trigger_id=capture.trigger_id, event_id=uuid4())
    )
    provider.release.set()
    await coach.settle()

    assert [type(item) for item in events if isinstance(item, AnalysisCompleted)]
    assert coach.health().shots == 1
    assert coach.health().active_focus == _CHECKPOINT


@pytest.mark.asyncio
async def test_a_late_speech_completion_does_not_deliver_a_newer_note(
    tmp_path: Path,
) -> None:
    book = reference()
    coach, _events = await _coach(book, _Provider(book))
    first = _capture(_package_dir(tmp_path, "spoken-1"))
    second = _capture(_package_dir(tmp_path, "spoken-2"))
    await coach.on_frames(SwingFramesReady(capture=first))
    await coach.settle()
    await coach.on_frames(SwingFramesReady(capture=second))
    await coach.settle()

    assert coach._previous is not None
    assert coach._previous.delivery_status == "not_delivered"
    assert coach._previous_shot == second.coaching_id
    await coach.on_speech(
        SpeechCompleted(
            utterance_id=uuid4(),
            outcome=SpeechOutcome.SPOKEN,
            shot_id=first.coaching_id,
        )
    )

    assert coach._previous.delivery_status == "not_delivered"
    assert coach._previous.previous_cue is None
    assert coach._previous_shot == second.coaching_id
    await coach.on_speech(
        SpeechCompleted(
            utterance_id=uuid4(),
            outcome=SpeechOutcome.SPOKEN,
            shot_id=second.coaching_id,
        )
    )
    assert coach._previous.delivery_status == "delivered"
    assert coach._previous_shot == second.coaching_id


@pytest.mark.asyncio
async def test_a_late_reply_does_not_replace_the_newer_shot_zip(tmp_path: Path) -> None:
    book = reference()
    provider = _HoldProvider(book)
    journal = DiagnosticJournal(
        tmp_path / "diagnostics",
        session_id="session-1",
        include_images=False,
    )
    coach, _events = await _coach(book, provider, diagnostics=journal)
    capture = _capture(_package_dir(tmp_path, "older-shot"))
    journal.note_strike(
        strike_id=str(capture.coaching_id),
        trigger_id="1",
        monotonic_seconds=1.0,
        occurred_at=datetime.now(UTC),
    )
    await coach.on_frames(SwingFramesReady(capture=capture))
    await provider.started.wait()
    later = uuid4()
    journal.note_strike(
        strike_id=str(later),
        trigger_id="2",
        monotonic_seconds=2.0,
        occurred_at=datetime.now(UTC),
    )
    journal.note_failure(str(later), "failed", "capture produced no frames")
    journal.flush()
    assert _zip_manifest(tmp_path / "diagnostics")["shot_id"] == str(later)

    await coach.on_shot_detected(ShotDetected(trigger_id="2", event_id=later))
    provider.release.set()
    await coach.settle()
    journal.flush()

    assert _zip_manifest(tmp_path / "diagnostics")["shot_id"] == str(later)
    report = json.loads(
        (shot_dir(tmp_path / "diagnostics", "session-1", str(capture.coaching_id)) / "report.json").read_text(
            encoding="utf-8"
        )
    )
    assert report["status"] == "superseded"
    journal.close()


async def _ignore(_event: object) -> None:
    return None


def _zip_manifest(root: Path) -> dict[str, object]:
    import zipfile

    with zipfile.ZipFile(root / "latest_shot.zip") as archive:
        loaded = json.loads(archive.read("manifest.json"))
    assert isinstance(loaded, dict)
    return loaded
