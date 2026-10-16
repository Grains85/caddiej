"""The result concept: score, observation, action line and the player's frame.

The score is CaddieJ's own arithmetic on the readings, so it reaches the floor
on every path that computed one, including the one where the model's words
were refused and CaddieJ's template stands in. The frame is the still the coach
cited, served by capture id and frame id exactly as stored, and nothing at
all when that still is not there.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from tests.unit.test_capture_scene import a_burst
from tests.unit.test_coach_protee import DRIVER, Coach, a_capture_on_disk, a_reply
from tests.unit.test_obs_client import _Bay
from tests.unit.test_play_flow import FloorSpy, a_diagnosis, an_advice, flow

from caddiej.adapters.hud import ConnectionRegistry, create_app
from caddiej.adapters.hud.evidence import EvidenceFrames
from caddiej.adapters.hud.protocol import FloorStatePayload, evidence_url
from caddiej.coaching.fakes import ScriptedProvider
from caddiej.domain.coaching import AdviceSource, CoachingFocus
from caddiej.domain.events import (
    AnalysisCompleted,
    ClubSelected,
    CoachingStage,
    EvidenceFrame,
    ShotDetected,
    ShotDiagnosed,
    SwingFramesReady,
)
from caddiej.domain.ports import CoachingResult
from caddiej.domain.swing_score import SwingScore
from caddiej.floor import FloorEvidence, FloorService
from caddiej.floor.layout import layout_from_tees
from caddiej.floor.presenter import FloorPresenter
from caddiej.swing_capture.store import frame_files

DECLINED = a_reply(kind="none", cue=None, speech=None)


class ResultFloor(FloorSpy):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[object] = []

    def show_advice(self, text: str, severity: float, result: object = None) -> None:
        super().show_advice(text, severity, result)
        self.results.append(result)


def a_score(value: int | None) -> SwingScore:
    return SwingScore(value=value, components=(), available_weight=60)


class TestTheScoreReachesTheFloor:
    async def test_the_template_fallback_carries_the_score(self, tmp_path) -> None:
        coach = Coach(ScriptedProvider(replies=(DECLINED,)))

        await coach.run(tmp_path)

        completed = coach.bus.of(AnalysisCompleted)
        assert completed, "the cue's reviewed line is what reaches the floor"
        assert completed[-1].advice.source is AdviceSource.TEMPLATE
        score = completed[-1].advice.swing_score
        assert score is not None and score.value is not None

    async def test_the_score_does_not_depend_on_the_model_s_words(
        self, tmp_path
    ) -> None:
        coached = Coach(ScriptedProvider(replies=(a_reply(),)))
        declined = Coach(ScriptedProvider(replies=(DECLINED,)))

        await coached.run(tmp_path / "a")
        await declined.run(tmp_path / "b")

        accepted = coached.bus.of(AnalysisCompleted)[0].advice.swing_score
        fallback = declined.bus.of(AnalysisCompleted)[-1].advice.swing_score
        assert accepted is not None and fallback is not None
        assert accepted.value == fallback.value

    async def test_the_flow_hands_the_template_score_to_the_floor(self) -> None:
        floor = ResultFloor()
        subject = flow(floor)
        strike = ShotDetected(trigger_id="16", source="protee_log")
        await subject.on_shot_detected(strike)

        await subject.on_shot_diagnosed(
            ShotDiagnosed(
                diagnosis=a_diagnosis(strike.event_id), swing_score=a_score(74)
            )
        )

        assert floor.advice[-1] == ("FACE OPEN", 0.6)
        result = floor.results[-1]
        assert isinstance(result, CoachingResult) and result.swing_score == 74
        await subject.aclose()

    async def test_a_score_with_nothing_to_say_still_shows_the_score(self) -> None:
        floor = ResultFloor()
        subject = flow(floor)
        shot_id = uuid4()

        await subject.on_shot_diagnosed(
            ShotDiagnosed(
                diagnosis=a_diagnosis(
                    shot_id, focus=CoachingFocus.NONE, severity=0.0, display=""
                ),
                swing_score=a_score(88),
            )
        )

        assert floor.advice == [("", 0.0)]
        result = floor.results[-1]
        assert isinstance(result, CoachingResult) and result.swing_score == 88
        await subject.aclose()

    async def test_an_unscorable_swing_is_not_a_zero(self) -> None:
        floor = ResultFloor()
        subject = flow(floor)

        await subject.on_shot_diagnosed(
            ShotDiagnosed(diagnosis=a_diagnosis(uuid4()), swing_score=a_score(None))
        )

        assert floor.results[-1] is None
        await subject.aclose()

    async def test_the_coached_line_carries_score_observation_and_frame(self) -> None:
        floor = ResultFloor()
        subject = flow(floor)
        shot_id = uuid4()
        capture_id = uuid4()
        advice = replace(
            an_advice(shot_id),
            observation="Face open at impact",
            swing_score=a_score(61),
        )

        await subject.on_advice_ready(
            AnalysisCompleted(
                shot_id=shot_id,
                advice=advice,
                evidence=EvidenceFrame(capture_id=capture_id, frame_id="impact_01"),
            )
        )

        result = floor.results[-1]
        assert isinstance(result, CoachingResult)
        assert result.swing_score == 61
        assert result.observation == "Face open at impact"
        assert result.evidence == EvidenceFrame(capture_id, "impact_01")
        await subject.aclose()


class TestTheCitedFrame:
    async def test_an_accepted_line_names_the_frame_in_the_capture_it_came_from(
        self, tmp_path
    ) -> None:
        reply = a_reply()
        reply["coach"]["evidence_frame_id"] = "impact_01"
        coach = Coach(ScriptedProvider(replies=(reply,)))
        await coach.service.open()
        await coach.service.on_club_selected(ClubSelected(club=DRIVER))
        detected = ShotDetected(trigger_id="16")
        await coach.service.on_shot_detected(detected)
        capture = a_capture_on_disk(tmp_path, strike_id=detected.event_id)
        await coach.service.on_swing_frames(SwingFramesReady(capture=capture))
        await coach.service.aclose()

        completed = coach.bus.of(AnalysisCompleted)[0]
        assert completed.advice.evidence_frame_id == "impact_01"
        assert completed.evidence == EvidenceFrame(capture.capture_id, "impact_01")

    async def test_a_line_that_cites_nothing_carries_no_frame(self, tmp_path) -> None:
        coach = Coach(ScriptedProvider(replies=(a_reply(),)))

        await coach.run(tmp_path)

        assert coach.bus.of(AnalysisCompleted)[0].evidence is None


def a_presenter(floor: FloorService) -> FloorPresenter:
    return FloorPresenter(floor, layout_from_tees(), club_path_length=0.4)


class TestTheProtocol:
    def test_the_result_fields_reach_the_browser(self) -> None:
        service = FloorService()
        capture_id = uuid4()
        a_presenter(service).show_advice(
            "SQUARE THE FACE",
            0.6,
            CoachingResult(
                observation="Face open at impact",
                swing_score=61,
                evidence=EvidenceFrame(capture_id, "impact_01"),
            ),
        )

        advice = FloorStatePayload.from_state(service.state).advice

        assert advice is not None
        assert advice.text == "SQUARE THE FACE"
        assert advice.observation == "Face open at impact"
        assert advice.swing_score == 61
        assert advice.evidence_url == f"/evidence/{capture_id}/impact_01"
        assert advice.evidence_frame_id == "impact_01"
        assert advice.cue_asset is None or "evidence" not in advice.cue_asset

    def test_no_evidence_means_no_url(self) -> None:
        service = FloorService()
        a_presenter(service).show_advice("FACE OPEN", 0.6)

        advice = FloorStatePayload.from_state(service.state).advice

        assert advice is not None
        assert advice.evidence_url is None and advice.swing_score is None

    def test_progress_is_a_stage_and_a_start_time_never_a_fraction(self) -> None:
        service = FloorService()
        a_presenter(service).show_progress(CoachingStage.ANALYZING)

        payload = FloorStatePayload.from_state(service.state).model_dump()

        assert set(payload["progress"]) == {"stage", "started_at"}
        assert payload["progress"]["stage"] == "analyzing"

    def test_both_halves_of_the_url_are_quoted(self) -> None:
        url = evidence_url(FloorEvidence(capture_id="a/b", frame_id="../x"))

        assert url == "/evidence/a%2Fb/..%2Fx"


class TestTheEvidenceRoute:
    async def stored(self, tmp_path: Path) -> tuple[TestClient, str, dict[str, Path]]:
        published, _ = await a_burst(tmp_path, _Bay())
        ready = next(e for e in published if isinstance(e, SwingFramesReady))
        root = tmp_path / "media" / "swing_shots"
        app = create_app(
            FloorService(), ConnectionRegistry(), evidence=EvidenceFrames(root).resolve
        )
        capture = ready.capture
        files = {
            frame: capture.directory / relative
            for frame, relative in frame_files(capture).items()
        }
        return TestClient(app), str(capture.capture_id), files

    async def test_an_exact_known_frame_is_served_as_stored(self, tmp_path) -> None:
        client, capture_id, files = await self.stored(tmp_path)
        frame, path = next(iter(files.items()))
        stored = next(
            p
            for p in (tmp_path / "media" / "swing_shots").rglob(path.name)
            if p.parent.name == path.parent.name
        )

        response = client.get(f"/evidence/{capture_id}/{frame}")

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"
        assert response.content == stored.read_bytes()
        assert "no-store" in response.headers["cache-control"]

    @pytest.mark.parametrize(
        "frame",
        [
            "metadata",
            "metadata.json",
            "..%2Fmetadata.json",
            "..%5Cmetadata.json",
            "%2E%2E%2F%2E%2E%2Fsecret",
            "swing_behind_99",
            "SWING_BEHIND_01",
        ],
    )
    async def test_unknown_or_traversing_frame_ids_are_not_found(
        self, tmp_path, frame: str
    ) -> None:
        client, capture_id, _files = await self.stored(tmp_path)

        assert client.get(f"/evidence/{capture_id}/{frame}").status_code == 404

    async def test_an_unknown_or_non_canonical_capture_is_not_found(
        self, tmp_path
    ) -> None:
        client, capture_id, files = await self.stored(tmp_path)
        frame = next(iter(files))

        for candidate in (
            str(uuid4()),
            capture_id.upper(),
            capture_id.replace("-", ""),
            "..",
            capture_id[:8],
        ):
            response = client.get(f"/evidence/{candidate}/{frame}")
            assert response.status_code == 404, candidate

    async def test_a_pruned_frame_degrades_to_no_image(self, tmp_path) -> None:
        client, capture_id, files = await self.stored(tmp_path)
        frame = next(iter(files))
        for path in (tmp_path / "media" / "swing_shots").rglob("*.jpg"):
            path.unlink()

        assert client.get(f"/evidence/{capture_id}/{frame}").status_code == 404

    def test_without_a_resolver_nothing_is_served(self) -> None:
        client = TestClient(create_app(FloorService(), ConnectionRegistry()))

        assert client.get(f"/evidence/{uuid4()}/impact_01").status_code == 404
