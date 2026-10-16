"""The Coach speech contract: one cue, one focus, one instruction.

A reply is an instruction and an optional reinforcement that may only repeat
it. Two instructions, or a sentence reaching into another focus, are refused
and never spoken; a sound cue with refused wording is spoken in that cue's own
reviewed sentence instead.

The cue itself is CaddieJ's. A model reading the grid chooses before anything
is admitted, so a cue the numbers then refuse loses to CaddieJ's, its words
survive only if they fit that cue, and the player is never left with two words
on the floor and a silent room.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest
from tests.unit.test_coach_protee import (
    DRIVER,
    Coach,
    a_capture_on_disk,
    a_reading,
    a_reply,
    an_open_face_grid,
)
from tests.unit.test_play_flow import FloorSpy, flow
from tests.unit.test_speech_service import RecordingSink, Table

from caddiej.coaching import vx
from caddiej.coaching.cues import CUE_SPEECH, CUES_BY_ID, cue_speech
from caddiej.coaching.fakes import ScriptedProvider
from caddiej.coaching.provider import AdviceRequest, ProviderReply
from caddiej.coaching.validate import SpeechRejected, _speech, _stays_on_focus
from caddiej.domain.coaching import AdviceKind, AdviceSource, CoachingFocus
from caddiej.domain.cues import CueId
from caddiej.domain.events import (
    AnalysisCompleted,
    BallDetected,
    ClubSelected,
    LaunchMonitorReadinessChanged,
    ShotDetected,
    ShotDiagnosed,
    SwingFramesReady,
)
from caddiej.domain.shot import Handedness

HOLD = "Hold the face a little longer through impact."
SAME_FEELING = "Keep that same feeling on the next swing."
WEIGHT = "Shift more weight onto your lead side."
CLOSED_SEEN = "Your face was closed at impact."
# The face cues' own reviewed lines, written out rather than looked up. They
# were the generic face-control templates until the registry audit gave each
# corrective cue a physical action of its own, and the whole point of that
# change is that these words are reviewed individually.
FACE_CLOSED_LINE = "Let the rotation slow down through the ball."
FACE_OPEN_LINE = "Let the toe pass through the ball a fraction sooner."


def a_closed_face_grid() -> dict[str, Any]:
    return an_open_face_grid(
        face_angle=a_reading(6.5, "deg", "closed"),
        launch_direction=a_reading(0.5, "deg", "left"),
    )


def a_face_closed_reply(
    instruction: str | None, reinforcement: str | None = None
) -> dict[str, Any]:
    return a_reply(
        a_closed_face_grid(),
        cue="FACE_CLOSED",
        speech=instruction,
        reinforcement=reinforcement,
    )


async def coached(tmp_path, reply: dict[str, Any]) -> Coach:
    coach = Coach(ScriptedProvider(replies=(reply,)))
    await coach.run(tmp_path)
    return coach


async def spoken(events: list[AnalysisCompleted]) -> list[str]:
    async with Table() as table:
        for event in events:
            await table.service.on_analysis_completed(event)
        await table.settled()
    return [text for text, _ in table.engine.calls]


# --- the schema asks for the shape it accepts ------------------------------


class TestTheSchema:
    def test_coach_is_cue_observation_instruction_and_an_optional_reinforcement(
        self,
    ) -> None:
        coach = vx.reply_schema((CueId.FACE_CLOSED,))["properties"]["coach"]
        assert "speech" not in coach["properties"]
        assert coach["required"] == [
            "kind",
            "cue",
            "observation",
            "instruction",
            "reinforcement",
            "evidence_frame_id",
        ]
        for name in ("observation", "reinforcement"):
            nullable = {item["type"] for item in coach["properties"][name]["anyOf"]}
            assert nullable == {"null", "string"}

    def test_the_prompt_states_one_focus_one_instruction_and_both_examples(
        self,
    ) -> None:
        system = vx.SYSTEM.replace("\n  ", " ")
        assert "ONE thing to do" in system
        assert "it never tells the player to do anything" in system
        assert 'starts with "Your" or "The"' in system
        assert "starts with a verb" in system
        assert "Shift more weight onto your" in system
        assert vx.PROTEE_PROMPT_VERSION == "vx13"

    def test_the_prompt_says_lead_and_trail_rather_than_left_and_right(self) -> None:
        system = vx.SYSTEM.replace("\n  ", " ")
        assert 'NEVER SAY "LEFT" OR "RIGHT". Say "lead" or "trail".' in system

    def test_each_offered_cue_names_its_focus_and_floor_words(self) -> None:
        evidence = vx.render_evidence(
            club=DRIVER.kind,
            handedness=Handedness.RIGHT,
            has_metrics=True,
            swing_images=(),
            cues=(CueId.FACE_CLOSED,),
            session_focus=None,
            focus_shots=0,
        )
        assert "FACE_CLOSED" in evidence
        assert "focus face control" in evidence
        assert "floor shows FACE CLOSED then SLOW THE ROTATION" in evidence

    def test_instruction_and_reinforcement_are_judged_as_one_line(self) -> None:
        joined = vx.coach_payload(a_face_closed_reply(HOLD, SAME_FEELING))
        assert joined is not None
        assert joined["speech"] == f"{HOLD} {SAME_FEELING}"
        alone = vx.coach_payload(a_face_closed_reply(HOLD))
        assert alone is not None and alone["speech"] == HOLD

    def test_an_observation_leads_and_the_instruction_follows(self) -> None:
        reply = a_face_closed_reply(HOLD, SAME_FEELING)
        reply["coach"]["observation"] = CLOSED_SEEN
        joined = vx.coach_payload(reply)
        assert joined is not None
        assert joined["speech"] == f"{CLOSED_SEEN} {HOLD}"
        assert joined["observation"] == CLOSED_SEEN

    def test_a_reinforcement_may_follow_the_observation_in_place_of_an_instruction(
        self,
    ) -> None:
        reply = a_face_closed_reply(None, SAME_FEELING)
        reply["coach"]["kind"] = "reinforcement"
        reply["coach"]["observation"] = "Your face was square at impact."
        joined = vx.coach_payload(reply)
        assert joined is not None
        assert joined["speech"] == f"Your face was square at impact. {SAME_FEELING}"


# --- what the validator counts ---------------------------------------------


class TestOneInstruction:
    def test_one_instruction_in_one_sentence_passes(self) -> None:
        assert _speech({"speech": HOLD}) == (HOLD, 1)

    def test_one_instruction_reinforced_across_two_sentences_passes(self) -> None:
        text = f"{HOLD} {SAME_FEELING}"
        assert _speech({"speech": text}) == (text, 1)

    @pytest.mark.parametrize(
        "text",
        [
            f"Hold the face through impact. {WEIGHT}",
            "Hold the face longer. Keep your head down.",
            "Square the face earlier. Turn your shoulders through.",
            "Hold the face longer. Keep that lead arm straight.",
        ],
    )
    def test_two_instructions_fail(self, text: str) -> None:
        with pytest.raises(SpeechRejected, match="2 instructions"):
            _speech({"speech": text})

    def test_a_restatement_before_the_instruction_fails(self) -> None:
        with pytest.raises(SpeechRejected, match="restated before"):
            _speech({"speech": "Keep that feeling. Hold the face longer."})

    async def test_a_correction_that_only_restates_fails(self, tmp_path) -> None:
        coach = await coached(tmp_path, a_face_closed_reply("Keep that same feeling."))

        assert coach.bus.of(AnalysisCompleted)[0].advice.speech == FACE_CLOSED_LINE
        assert "only restated" in (coach.results[0].detail or "")

    def test_an_observation_and_one_instruction_passes(self) -> None:
        text = "Good face control. Keep that release."
        assert _speech({"speech": text}) == (text, 1)


class TestOnFocus:
    def test_face_speech_on_a_face_cue_passes(self) -> None:
        _stays_on_focus(f"{HOLD} {SAME_FEELING}", CoachingFocus.FACE_CONTROL)

    @pytest.mark.parametrize(
        ("text", "family"),
        [
            (WEIGHT, "weight"),
            ("Turn your shoulders through the ball.", "body"),
            ("Feel the club come more from the inside.", "path"),
            ("Ease back to a smoother tempo.", "tempo"),
            ("Good path. Square the face a little earlier.", "path"),
        ],
    )
    def test_no_other_focus_is_introduced(self, text: str, family: str) -> None:
        with pytest.raises(SpeechRejected, match=family):
            _stays_on_focus(text, CoachingFocus.FACE_CONTROL)

    def test_a_weight_cue_may_talk_about_weight(self) -> None:
        _stays_on_focus("Load the trail side and stay centred.", CoachingFocus.SWAY)


# --- the cue library's own sentences ---------------------------------------


class TestCueSpeech:
    @pytest.mark.parametrize("cue_id", sorted(CUE_SPEECH, key=lambda cue: cue.value))
    def test_every_reviewed_line_passes_for_its_own_cue(self, cue_id: CueId) -> None:
        line = CUE_SPEECH[cue_id]
        cue = CUES_BY_ID[cue_id]
        _, instructions = _speech({"speech": line})
        if cue.kind is AdviceKind.CORRECTION:
            assert instructions == 1
        _stays_on_focus(line, cue.focus)

    def test_each_face_cue_says_its_own_action_and_nothing_else(self) -> None:
        assert cue_speech(CueId.FACE_CLOSED) == FACE_CLOSED_LINE
        assert cue_speech(CueId.FACE_OPEN) == FACE_OPEN_LINE
        assert cue_speech(CueId.FACE_CLOSED) != cue_speech(CueId.FACE_OPEN)

    def test_a_cue_without_a_reviewed_line_has_none(self) -> None:
        # The putting cues, since the audit gave every full-swing corrective
        # cue a sentence. A cue with none is still floor-only, by design.
        assert cue_speech(CueId.PUTT_FACE_OPEN) is None


# --- through the service, to the floor and to Kokoro -----------------------


class TestThroughTheService:
    async def test_valid_two_sentence_speech_is_the_models_and_reaches_kokoro(
        self, tmp_path
    ) -> None:
        coach = await coached(tmp_path, a_face_closed_reply(HOLD, SAME_FEELING))

        completed = coach.bus.of(AnalysisCompleted)
        assert len(completed) == 1
        advice = completed[0].advice
        assert advice.cue is CueId.FACE_CLOSED
        assert advice.focus is CoachingFocus.FACE_CONTROL
        assert advice.display == "SLOW THE ROTATION"
        assert advice.source is AdviceSource.MODEL
        assert advice.speech == f"{HOLD} {SAME_FEELING}"
        assert coach.results[0].status == "ok"
        assert await spoken(completed) == [f"{HOLD} {SAME_FEELING}"]

    async def test_one_sentence_speech_reaches_kokoro(self, tmp_path) -> None:
        coach = await coached(tmp_path, a_face_closed_reply(HOLD))
        assert await spoken(coach.bus.of(AnalysisCompleted)) == [HOLD]

    async def test_two_instructions_are_never_spoken_the_cue_line_is(
        self, tmp_path, caplog
    ) -> None:
        caplog.set_level("INFO")
        coach = await coached(
            tmp_path, a_face_closed_reply("Hold the face through impact.", WEIGHT)
        )

        completed = coach.bus.of(AnalysisCompleted)
        assert len(completed) == 1
        advice = completed[0].advice
        assert advice.cue is CueId.FACE_CLOSED
        assert advice.display == "SLOW THE ROTATION"
        assert advice.speech == FACE_CLOSED_LINE
        assert advice.source is AdviceSource.TEMPLATE
        said = await spoken(completed)
        assert said == [FACE_CLOSED_LINE]
        assert not any("weight" in line.lower() for line in said)

        result = coach.results[0]
        assert result.status == "template"
        assert result.cue_id == "FACE_CLOSED"
        assert "2 instructions" in (result.detail or "")
        record = next(
            r for r in caplog.records if r.getMessage() == "coaching_speech_fallback"
        )
        assert record.coaching_cue == "FACE_CLOSED"
        assert "2 instructions" in record.coaching_detail
        assert "weight" not in record.coaching_detail.lower()

    async def test_an_off_focus_instruction_falls_back_to_the_same_cue(
        self, tmp_path
    ) -> None:
        coach = await coached(tmp_path, a_face_closed_reply(WEIGHT))

        advice = coach.bus.of(AnalysisCompleted)[0].advice
        assert advice.cue is CueId.FACE_CLOSED
        assert advice.speech == FACE_CLOSED_LINE
        assert "weight" in (coach.results[0].detail or "")

    async def test_the_floor_keeps_the_deterministic_cue_display(
        self, tmp_path
    ) -> None:
        coach = await coached(
            tmp_path, a_face_closed_reply("Hold the face through impact.", WEIGHT)
        )

        advice = coach.bus.of(AnalysisCompleted)[0].advice
        assert advice.display == CUES_BY_ID[CueId.FACE_CLOSED].display

    async def test_a_cue_the_numbers_reject_is_never_said_caddiejs_cue_is(
        self, tmp_path
    ) -> None:
        # The grid is an open face, so FACE_CLOSED is not admissible: nothing
        # may be said for it. Its words hold two instructions, so they are
        # refused for FACE_OPEN as well, and FACE_OPEN's reviewed line is said.
        coach = await coached(
            tmp_path,
            a_reply(cue="FACE_CLOSED", speech="Hold the face.", reinforcement=WEIGHT),
        )

        completed = coach.bus.of(AnalysisCompleted)
        assert [event.advice.cue for event in completed] == [CueId.FACE_OPEN]
        said = await spoken(completed)
        assert said == [cue_speech(CueId.FACE_OPEN)]
        assert FACE_CLOSED_LINE not in said
        assert not coach.bus.of(ShotDiagnosed)
        assert coach.results[0].status == "ok"


class HeldBadWording(ScriptedProvider):
    """Holds the first answer until released, so a newer strike can land."""

    def __init__(self) -> None:
        super().__init__(
            replies=(a_face_closed_reply("Hold the face through impact.", WEIGHT),)
        )
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request: AdviceRequest) -> ProviderReply:
        self.started.set()
        await self.release.wait()
        return await super().complete(request)


class TestNewestShotWins:
    async def test_a_newer_strike_drops_the_cue_line_for_the_old_one(
        self, tmp_path
    ) -> None:
        provider = HeldBadWording()
        coach = Coach(provider, timeout_seconds=5.0)
        await coach.service.open()
        await coach.service.on_club_selected(ClubSelected(club=DRIVER))
        old = ShotDetected(trigger_id="16")
        await coach.service.on_shot_detected(old)
        capture = a_capture_on_disk(tmp_path, strike_id=old.event_id, trigger_id="16")
        task = asyncio.create_task(
            coach.service.on_swing_frames(SwingFramesReady(capture=capture))
        )
        await asyncio.wait_for(provider.started.wait(), 2.0)

        await coach.service.on_shot_detected(ShotDetected(trigger_id="17"))
        provider.release.set()
        await task
        await coach.service.aclose()

        assert not [
            event
            for event in coach.bus.of(AnalysisCompleted)
            if event.shot_id == old.event_id
        ]


class TestBallReady:
    async def test_ball_ready_stales_a_cue_line(self, tmp_path) -> None:
        # Reversed by the first hardware test: the next ball is now the end of
        # the previous shot's coaching, reviewed line or not.
        coach = await coached(
            tmp_path, a_face_closed_reply("Hold the face through impact.", WEIGHT)
        )
        line = coach.bus.of(AnalysisCompleted)[0]

        floor = FloorSpy()
        subject = flow(floor)
        strike = ShotDetected(trigger_id="16")
        await subject.on_shot_detected(strike)
        await subject.on_ball_detected(BallDetected())
        await subject.on_readiness_changed(LaunchMonitorReadinessChanged(ready=True))
        advice = replace(line.advice, shot_id=strike.event_id)
        await subject.on_advice_ready(
            AnalysisCompleted(shot_id=strike.event_id, advice=advice)
        )

        assert not floor.showing_advice


# --- one authority: CaddieJ's cue, the model's words only if they fit -----

FROM_INSIDE = "Feel the club come more from the inside."
SLICE = CUES_BY_ID[CueId.CURVE_SLICE]
SCATTER = CUES_BY_ID[CueId.SCATTER]


def a_slice_grid() -> dict[str, Any]:
    """Face and path inside their bands, spin axis well right: slice spin.

    PATH_OUT_TO_IN is offered in the request, because nothing is known until
    the grid is read, and not admitted afterwards, which is the hardware log.
    """

    return an_open_face_grid(
        face_angle=a_reading(1.0, "deg", "open"),
        swing_path=a_reading(0.5, "deg", "out_to_in"),
        launch_direction=a_reading(0.5, "deg", "right"),
        spin_axis=a_reading(14.0, "deg", "right"),
    )


def a_level_grid(carry: float) -> dict[str, Any]:
    return an_open_face_grid(
        face_angle=a_reading(0.5, "deg", "open"),
        swing_path=a_reading(0.5, "deg", "in_to_out"),
        launch_direction=a_reading(0.5, "deg", "right"),
        spin_axis=a_reading(0.5, "deg", "right"),
        carry=a_reading(carry, "yd"),
    )


def a_path_reply(
    metrics: dict[str, Any],
    instruction: str | None = FROM_INSIDE,
    reinforcement: str | None = None,
    observation: str | None = None,
) -> dict[str, Any]:
    reply = a_reply(
        metrics, cue="PATH_OUT_TO_IN", speech=instruction, reinforcement=reinforcement
    )
    reply["coach"]["observation"] = observation
    return reply


async def scattered(tmp_path, final: dict[str, Any]) -> Coach:
    """Five widely spread drives, then the one the test is about."""

    carries = (150.0, 260.0, 190.0, 280.0, 130.0)
    replies = (
        *(a_reply(a_level_grid(carry), kind="none", cue=None) for carry in carries),
        final,
    )
    coach = Coach(ScriptedProvider(replies=replies))
    await coach.service.open()
    await coach.service.on_club_selected(ClubSelected(club=DRIVER))
    for index in range(len(replies)):
        await coach.strike(tmp_path, trigger_id=str(20 + index))
        async with asyncio.timeout(2.0):
            while len(coach.results) <= index:
                await asyncio.sleep(0.01)
    await coach.service.aclose()
    return coach


def messages(caplog, name: str) -> list[Any]:
    return [record for record in caplog.records if record.getMessage() == name]


class TestOneAuthority:
    async def test_curve_slice_stands_when_the_model_names_path_out_to_in(
        self, tmp_path, caplog
    ) -> None:
        caplog.set_level("INFO")
        coach = await coached(tmp_path, a_path_reply(a_slice_grid()))

        completed = coach.bus.of(AnalysisCompleted)
        assert len(completed) == 1
        advice = completed[0].advice
        assert advice.cue is CueId.CURVE_SLICE
        assert advice.focus is CoachingFocus.CURVE
        assert advice.display == SLICE.display
        assert advice.observation == SLICE.observed
        assert coach.results[0].cue_id == "CURVE_SLICE"
        assert not any(
            event.advice.cue is CueId.PATH_OUT_TO_IN
            for event in coach.bus.of(AnalysisCompleted)
        )
        # And the words go with the cue. They could have been written for a
        # slice, which is why they used to be kept, but "Feel the club come
        # more from the inside." is a path lesson and the heading above it
        # now says RELEASE SOONER. One of the two would have to be wrong, so
        # the cue that won says the sentence reviewed beside it.
        assert advice.source is AdviceSource.TEMPLATE
        assert advice.speech == cue_speech(CueId.CURVE_SLICE)
        assert advice.speech != FROM_INSIDE
        # Still "ok": the reply was used. Its metrics and its reading of the
        # swing are in the score and its evidence picked the frame. Only the
        # sentence was replaced.
        assert coach.results[0].status == "ok"

        record = messages(caplog, "coaching_cue_reconciled")[0]
        assert record.coaching_model_cue == "PATH_OUT_TO_IN"
        assert record.coaching_cue == "CURVE_SLICE"
        assert record.coaching_focus == "curve"
        assert record.coaching_reason == "reviewed_words"
        assert not messages(caplog, "speech_suppressed")

    async def test_words_contradicting_the_cue_are_not_relabelled(
        self, tmp_path, caplog
    ) -> None:
        caplog.set_level("INFO")
        coach = await coached(
            tmp_path,
            a_path_reply(a_slice_grid(), observation="The ball curved with hook spin."),
        )

        advice = coach.bus.of(AnalysisCompleted)[0].advice
        assert advice.cue is CueId.CURVE_SLICE
        assert advice.source is AdviceSource.TEMPLATE
        assert "hook" not in advice.speech.lower()
        assert advice.speech == cue_speech(CueId.CURVE_SLICE)
        record = messages(caplog, "coaching_cue_reconciled")[0]
        assert record.coaching_reason == "reviewed_words"
        assert "PATH_OUT_TO_IN was not admissible" in record.coaching_detail

    async def test_validation_itself_still_refuses_the_inadmissible_cue(
        self, tmp_path
    ) -> None:
        coach = await coached(tmp_path, a_path_reply(a_slice_grid()))

        assert "PATH_OUT_TO_IN was not admissible" in (coach.results[0].detail or "")


class TestNoBareTemplate:
    @pytest.mark.parametrize(
        "reply",
        [
            a_path_reply(a_slice_grid(), "Let the toe pass sooner.", WEIGHT),
            a_reply(a_slice_grid(), kind="none", cue=None, speech=None),
        ],
        ids=["inadmissible_cue_two_instructions", "model_declined"],
    )
    async def test_a_refused_reply_gives_the_cues_whole_reviewed_lesson(
        self, tmp_path, reply: dict[str, Any]
    ) -> None:
        coach = await coached(tmp_path, reply)

        completed = coach.bus.of(AnalysisCompleted)
        assert len(completed) == 1
        advice = completed[0].advice
        assert advice.cue is CueId.CURVE_SLICE
        assert advice.source is AdviceSource.TEMPLATE
        assert advice.display == "RELEASE SOONER"
        assert advice.observation == "SLICE SPIN"
        assert advice.spoken_observation == SLICE.explanation
        assert advice.speech == cue_speech(CueId.CURVE_SLICE)
        assert _speech({"speech": advice.speech})[1] == 1
        assert completed[0].evidence is not None
        # The model cited the metrics grid. The player gets the impact still
        # instead: the grid is five thousand pixels of tiny text and was
        # never something to put on a floor.
        assert completed[0].evidence.frame_id == "impact_01"
        assert advice.swing_score is not None
        assert not coach.bus.of(ShotDiagnosed)

    async def test_the_reviewed_lesson_is_queued_and_george_says_it(
        self, tmp_path, caplog
    ) -> None:
        caplog.set_level("INFO")
        coach = await coached(
            tmp_path, a_path_reply(a_slice_grid(), "Let the toe pass sooner.", WEIGHT)
        )

        said = await spoken(coach.bus.of(AnalysisCompleted))

        assert said == [cue_speech(CueId.CURVE_SLICE)]
        assert not messages(caplog, "speech_suppressed")
        # No fallback is needed any more. The reply held two instructions and
        # an inadmissible cue, and neither is a reason to drop to a bare
        # template: the reconciliation itself now supplies the reviewed line.
        assert not messages(caplog, "coaching_advice_fallback")
        reconciled = messages(caplog, "coaching_cue_reconciled")[0]
        assert reconciled.coaching_cue == "CURVE_SLICE"
        assert reconciled.coaching_model_cue == "PATH_OUT_TO_IN"
        ready = messages(caplog, "coaching_advice_ready")[0]
        assert ready.coaching_observed == "SLICE SPIN"

    async def test_invalid_model_speech_is_never_spoken(self, tmp_path) -> None:
        coach = await coached(
            tmp_path, a_path_reply(a_slice_grid(), "Let the toe pass sooner.", WEIGHT)
        )

        said = await spoken(coach.bus.of(AnalysisCompleted))

        assert said
        assert not any("weight" in line.lower() for line in said)
        assert not any("Let the toe pass sooner." in line for line in said)

    async def test_scatter_stands_when_the_model_names_path_out_to_in(
        self, tmp_path, caplog
    ) -> None:
        caplog.set_level("INFO")
        coach = await scattered(tmp_path, a_path_reply(a_level_grid(240.0)))

        assert coach.results[-1].cue_id == "SCATTER"
        advice = coach.bus.of(AnalysisCompleted)[-1].advice
        assert advice.cue is CueId.SCATTER
        assert advice.focus is CoachingFocus.CONSISTENCY
        assert advice.display == "REPEAT YOUR TEMPO"
        assert advice.observation == SCATTER.observed
        assert advice.spoken_observation == SCATTER.explanation
        # "From the inside" is a path correction, not a consistency one.
        assert advice.speech == cue_speech(CueId.SCATTER)
        said = await spoken([coach.bus.of(AnalysisCompleted)[-1]])
        assert said == [cue_speech(CueId.SCATTER)]
        assert "inside" not in said[0]
        record = messages(caplog, "coaching_cue_reconciled")[-1]
        assert record.coaching_model_cue == "PATH_OUT_TO_IN"
        assert record.coaching_cue == "SCATTER"
        assert record.coaching_reason == "reviewed_words"


class HeldReply(ScriptedProvider):
    """Holds its answer until released, so a newer strike can land first."""

    def __init__(self, reply: dict[str, Any]) -> None:
        super().__init__(replies=(reply,))
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request: AdviceRequest) -> ProviderReply:
        self.started.set()
        await self.release.wait()
        return await super().complete(request)


class TestTheReviewedLessonIsStillPerishable:
    async def test_a_newer_strike_supersedes_the_reconciled_line(
        self, tmp_path, caplog
    ) -> None:
        caplog.set_level("INFO")
        provider = HeldReply(
            a_path_reply(a_slice_grid(), "Let the toe pass sooner.", WEIGHT)
        )
        coach = Coach(provider, timeout_seconds=5.0)
        await coach.service.open()
        await coach.service.on_club_selected(ClubSelected(club=DRIVER))
        old = ShotDetected(trigger_id="16")
        await coach.service.on_shot_detected(old)
        capture = a_capture_on_disk(tmp_path, strike_id=old.event_id, trigger_id="16")
        task = asyncio.create_task(
            coach.service.on_swing_frames(SwingFramesReady(capture=capture))
        )
        await asyncio.wait_for(provider.started.wait(), 2.0)

        await coach.service.on_shot_detected(ShotDetected(trigger_id="17"))
        provider.release.set()
        await task
        await coach.service.aclose()

        assert not [
            event
            for event in coach.bus.of(AnalysisCompleted)
            if event.shot_id == old.event_id
        ]
        assert any(
            record.coaching_reason == "superseded"
            for record in messages(caplog, "speech_suppressed")
        )

    async def test_george_drops_an_unsaid_reviewed_line_for_the_next_strike(
        self, tmp_path
    ) -> None:
        coach = await coached(
            tmp_path, a_path_reply(a_slice_grid(), "Let the toe pass sooner.", WEIGHT)
        )
        line = coach.bus.of(AnalysisCompleted)[0]
        gate = asyncio.Event()

        async with Table(sink=RecordingSink(gate=gate)) as table:
            await table.say("Playing now.", perishable=False)
            for _ in range(20):
                await asyncio.sleep(0)
            strike = ShotDetected(trigger_id="16")
            await table.service.on_shot_detected(strike)
            await table.service.on_analysis_completed(
                replace(line, shot_id=strike.event_id)
            )
            await table.service.on_shot_detected(ShotDetected(trigger_id="17"))
            gate.set()
            await table.settled()

        said = [text for text, _ in table.engine.calls]
        assert cue_speech(CueId.CURVE_SLICE) not in said
        assert table.service.health().superseded == 1

    async def test_the_reviewed_line_is_shown_until_the_next_ball(
        self, tmp_path
    ) -> None:
        coach = await coached(
            tmp_path, a_path_reply(a_slice_grid(), "Let the toe pass sooner.", WEIGHT)
        )
        line = coach.bus.of(AnalysisCompleted)[0]

        floor = FloorSpy()
        subject = flow(floor)
        strike = ShotDetected(trigger_id="16")
        await subject.on_shot_detected(strike)
        advice = replace(line.advice, shot_id=strike.event_id)
        await subject.on_advice_ready(
            AnalysisCompleted(shot_id=strike.event_id, advice=advice)
        )

        assert floor.showing_advice
        assert floor.advice[-1][0] == "RELEASE SOONER"

        await subject.on_ball_detected(BallDetected())

        assert not floor.showing_advice
