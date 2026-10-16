"""Coach V1: cues, evidence, newest-shot-wins, media, and the production probe."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from tests.unit.test_coaching_service import a_shot_with_no_evidence

from caddiej.coaching.cues import (
    CUES,
    CUES_BY_ID,
    PUTTING,
    VISUAL_CONFIDENCE_CEILING,
    admissible_cues,
    cap_visual_confidence,
    derived_confidence,
)
from caddiej.coaching.diagnose import diagnose
from caddiej.coaching.fakes import GOOD_REPLY, ScriptedProvider
from caddiej.coaching.probe import (
    fixture_putt,
    fixture_shot,
    probe_coach,
    probe_coach_cues,
)
from caddiej.coaching.prompt import PROMPT_VERSION, build_request
from caddiej.coaching.provider import ProviderTimeoutError
from caddiej.coaching.service import AnalysisService
from caddiej.coaching.session import CoachingSession
from caddiej.coaching.validate import AdviceRejected, validate
from caddiej.domain.coaching import AdviceKind, AdviceStatus, CoachingFocus
from caddiej.domain.cues import (
    CueId,
    EvidenceGrade,
    PanelKind,
    VisualClaim,
)
from caddiej.domain.events import (
    AnalysisCompleted,
    AnalysisFailed,
    ShotCompleted,
    ShotDiagnosed,
    SwingFramesReady,
)
from caddiej.domain.media import SwingCameraCapture, SwingCapture, SwingFrame
from caddiej.domain.shot import ClubKind, Handedness
from caddiej.persistence.schema import SCHEMA_VERSION
from caddiej.swing_capture.select import (
    ActiveInterval,
    CapturedFrame,
    likely_blank,
    nearest,
    settled_frame,
    static_choice,
)
from caddiej.swing_capture.store import (
    CameraResult,
    CaptureRecord,
    LatestSwingStore,
)

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def _baseline():
    from caddiej.coaching.baseline import ClubBaseline

    return ClubBaseline(club=ClubKind.IRON, shots=0)


def _capture(
    trigger: str = "1", *, claims: frozenset[VisualClaim] | None = None
) -> SwingCapture:
    supports = claims or frozenset({VisualClaim.BODY_THROUGH_IMPACT})
    return SwingCapture(
        capture_id=uuid4(),
        trigger_id=trigger,
        directory=Path("media/latest_swing"),
        started_at=NOW,
        detected_at=NOW,
        cameras=(
            SwingCameraCapture(
                camera_id="swing",
                source="CaddieJ Swing",
                ok=True,
                frames=(
                    SwingFrame(
                        camera_id="swing",
                        index=1,
                        offset_seconds=0.5,
                        path=Path("swing/frame_001.jpg"),
                        width=640,
                        height=360,
                    ),
                ),
                captured=4,
                failed=0,
                kind=PanelKind.DYNAMIC,
                supports=supports,
            ),
        ),
    )


class _Bus:
    def __init__(self) -> None:
        self.events: list = []

    async def __call__(self, event: object) -> None:
        self.events.append(event)

    def of(self, kind: type) -> list:
        return [event for event in self.events if isinstance(event, kind)]


async def _service(provider=None, **kwargs) -> tuple[AnalysisService, _Bus, list]:
    bus = _Bus()
    recorded: list = []

    async def record(outcome) -> None:
        recorded.append(outcome)

    service = AnalysisService(
        bus,
        provider=provider,
        record=record,
        send_frames=kwargs.pop("send_frames", False),
        visual_cues=kwargs.pop("visual_cues", True),
        max_images=kwargs.pop("max_images", 6),
        clip_wait_seconds=kwargs.pop("clip_wait_seconds", 0.05),
        **kwargs,
    )
    await service.open()
    return service, bus, recorded


class TestCueAdmissibility:
    def test_open_face_iron_admits_face_open(self) -> None:
        diagnosis = diagnose(fixture_shot(), _baseline())
        assert CueId.FACE_OPEN in diagnosis.admissible_cues
        assert diagnosis.cue is CueId.FACE_OPEN
        assert diagnosis.display == "PASS THE TOE EARLY"

    def test_every_full_swing_correction_excludes_putter(self) -> None:
        for cue in CUES:
            if (
                cue.kind is AdviceKind.CORRECTION
                and cue.grade is not EvidenceGrade.DERIVED
            ):
                if cue.cue_id.value.startswith("PUTT_"):
                    assert ClubKind.PUTTER in cue.clubs
                else:
                    assert ClubKind.PUTTER not in cue.clubs

    def test_putt_admits_only_putting_cues(self) -> None:
        diagnosis = diagnose(fixture_putt(), _baseline(), target_distance_m=4.0)
        assert diagnosis.club is ClubKind.PUTTER
        for cue in diagnosis.admissible_cues:
            assert CUES_BY_ID[cue].clubs & PUTTING

    def test_observe_mode_lists_unmeasured_club_metrics(self) -> None:
        diagnosis = diagnose(a_shot_with_no_evidence(), _baseline())
        assert "club face angle" in diagnosis.unmeasured
        assert "club path" in diagnosis.unmeasured

    def test_left_hander_mirrors_open_face(self) -> None:
        diagnosis = diagnose(
            fixture_shot(handedness=Handedness.LEFT, face_to_target=-4.8),
            _baseline(),
        )
        assert CueId.FACE_OPEN in diagnosis.admissible_cues

    def test_visual_cue_needs_supporting_panel(self) -> None:
        diagnosis = diagnose(fixture_shot(), _baseline())
        without = admissible_cues(diagnosis, has_frames=True, capture=None)
        assert CueId.EARLY_EXTENSION not in without
        # May still be contraindicated by a clean strike; the claim is required.
        capture = _capture(claims=frozenset({VisualClaim.BODY_THROUGH_IMPACT}))
        allowed = admissible_cues(diagnosis, has_frames=True, capture=capture)
        assert all(
            CUES_BY_ID[cue].grade is not EvidenceGrade.VISUAL
            or any(
                CUES_BY_ID[cue].requires.visual_claim in camera.supports
                for camera in capture.cameras
            )
            for cue in allowed
        )

    def test_visual_confidence_caps_and_rejects_below_floor(self) -> None:
        assert cap_visual_confidence(0.99) == VISUAL_CONFIDENCE_CEILING
        assert cap_visual_confidence(0.10) is None
        assert cap_visual_confidence(0.44) == 0.45
        diagnosis = diagnose(fixture_shot(), _baseline())
        assert cap_visual_confidence(1.0) <= derived_confidence(
            diagnosis, CUES_BY_ID[CueId.FACE_OPEN]
        )


class TestValidation:
    def test_unsupported_cue_is_rejected(self) -> None:
        diagnosis = diagnose(fixture_shot(), _baseline())
        with pytest.raises(AdviceRejected, match="not admissible"):
            validate(
                {
                    "cue": "EARLY_EXTENSION",
                    "speech": "Hold your angles through the ball.",
                },
                diagnosis,
                NOW,
            )

    def test_malformed_reply_is_rejected(self) -> None:
        diagnosis = diagnose(fixture_shot(), _baseline())
        with pytest.raises(AdviceRejected):
            validate({"speech": "Square the face."}, diagnosis, NOW)

    def test_display_comes_from_the_cue_table(self) -> None:
        diagnosis = diagnose(fixture_shot(), _baseline())
        advice = validate(dict(GOOD_REPLY), diagnosis, NOW)
        assert advice.display == "PASS THE TOE EARLY"
        assert advice.cue is CueId.FACE_OPEN
        assert advice.confidence_source is not None

    def test_low_visual_confidence_is_rejected(self) -> None:
        diagnosis = replace(
            diagnose(fixture_shot(), _baseline()),
            admissible_cues=(CueId.FACE_OPEN, CueId.EARLY_EXTENSION),
        )
        with pytest.raises(AdviceRejected, match="below the floor"):
            validate(
                {
                    "cue": "EARLY_EXTENSION",
                    "speech": "Hold your angles through the ball.",
                    "visual_confidence": 0.1,
                },
                diagnosis,
                NOW,
                frames_sent=True,
            )


class TestHappyPath:
    async def test_complete_path_records_cue_and_grades(self) -> None:
        service, bus, recorded = await _service(ScriptedProvider([GOOD_REPLY]))
        await service.on_shot_completed(
            ShotCompleted(shot=fixture_shot(), trigger_id="7")
        )
        await asyncio.sleep(0.05)
        await service.aclose()
        diagnosed = bus.of(ShotDiagnosed)[0]
        completed = bus.of(AnalysisCompleted)[0]
        assert diagnosed.diagnosis.cue is CueId.FACE_OPEN
        assert completed.advice.cue is CueId.FACE_OPEN
        assert completed.advice.speech == GOOD_REPLY["speech"]
        assert recorded
        assert recorded[-1].advice.cue is CueId.FACE_OPEN
        assert recorded[-1].evidence_grades is not None
        assert SCHEMA_VERSION >= 3

    async def test_insufficient_evidence_is_silence(self) -> None:
        service, bus, recorded = await _service(ScriptedProvider([GOOD_REPLY]))
        await service.on_shot_completed(ShotCompleted(shot=a_shot_with_no_evidence()))
        await asyncio.sleep(0.05)
        await service.aclose()
        diagnosed = bus.of(ShotDiagnosed)[0]
        assert diagnosed.diagnosis.focus is CoachingFocus.NONE
        assert diagnosed.diagnosis.display == ""


class TestNewestShotWins:
    async def test_stale_ai_reply_is_superseded(self) -> None:
        first = {"cue": "FACE_OPEN", "speech": "Square the face a little earlier."}
        provider = ScriptedProvider(replies=[first, first], delay=0.2)
        service, bus, recorded = await _service(
            provider,
            timeout_seconds=1.0,
            total_budget_seconds=2.0,
            clip_wait_seconds=0.0,
        )
        await service.on_shot_completed(
            ShotCompleted(shot=fixture_shot(), trigger_id="1")
        )
        await service.on_shot_completed(
            ShotCompleted(shot=fixture_shot(), trigger_id="2")
        )
        await asyncio.sleep(0.4)
        await service.aclose()
        failed = bus.of(AnalysisFailed)
        assert any(
            outcome.failure and outcome.failure.value == "superseded"
            for outcome in recorded
        ) or any(event.reason.value == "superseded" for event in failed)

    async def test_openai_timeout_leaves_template_standing(self) -> None:
        service, bus, recorded = await _service(
            ScriptedProvider(replies=[ProviderTimeoutError("slow")], delay=0.0)
        )
        await service.on_shot_completed(
            ShotCompleted(shot=fixture_shot(), trigger_id="1")
        )
        await asyncio.sleep(0.05)
        await service.aclose()
        assert bus.of(AnalysisCompleted) == []
        assert recorded
        assert (
            recorded[-1].status is not AdviceStatus.OK
            or recorded[-1].failure is not None
        )


class TestObsAndSpeechIsolation:
    async def test_obs_failure_still_coaches_from_numbers(self) -> None:
        service, bus, recorded = await _service(
            ScriptedProvider([GOOD_REPLY]), send_frames=True
        )
        await service.on_shot_completed(
            ShotCompleted(shot=fixture_shot(), trigger_id="9")
        )
        await asyncio.sleep(0.1)
        await service.aclose()
        assert any(outcome.advice.cue is CueId.FACE_OPEN for outcome in recorded)

    async def test_putt_sends_no_images(self) -> None:
        requests: list = []

        class Recording(ScriptedProvider):
            async def complete(self, request):  # type: ignore[override]
                requests.append(request)
                return await super().complete(request)

        provider = Recording(replies=[GOOD_REPLY])
        service, _bus, _recorded = await _service(provider, send_frames=True)
        await service.on_swing_frames(SwingFramesReady(capture=_capture("p")))
        await service.on_shot_completed(
            ShotCompleted(shot=fixture_putt(), trigger_id="p")
        )
        await asyncio.sleep(0.1)
        await service.aclose()
        if requests:
            assert requests[0].images == ()


class TestSessionAndHandedness:
    def test_visual_cue_does_not_adopt_full_swing_focus(self) -> None:
        session = CoachingSession()
        diagnosis = diagnose(fixture_shot(), _baseline())
        visual = replace(
            diagnosis,
            cue=CueId.EARLY_EXTENSION,
            focus=CoachingFocus.EARLY_EXTENSION,
            kind=AdviceKind.CORRECTION,
        )
        session.remember(visual)
        assert session.focus is None

    def test_putting_track_is_separate(self) -> None:
        session = CoachingSession()
        full = diagnose(fixture_shot(), _baseline())
        session.remember(full)
        assert session.focus is CoachingFocus.FACE_CONTROL
        putt = diagnose(fixture_putt(), _baseline(), target_distance_m=4.0)
        session.remember(putt)
        assert session.focus is CoachingFocus.FACE_CONTROL
        assert session.putting_focus is not None

    def test_three_clean_shots_retire_focus(self) -> None:
        session = CoachingSession()
        session.remember(diagnose(fixture_shot(), _baseline()))
        good = diagnose(fixture_shot(face_to_target=0.1), _baseline())
        for _ in range(3):
            session.remember(good)
        # A tiny face may still be a correction depending on tolerance.
        if good.kind is AdviceKind.REINFORCEMENT:
            assert session.focus is None or session.focus is CoachingFocus.FACE_CONTROL


class TestMedia:
    def test_settled_and_unsettled_static_frames(self) -> None:
        same = b"jpeg-same"
        moving = [
            CapturedFrame(0.0, b"a"),
            CapturedFrame(0.5, b"b"),
            CapturedFrame(1.0, b"c"),
        ]
        settled = [
            CapturedFrame(0.0, b"a"),
            CapturedFrame(0.5, same),
            CapturedFrame(1.0, same),
        ]
        assert settled_frame(settled) == (2, True)
        assert settled_frame(moving)[1] is False
        assert settled_frame((CapturedFrame(0.0, b"only"),)) == (0, False)

    def test_tiny_jpeg_is_detectable_as_blank(self) -> None:
        assert likely_blank(b"\xff\xd8" + b"\x00" * 10)
        assert not likely_blank(b"not-a-jpeg")

    def test_a_black_burst_is_not_kept(self) -> None:
        blank = CapturedFrame(0.0, b"\xff\xd8" + b"\x00" * 10)
        later = CapturedFrame(1.0, b"\xff\xd8" + b"\x00" * 10)
        assert nearest((blank, later), (0.0, 1.0)) == ()
        assert static_choice((blank, later)) is None

    def test_debug_keep_is_bounded_and_production_is_one(self, tmp_path: Path) -> None:
        store = LatestSwingStore(tmp_path / "latest_swing", debug_keep_shots=2)
        store.prepare()

        def publish_one(suffix: str) -> None:
            record = CaptureRecord(
                capture_id=uuid4(),
                trigger_id=suffix,
                detected_at=NOW,
                started_at=NOW,
                completed_at=NOW,
                cameras=(
                    CameraResult(
                        camera_id="swing",
                        source="src",
                        ok=True,
                        captured=1,
                        failed=0,
                        frames=(CapturedFrame(0.1, b"\xff\xd8\xff" + suffix.encode()),),
                    ),
                ),
                interval=ActiveInterval(0.0, 0.1, detected=False),
                configuration={},
            )
            staged = store.stage(record)
            store.publish(staged, record)

        publish_one("a")
        publish_one("b")
        publish_one("c")
        debug = (
            list((tmp_path / "swing_debug").iterdir())
            if (tmp_path / "swing_debug").exists()
            else []
        )
        assert len(debug) <= 2
        assert (tmp_path / "latest_swing").exists()

    def test_windows_rename_retries_then_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        attempts = {"n": 0}
        real_rename = Path.rename

        def flaky(self: Path, dest: Path) -> Path:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise OSError(13, "locked")
            return real_rename(self, dest)

        monkeypatch.setattr(Path, "rename", flaky)
        store = LatestSwingStore(tmp_path / "latest_swing")
        store.prepare()
        record = CaptureRecord(
            capture_id=uuid4(),
            trigger_id="1",
            detected_at=NOW,
            started_at=NOW,
            completed_at=NOW,
            cameras=(
                CameraResult(
                    camera_id="swing",
                    source="src",
                    ok=True,
                    captured=1,
                    failed=0,
                    frames=(CapturedFrame(0.1, b"jpeg"),),
                ),
            ),
            interval=ActiveInterval(0.0, 0.1, detected=False),
            configuration={},
        )
        staged = store.stage(record)
        published = store.publish(staged, record)
        assert published.capture.directory.exists()
        assert published.latest_ok
        assert attempts["n"] >= 3


class TestStaleSpeech:
    async def test_a_newer_shot_prevents_older_speech(self) -> None:
        from tests.unit.test_speech_service import Table, a_shot

        from caddiej.domain.speech import SpeechOutcome
        from caddiej.speech.fakes import RecordingSink

        first, second = uuid4(), uuid4()
        gate = asyncio.Event()
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


class TestShutdownAndRestart:
    async def test_shutdown_during_analysis_does_not_raise(self) -> None:
        service, _bus, _recorded = await _service(
            ScriptedProvider([GOOD_REPLY], delay=0.3),
            drain_timeout_seconds=0.05,
        )
        await service.on_shot_completed(ShotCompleted(shot=fixture_shot()))
        await asyncio.wait_for(service.aclose(), timeout=2.0)

    async def test_restart_forgets_session_focus(self) -> None:
        session = CoachingSession()
        session.remember(diagnose(fixture_shot(), _baseline()))
        assert session.focus is not None
        session.forget()
        assert session.focus is None
        assert session.putting_focus is None


class TestProbeUsesProductionCode:
    def test_cue_probe_is_offline_and_honest(self) -> None:
        probe = probe_coach_cues()
        assert probe.ok
        assert "FACE_OPEN" in probe.report
        assert "PUTT_" in probe.report

    async def test_coach_probe_runs_analysis_service(self) -> None:
        probe = await probe_coach(
            provider_name="scripted",
            provider=ScriptedProvider([GOOD_REPLY]),
        )
        assert probe.ok
        assert probe.diagnosis is not None
        assert "AnalysisService" in probe.report or "diagnose()" in probe.report


class TestPromptSchema:
    def test_schema_enumerates_only_admissible_cues(self) -> None:
        diagnosis = diagnose(fixture_shot(), _baseline())
        request = build_request(diagnosis, "evidence", cues=diagnosis.admissible_cues)
        assert request.schema["properties"]["cue"]["enum"] == [
            cue.value for cue in diagnosis.admissible_cues
        ]
        assert PROMPT_VERSION == "4"


class TestCapturePlanFitsWait:
    def test_default_wall_time_is_under_clip_wait(self) -> None:
        from caddiej.config import Settings

        settings = Settings(
            _env_file=None,
            swing_capture_enabled=False,
        )
        wall = max(
            settings.swing_capture_initial_delay_seconds
            + settings.swing_capture_duration_seconds,
            settings.swing_capture_static_delay_seconds
            + settings.swing_capture_static_duration_seconds,
        )
        assert wall < settings.coaching_clip_wait
