"""Coach V2: one authoritative focus, an observation before the action, a frame.

Three promises, each checked from the boundary that would break it.

**One focus per shot.** A diagnosis names a candidate before anything is asked.
The model may then choose any admissible cue, and the line that passes
validation is final. The session, the logs and the floor all follow the final
one; the candidate is kept only so the shift between them is visible.

**What was seen, then one thing to do.** The headline pair comes from the cue
registry, never from the model. The model's own observation sentence is judged
as part of the line, and an observation that is secretly a second instruction
is refused.

**A frame is cited from the list that was sent.** The schema enumerates the
ids, the validator refuses anything else, and the resolver turns an id into a
file only by exact lookup inside the capture directory.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from tests.unit.sheet_support import sheet_cells
from tests.unit.test_coach_protee import (
    Coach,
    a_capture_on_disk,
    a_reading,
    a_reply,
    an_open_face_grid,
)
from tests.unit.test_coaching_service import a_service, a_shot
from tests.unit.test_openai_provider import (
    FakeSdk,
    _response,
    a_provider,
    a_request,
)

from caddiej.adapters.openai.pricing import (
    INPUT_OVERRIDE,
    OUTPUT_OVERRIDE,
    estimate_cost,
)
from caddiej.adapters.openai.provider import OpenAIAdviceProvider
from caddiej.coaching import vx
from caddiej.coaching.cache import AdviceCache
from caddiej.coaching.cues import CUES, display_for_cue, observed_for_cue
from caddiej.coaching.fakes import ScriptedProvider
from caddiej.coaching.prompt import (
    MAX_DISPLAY_CHARACTERS,
    MAX_SPEECH_CHARACTERS,
    MAX_SPEECH_WORDS,
)
from caddiej.coaching.provider import ImageInput
from caddiej.coaching.session import CoachingSession
from caddiej.coaching.validate import (
    AdviceRejected,
    SpeechRejected,
    from_diagnosis,
    validate,
)
from caddiej.config import Settings
from caddiej.domain.coaching import (
    Advice,
    AdviceKind,
    CoachingFocus,
    Diagnosis,
    FocusCandidate,
    Observation,
)
from caddiej.domain.cues import CueId
from caddiej.domain.events import AnalysisCompleted, ShotCompleted, ShotDiagnosed
from caddiej.domain.shot import ClubKind, Handedness
from caddiej.swing_capture.stills import read_labeled_stills
from caddiej.swing_capture.store import (
    METADATA_NAME,
    METRICS_FRAME_ID,
    frame_files,
    frame_id,
    frame_path,
)

NOW = datetime(2026, 10, 2, 21, 0, tzinfo=UTC)

PATH_SEEN = "Your swing path moved across the ball."
PATH_DO = "Turn your trail shoulder down as the downswing starts."
FACE_SEEN = "Your face was open at impact."
FACE_DO = "Square the face a little earlier."


def a_slice_grid() -> dict[str, Any]:
    """A slice the numbers rank as curve first, with an out-to-in path behind it.

    Both cues are admissible, which is the shape the bay produced: the
    diagnosis named curve, and the model coached the path.
    """

    return an_open_face_grid(
        face_angle=a_reading(1.0, "deg", "open"),
        swing_path=a_reading(7.0, "deg", "out_to_in"),
        spin_axis=a_reading(12.0, "deg", "right"),
        launch_direction=a_reading(0.5, "deg", "right"),
    )


def a_v2_reply(
    metrics: dict[str, Any] | None = None,
    *,
    cue: str = "PATH_OUT_TO_IN",
    observation: str | None = PATH_SEEN,
    instruction: str | None = PATH_DO,
    evidence_frame_id: str | None = None,
) -> dict[str, Any]:
    reply = a_reply(
        a_slice_grid() if metrics is None else metrics, cue=cue, speech=instruction
    )
    reply["coach"]["observation"] = observation
    reply["coach"]["evidence_frame_id"] = evidence_frame_id
    return reply


def a_diagnosis(
    focus: CoachingFocus = CoachingFocus.FACE_CONTROL,
    cue: CueId | None = CueId.FACE_OPEN,
    kind: AdviceKind = AdviceKind.CORRECTION,
    club: ClubKind = ClubKind.DRIVER,
) -> Diagnosis:
    return Diagnosis(
        shot_id=uuid4(),
        focus=focus,
        severity=0.42,
        display="FACE OPEN",
        speech=FACE_DO,
        club=club,
        handedness=Handedness.RIGHT,
        baseline_shots=12,
        candidates=(
            FocusCandidate(focus=CoachingFocus.FACE_CONTROL, severity=0.42),
            FocusCandidate(focus=CoachingFocus.PATH, severity=0.31),
        ),
        observations=(
            Observation(
                focus=CoachingFocus.FACE_CONTROL,
                label="face open",
                value=6.5,
                unit="deg",
            ),
            Observation(
                focus=CoachingFocus.PATH, label="out to in", value=-4.0, unit="deg"
            ),
        ),
        kind=kind,
        cue=cue,
        admissible_cues=(CueId.FACE_OPEN, CueId.PATH_OUT_TO_IN),
    )


def an_advice(diagnosis: Diagnosis, focus: CoachingFocus, cue: CueId | None) -> Advice:
    return Advice(
        shot_id=diagnosis.shot_id,
        focus=focus,
        display="",
        speech="",
        severity=0.4,
        created_at=NOW,
        kind=diagnosis.kind,
        cue=cue,
    )


def records(caplog: pytest.LogCaptureFixture, event: str) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.getMessage() == event]


# --- one focus, from the candidate to the session --------------------------


class TestTheSessionFollowsTheFinalLine:
    def test_a_final_focus_replaces_the_candidate(self) -> None:
        session = CoachingSession()
        diagnosis = a_diagnosis(CoachingFocus.PATH, CueId.PATH_OUT_TO_IN)
        session.remember(diagnosis)
        assert session.focus is CoachingFocus.PATH

        moved = session.settle(
            an_advice(diagnosis, CoachingFocus.CURVE, CueId.CURVE_SLICE)
        )

        assert moved
        assert session.focus is CoachingFocus.CURVE
        assert session.progress.focus_shots == 1

    def test_reconciling_onto_the_running_focus_counts_the_shot_once(self) -> None:
        session = CoachingSession()
        for _ in range(2):
            session.remember(a_diagnosis(CoachingFocus.CURVE, CueId.CURVE_SLICE))
        assert session.progress.focus_shots == 2

        diagnosis = a_diagnosis(CoachingFocus.PATH, CueId.PATH_OUT_TO_IN)
        session.remember(diagnosis)
        assert session.focus is CoachingFocus.PATH
        session.settle(an_advice(diagnosis, CoachingFocus.CURVE, CueId.CURVE_SLICE))

        assert session.focus is CoachingFocus.CURVE
        assert session.progress.focus_shots == 3

    def test_the_cue_streak_follows_the_final_cue(self) -> None:
        session = CoachingSession()
        for _ in range(2):
            session.remember(a_diagnosis(CoachingFocus.CURVE, CueId.CURVE_SLICE))
        diagnosis = a_diagnosis(CoachingFocus.PATH, CueId.PATH_OUT_TO_IN)
        session.remember(diagnosis)
        session.settle(an_advice(diagnosis, CoachingFocus.CURVE, CueId.CURVE_SLICE))

        assert session.speech_variant(CoachingFocus.CURVE, CueId.CURVE_SLICE) == 2
        assert session.speech_variant(CoachingFocus.PATH, CueId.PATH_OUT_TO_IN) == 0

    def test_settling_on_the_candidate_changes_nothing(self) -> None:
        session = CoachingSession()
        diagnosis = a_diagnosis()
        session.remember(diagnosis)
        before = session.progress

        moved = session.settle(
            an_advice(diagnosis, CoachingFocus.FACE_CONTROL, CueId.FACE_OPEN)
        )

        assert not moved
        assert session.progress == before

    def test_settling_twice_is_the_same_as_settling_once(self) -> None:
        session = CoachingSession()
        diagnosis = a_diagnosis(CoachingFocus.PATH, CueId.PATH_OUT_TO_IN)
        session.remember(diagnosis)
        final = an_advice(diagnosis, CoachingFocus.CURVE, CueId.CURVE_SLICE)
        session.settle(final)
        session.settle(final)

        assert session.focus is CoachingFocus.CURVE
        assert session.progress.focus_shots == 1

    def test_a_late_line_for_an_older_shot_does_not_rewrite_the_lesson(self) -> None:
        session = CoachingSession()
        older = a_diagnosis(CoachingFocus.PATH, CueId.PATH_OUT_TO_IN)
        session.remember(older)
        session.remember(a_diagnosis())

        moved = session.settle(an_advice(older, CoachingFocus.CURVE, CueId.CURVE_SLICE))

        assert not moved
        assert session.focus is CoachingFocus.FACE_CONTROL

    def test_a_visual_final_line_still_never_redirects_the_lesson(self) -> None:
        session = CoachingSession()
        session.remember(a_diagnosis())
        diagnosis = a_diagnosis(CoachingFocus.PATH, CueId.PATH_OUT_TO_IN)
        session.remember(diagnosis)
        assert session.focus is CoachingFocus.PATH

        session.settle(
            an_advice(diagnosis, CoachingFocus.EARLY_EXTENSION, CueId.EARLY_EXTENSION)
        )

        assert session.focus is CoachingFocus.FACE_CONTROL
        assert session.progress.focus_shots == 1


class TestOneFocusThroughTheService:
    async def test_the_final_focus_is_the_one_the_session_and_floor_follow(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level("INFO")
        provider = ScriptedProvider(replies=(a_v2_reply(),))
        coach = Coach(provider)

        await coach.run(tmp_path)

        assert provider.calls == 1
        completed = coach.bus.of(AnalysisCompleted)
        assert len(completed) == 1
        advice = completed[0].advice
        assert advice.focus is CoachingFocus.PATH
        assert advice.cue is CueId.PATH_OUT_TO_IN
        session = coach.service._session
        assert session.focus is CoachingFocus.PATH
        assert session.progress.focus_shots == 1
        assert coach.service.health().focus == "path"
        assert coach.results[0].focus == "path"

        diagnosed = records(caplog, "coaching_shot_diagnosed")[0]
        assert diagnosed.coaching_stage == "candidate"
        assert diagnosed.coaching_focus == "curve"
        assert diagnosed.coaching_cue == "CURVE_SLICE"

        reconciled = records(caplog, "coaching_focus_reconciled")[0]
        assert reconciled.coaching_candidate_focus == "curve"
        assert reconciled.coaching_candidate_cue == "CURVE_SLICE"
        assert reconciled.coaching_final_focus == "path"
        assert reconciled.coaching_final_cue == "PATH_OUT_TO_IN"
        assert reconciled.coaching_focus == "path"

        ready = records(caplog, "coaching_advice_ready")[0]
        assert ready.coaching_stage == "final"
        assert ready.coaching_focus == "path"
        assert ready.coaching_cue == "PATH_OUT_TO_IN"
        assert ready.coaching_candidate_focus == "curve"
        assert ready.coaching_candidate_cue == "CURVE_SLICE"
        assert ready.coaching_observed == "OUT-TO-IN PATH"
        assert ready.coaching_display == "SWING FROM INSIDE"

    async def test_the_gspro_path_follows_the_final_line_too(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level("INFO")
        provider = ScriptedProvider(
            replies=[{"cue": "PATH_OUT_TO_IN", "speech": f"{PATH_SEEN} {PATH_DO}"}]
        )
        service, bus, record = a_service(provider)
        await service.open()

        await service.on_shot_completed(ShotCompleted(shot=a_shot(face=6.5, path=-6.0)))
        await service.aclose()

        assert bus.of(ShotDiagnosed)[0].diagnosis.focus is CoachingFocus.FACE_CONTROL
        assert provider.calls == 1
        advice = bus.of(AnalysisCompleted)[0].advice
        assert advice.focus is CoachingFocus.PATH
        assert service._session.focus is CoachingFocus.PATH
        assert service._session.progress.focus_shots == 1
        assert record.outcomes[0].advice.focus is CoachingFocus.PATH
        ready = records(caplog, "coaching_advice_ready")[0]
        assert ready.coaching_stage == "final"
        assert ready.coaching_candidate_focus == "face_control"
        assert ready.coaching_focus == "path"

    async def test_agreeing_with_the_candidate_reconciles_nothing(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level("INFO")
        reply = a_v2_reply(
            cue="CURVE_SLICE",
            observation="The ball curved with slice spin.",
            instruction="Release the face sooner through impact.",
        )
        coach = Coach(ScriptedProvider(replies=(reply,)))

        await coach.run(tmp_path)

        assert coach.bus.of(AnalysisCompleted)[0].advice.focus is CoachingFocus.CURVE
        assert not records(caplog, "coaching_focus_reconciled")
        ready = records(caplog, "coaching_advice_ready")[0]
        assert ready.coaching_focus == ready.coaching_candidate_focus == "curve"


# --- what was seen, then what to do ---------------------------------------


class TestTheRegistryNamesWhatWasSeen:
    @pytest.mark.parametrize("cue", CUES, ids=lambda cue: cue.cue_id.value)
    def test_every_cue_has_a_short_observation_headline(self, cue: Any) -> None:
        observed = cue.observed
        assert re.fullmatch(r"[A-Z][A-Z \-]*", observed)
        assert 2 <= len(observed.split()) <= 4
        assert len(observed) <= MAX_DISPLAY_CHARACTERS
        assert observed != cue.display
        assert "LEFT" not in observed and "RIGHT" not in observed

    def test_the_headline_pair_is_looked_up_from_the_cue(self) -> None:
        assert observed_for_cue(CueId.PATH_OUT_TO_IN) == "OUT-TO-IN PATH"
        assert display_for_cue(CueId.PATH_OUT_TO_IN) == "SWING FROM INSIDE"
        assert observed_for_cue(CueId.CURVE_SLICE) == "SLICE SPIN"
        assert observed_for_cue(CueId.EARLY_EXTENSION) == "EARLY EXTENSION"

    def test_two_short_sentences_fit_the_speech_budget(self) -> None:
        line = f"{PATH_SEEN} {PATH_DO}"
        assert len(line) <= MAX_SPEECH_CHARACTERS
        assert len(line.split()) <= MAX_SPEECH_WORDS


class TestObservationAndInstruction:
    def test_an_observation_and_one_instruction_on_one_focus_pass(self) -> None:
        payload = {
            "cue": "FACE_OPEN",
            "observation": FACE_SEEN,
            "speech": f"{FACE_SEEN} {FACE_DO}",
        }

        advice = validate(payload, a_diagnosis(), NOW)

        assert advice.speech == f"{FACE_SEEN} {FACE_DO}"
        assert advice.spoken_observation == FACE_SEEN
        assert advice.observation == "FACE OPEN"
        assert advice.display == "PASS THE TOE EARLY"

    def test_an_observation_that_is_an_instruction_is_refused(self) -> None:
        payload = {
            "cue": "FACE_OPEN",
            "observation": "Square the face sooner.",
            "speech": "Square the face sooner. Keep that same feeling.",
        }

        with pytest.raises(SpeechRejected, match="observation was itself"):
            validate(payload, a_diagnosis(), NOW)

    def test_an_observation_about_another_focus_is_refused(self) -> None:
        payload = {
            "cue": "FACE_OPEN",
            "observation": PATH_SEEN,
            "speech": f"{PATH_SEEN} {FACE_DO}",
        }

        with pytest.raises(SpeechRejected, match="path"):
            validate(payload, a_diagnosis(), NOW)

    def test_a_second_independent_instruction_is_refused(self) -> None:
        joined = vx.coach_payload(
            {
                "coach": {
                    "kind": "correction",
                    "cue": "FACE_OPEN",
                    "observation": None,
                    "instruction": FACE_DO,
                    "reinforcement": "Keep your head still.",
                }
            }
        )
        assert joined is not None

        with pytest.raises(SpeechRejected, match="2 instructions"):
            validate(joined, a_diagnosis(), NOW)

    def test_a_reinforcement_cannot_ride_along_after_an_instruction(self) -> None:
        joined = vx.coach_payload(
            {
                "coach": {
                    "kind": "correction",
                    "cue": "FACE_OPEN",
                    "observation": FACE_SEEN,
                    "instruction": FACE_DO,
                    "reinforcement": "Keep your head still.",
                }
            }
        )

        assert joined is not None
        assert joined["speech"] == f"{FACE_SEEN} {FACE_DO}"

    def test_the_template_line_carries_the_headline_and_no_frame(self) -> None:
        advice = from_diagnosis(a_diagnosis(), NOW)

        assert advice.observation == "FACE OPEN"
        assert advice.spoken_observation == ""
        assert advice.evidence_frame_id is None


# --- the cited frame --------------------------------------------------------


class TestTheCitedFrame:
    SENT = ("swing_side_02", "impact_01", METRICS_FRAME_ID)

    def payload(self, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {"cue": "FACE_OPEN", "speech": FACE_DO}
        payload.update(overrides)
        return payload

    def test_a_frame_that_was_sent_is_kept(self) -> None:
        advice = validate(
            self.payload(evidence_frame_id="swing_side_02"),
            a_diagnosis(),
            NOW,
            frame_ids=self.SENT,
        )
        assert advice.evidence_frame_id == "swing_side_02"

    @pytest.mark.parametrize(
        "invented",
        ["swing_side_09", "swing_side/frame_002.jpg", "../metadata.json", ""],
    )
    def test_a_frame_that_was_never_sent_is_refused(self, invented: str) -> None:
        with pytest.raises(AdviceRejected, match="not a frame sent"):
            validate(
                self.payload(evidence_frame_id=invented),
                a_diagnosis(),
                NOW,
                frame_ids=self.SENT,
            )

    def test_naming_a_frame_when_none_was_sent_is_refused(self) -> None:
        with pytest.raises(AdviceRejected, match="not a frame sent"):
            validate(self.payload(evidence_frame_id="impact_01"), a_diagnosis(), NOW)

    def test_no_frame_is_an_honest_answer(self) -> None:
        cited = validate(
            self.payload(evidence_frame_id=None),
            a_diagnosis(),
            NOW,
            frame_ids=self.SENT,
        )
        absent = validate(self.payload(), a_diagnosis(), NOW, frame_ids=self.SENT)

        assert cited.evidence_frame_id is None
        assert absent.evidence_frame_id is None

    def test_a_frame_that_is_not_text_is_refused(self) -> None:
        with pytest.raises(AdviceRejected, match="not a string"):
            validate(
                self.payload(evidence_frame_id=2),
                a_diagnosis(),
                NOW,
                frame_ids=self.SENT,
            )

    def test_the_schema_offers_exactly_the_ids_sent(self) -> None:
        coach = vx.reply_schema((CueId.FACE_OPEN,), self.SENT)["properties"]["coach"]
        offered = coach["properties"]["evidence_frame_id"]["anyOf"]
        assert offered == [
            {"type": "null"},
            {"type": "string", "enum": list(self.SENT)},
        ]
        nothing = vx.reply_schema((CueId.FACE_OPEN,))["properties"]["coach"]
        assert nothing["properties"]["evidence_frame_id"] == {"type": "null"}

    def test_a_cached_line_forgets_the_frame_of_the_swing_it_was_about(self) -> None:
        cache = AdviceCache()
        advice = validate(
            self.payload(evidence_frame_id="impact_01"),
            a_diagnosis(),
            NOW,
            frame_ids=self.SENT,
        )
        cache.put("key", advice)

        again = cache.get("key", uuid4(), NOW)

        assert again is not None and again.evidence_frame_id is None


class TestTheFrameThroughTheService:
    async def test_every_image_carries_an_id_and_the_schema_offers_only_those(
        self, tmp_path: Path
    ) -> None:
        provider = ScriptedProvider(replies=(a_v2_reply(),))
        coach = Coach(provider, max_images=8)

        await coach.run(tmp_path, behind=4, side=3, impact=1)

        request = provider.requests[0]
        (sheet,) = request.images
        assert sheet.frame_id == "", "the sheet itself is never citable"
        ids = list(sheet_cells(sheet.data))
        assert ids == [
            "DTL_01",
            "DTL_02",
            "DTL_03",
            "DTL_04",
            "FO_01",
            "FO_02",
            "FO_03",
            "IMPACT",
            "ALL_VALUES",
        ]
        coach_schema = request.schema["properties"]["coach"]["properties"]
        assert coach_schema["evidence_frame_id"]["anyOf"][1]["enum"] == ids
        assert "FO_02 SWING_SIDE / FACE_ON" in request.evidence
        assert "IMPACT, ALL_VALUES." in request.evidence

    async def test_all_values_stays_outside_the_frame_budget(
        self, tmp_path: Path
    ) -> None:
        provider = ScriptedProvider(replies=(a_v2_reply(),))
        coach = Coach(provider, max_images=8)

        await coach.run(tmp_path, behind=8, side=8, impact=2)

        images = provider.requests[0].images
        assert provider.calls == 1
        assert len(images) == 1
        cells = sheet_cells(images[0].data)
        assert sum(1 for cell in cells if cell != "ALL_VALUES") == 8
        assert cells[-1] == "ALL_VALUES"

    async def test_a_cited_frame_reaches_the_floor_and_resolves_to_its_file(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level("INFO")
        provider = ScriptedProvider(
            replies=(a_v2_reply(evidence_frame_id="swing_side_02"),)
        )
        coach = Coach(provider)

        detected = await coach.run(tmp_path)

        advice = coach.bus.of(AnalysisCompleted)[0].advice
        assert advice.evidence_frame_id == "swing_side_02"
        ready = records(caplog, "coaching_advice_ready")[0]
        assert ready.coaching_evidence_frame_id == "swing_side_02"

        capture = a_capture_on_disk(tmp_path, strike_id=detected.event_id)
        resolved = frame_path(capture, "swing_side_02")
        assert resolved == (tmp_path / "latest_swing" / "swing_side" / "frame_002.jpg")
        assert resolved is not None and resolved.is_file()

    async def test_an_invented_frame_never_reaches_the_floor(
        self, tmp_path: Path
    ) -> None:
        provider = ScriptedProvider(
            replies=(a_v2_reply(evidence_frame_id="swing_side_07"),)
        )
        coach = Coach(provider)

        await coach.run(tmp_path)

        assert not [
            event
            for event in coach.bus.of(AnalysisCompleted)
            if event.advice.evidence_frame_id == "swing_side_07"
        ]
        assert "evidence_frame_id" in (coach.results[0].detail or "")

    async def test_the_id_to_file_mapping_is_kept_with_the_capture(
        self, tmp_path: Path
    ) -> None:
        coach = Coach(ScriptedProvider(replies=(a_v2_reply(),)))

        await coach.run(tmp_path)

        payload = json.loads(
            (tmp_path / "latest_swing" / METADATA_NAME).read_text(encoding="utf-8")
        )
        mapping = payload["provider_frame_ids"]
        assert mapping["swing_side_02"] == "swing_side/frame_002.jpg"
        assert mapping["impact_01"] == "impact/frame_001.jpg"
        assert mapping["all_values"] == "metrics/all_values.jpg"
        assert len(mapping) == 9


class TestTheFrameResolver:
    def test_ids_name_the_panel_and_the_stills_place_in_it(self, tmp_path) -> None:
        capture = a_capture_on_disk(tmp_path)
        side = next(
            camera for camera in capture.cameras if camera.camera_id == "swing_side"
        )

        assert frame_id(side.frames[1]) == "swing_side_02"
        assert set(frame_files(capture)) == {
            "swing_behind_01",
            "swing_behind_02",
            "swing_behind_03",
            "swing_behind_04",
            "swing_side_01",
            "swing_side_02",
            "swing_side_03",
            "impact_01",
            "all_values",
        }

    def test_a_known_id_resolves_to_the_real_file(self, tmp_path) -> None:
        capture = a_capture_on_disk(tmp_path)

        impact = frame_path(capture, "impact_01")
        grid = frame_path(capture, "all_values")

        assert impact == tmp_path / "latest_swing" / "impact" / "frame_001.jpg"
        assert grid == tmp_path / "latest_swing" / "metrics" / "all_values.jpg"

    @pytest.mark.parametrize(
        "asked",
        [
            "swing_side_04",
            "swing_side",
            "frame_001.jpg",
            "swing_side/frame_001.jpg",
            "../latest_swing/metadata.json",
            "..\\metadata.json",
            "metadata.json",
            "SWING_SIDE_01",
            " swing_side_01",
            "",
        ],
    )
    def test_anything_but_an_exact_known_id_resolves_to_nothing(
        self, tmp_path, asked: str
    ) -> None:
        capture = a_capture_on_disk(tmp_path)

        assert frame_path(capture, asked) is None

    def test_an_absolute_path_resolves_to_nothing(self, tmp_path) -> None:
        capture = a_capture_on_disk(tmp_path)
        target = tmp_path / "latest_swing" / "impact" / "frame_001.jpg"

        assert frame_path(capture, str(target)) is None

    def test_a_still_that_has_gone_resolves_to_nothing(self, tmp_path) -> None:
        capture = a_capture_on_disk(tmp_path)
        (tmp_path / "latest_swing" / "impact" / "frame_001.jpg").unlink()

        assert frame_path(capture, "impact_01") is None

    def test_no_all_values_still_means_no_all_values_id(self, tmp_path) -> None:
        capture = a_capture_on_disk(tmp_path, metrics=False)

        assert METRICS_FRAME_ID not in frame_files(capture)
        assert frame_path(capture, METRICS_FRAME_ID) is None

    async def test_the_still_reader_names_each_still_by_its_id(self, tmp_path) -> None:
        capture = a_capture_on_disk(tmp_path)

        sent = await read_labeled_stills(capture, 8)

        assert [item[3] for item in sent][:2] == ["swing_behind_01", "swing_behind_02"]
        for _data, _label, _panel, identifier, _detail in sent:
            assert frame_path(capture, identifier) is not None


# --- the provider announces ids --------------------------------------------


async def test_each_image_is_announced_by_its_id_and_its_view() -> None:
    module = FakeSdk()
    images = (
        ImageInput(
            data=b"side", label="SWING_SIDE / FACE_ON", frame_id="swing_side_02"
        ),
        ImageInput(data=b"grid", label="ALL_VALUES", frame_id="all_values"),
    )

    await a_provider(module).complete(a_request(images=images))

    parts = module.clients[0].calls[0]["input"][0]["content"]
    texts = [part["text"] for part in parts if part["type"] == "input_text"]
    assert "Next image: swing_side_02 (SWING_SIDE / FACE_ON)" in texts
    assert "Next image: all_values (ALL_VALUES)" in texts


# --- the foundation this pass did not move ---------------------------------


class TestTheFoundationIsUnchanged:
    def test_the_protee_request_keeps_its_ceiling_timeout_and_model(self) -> None:
        fields = Settings.model_fields
        assert fields["coaching_protee_max_output_tokens"].default == 10000
        assert fields["coach_openai_timeout"].default == 30.0
        assert fields["coaching_model"].default == "gpt-5-mini"

    async def test_the_request_carries_the_configured_ceiling_and_timeout(
        self, tmp_path: Path
    ) -> None:
        provider = ScriptedProvider(replies=(a_v2_reply(),))
        coach = Coach(
            provider,
            protee_max_output_tokens=6000,
            protee_timeout_seconds=30.0,
            protee_budget_seconds=45.0,
        )

        await coach.run(tmp_path)

        assert provider.calls == 1
        request = provider.requests[0]
        assert request.max_output_tokens == 6000
        assert request.timeout_seconds == pytest.approx(30.0, abs=0.5)


# --- what it cost, as an estimate -------------------------------------------


class TestCostEstimate:
    def test_an_unknown_model_has_no_price(self) -> None:
        assert estimate_cost("somebody-elses-model", 1000, 500) is None

    @pytest.mark.parametrize(
        ("used", "made"), [(None, 500), (1000, None), (None, None)]
    )
    def test_missing_usage_has_no_price(
        self, used: int | None, made: int | None
    ) -> None:
        assert estimate_cost("gpt-5-mini", used, made) is None

    def test_a_known_model_is_priced_per_million_tokens(self) -> None:
        assert estimate_cost("gpt-5-mini", 1000, 500) == pytest.approx(0.00125)

    def test_a_dated_snapshot_is_priced_as_its_alias(self) -> None:
        assert estimate_cost("gpt-5-mini-2025-08-07", 1000, 500) == pytest.approx(
            0.00125
        )

    def test_an_operator_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(INPUT_OVERRIDE, "1.0")
        monkeypatch.setenv(OUTPUT_OVERRIDE, "4.0")
        assert estimate_cost("somebody-elses-model", 1_000_000, 0) == pytest.approx(1.0)
        assert estimate_cost("gpt-5-mini", 0, 1_000_000) == pytest.approx(4.0)

    def test_half_an_override_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(INPUT_OVERRIDE, "1.0")
        monkeypatch.delenv(OUTPUT_OVERRIDE, raising=False)
        assert estimate_cost("somebody-elses-model", 1000, 500) is None

    async def test_the_usage_log_carries_the_estimate(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level("INFO")
        module = FakeSdk()
        provider = OpenAIAdviceProvider(
            api_key="sk-test", model="gpt-5-mini", module=module
        )
        provider._connect()
        module.clients[0].response = _response(
            json.dumps({"ok": True}),
            model="gpt-5-mini-2025-08-07",
            input_tokens=1000,
            output_tokens=500,
        )

        await provider.complete(a_request())

        usage = records(caplog, "coaching_openai_usage")[0]
        assert usage.coaching_cost_usd == pytest.approx(0.00125)

    async def test_an_unpriced_model_logs_no_estimate_and_still_answers(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level("INFO")
        module = FakeSdk()

        reply = await a_provider(module).complete(a_request())

        assert reply.payload
        assert records(caplog, "coaching_openai_usage")[0].coaching_cost_usd is None
