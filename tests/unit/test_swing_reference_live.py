"""A captured shot is coached only by Ideal Swing, and only when vision calls are on."""

from __future__ import annotations

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
from caddiej.domain.coaching import AdviceFailure, CoachingFocus
from caddiej.domain.events import (
    AnalysisCompleted,
    ClubSelected,
    HandednessChanged,
    SpeechCompleted,
    SwingFramesReady,
)
from caddiej.domain.media import SwingCameraCapture, SwingCapture, SwingFrame
from caddiej.domain.shot import Club, ClubKind, Handedness
from caddiej.domain.speech import SpeechOutcome
from caddiej.swing_reference.feedback import FeedbackStrategy
from caddiej.swing_reference.live import ReferenceLiveCoach
from caddiej.swing_reference.model import SwingReference

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
        context=IRON,
        feedback=feedback,
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
    assert coach._context.club_type == "iron"
    assert coach._context.handedness == "right"
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
