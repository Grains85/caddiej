"""What the first real hardware test of Coach V3 found, and must never find again.

Coach V3 passed its whole gate and then failed on the mat, which is the only
place that counts. Three shots were enough to find one silent, serious bug and
a registry full of advice that was not advice. This file is the record of both,
written as tests so that the next person to change `cues.py`, `validate.py` or
`service.py` has to keep the promises the hardware extracted.

The bug worth understanding before reading further. A shot measured nothing
worth correcting -- every band clear -- and the model, looking at the stills,
rated the trail shoulder a fault. The promotion machinery worked: the seen cause
outranked the measured reinforcement and `OVER_THE_TOP` became the cue. Then
reconciliation substituted that cue into a payload the model had written for a
*measured* cue, which naturally carried no `visual_confidence`, and validation
refused it -- correctly, because a visual lesson is judged on nothing else. The
fallback then built its line from a diagnosis in which only `cue` had been
replaced, so the cue said `OVER_THE_TOP` and the display still said
**GOOD PATH**. CaddieJ congratulated a player on the exact swing it had just
decided to correct them about, and said nothing out loud at all.

Two defects, one root: a number that existed was not carried, and an object was
left able to contradict itself. Both are fixed at the source rather than at the
place the contradiction happened to surface, and the tests below are written
against the behaviour a player sees rather than the internals, so that a future
rewrite of the plumbing cannot quietly reintroduce either.
"""

from __future__ import annotations

from typing import Any

import pytest
from tests.unit.test_coach_protee import (
    Coach,
    a_checkpoint,
    a_reading,
    a_swing,
    an_open_face_grid,
    every_metric_null,
)
from tests.unit.test_play_flow import Announcements, FloorSpy, an_advice, flow

from caddiej.coaching.cues import (
    CUE_SPEECH,
    CUES_BY_ID,
    cue_speech,
)
from caddiej.coaching.fakes import ScriptedProvider
from caddiej.coaching.prompt import MAX_DISPLAY_CHARACTERS
from caddiej.coaching.templates import REINFORCEMENT_DISPLAYS
from caddiej.domain.coaching import AdviceKind, ConfidenceSource, SeveritySource
from caddiej.domain.cues import CueId, EvidenceGrade
from caddiej.domain.events import (
    AnalysisCompleted,
    BallCleared,
    BallDetected,
    LaunchMonitorReadinessChanged,
    ShotDetected,
)
from caddiej.domain.swing_checkpoints import CheckpointId

# The shot the hardware actually hit, as its All Values grid read it. Every one
# of the five scored deliveries is inside its band, which is what made the
# promotion matter: there was no measured fault for the lesson to be about.
THE_HARDWARE_SHOT: dict[str, Any] = {
    **every_metric_null(),
    "ball_speed": a_reading(148.9, "mph"),
    "club_speed": a_reading(99.9, "mph"),
    "smash_factor": a_reading(1.49, "ratio"),
    "carry": a_reading(152.1, "yd"),
    "face_angle": a_reading(0.7, "deg", "open"),
    "swing_path": a_reading(1.7, "deg", "out_to_in"),
    "face_to_path": a_reading(2.4, "deg", "open"),
    "impact_width": a_reading(8.75, "mm", "toe"),
}

# Wording that is refused for a path cue because it is about weight, so that a
# test can fail the model's *words* while leaving its cue perfectly sound.
OFF_FOCUS = "Shift more weight onto your lead side as you come down."

GOOD_DISPLAYS = frozenset(REINFORCEMENT_DISPLAYS.values())


ROOT = CheckpointId.TRANSITION_TRAIL_SHOULDER
"""The checkpoint that carries the hardware shot's fault under rubric v4.

The trail shoulder moving out towards the ball, read from a down-the-line
picture. Rubric v2 called the same observation `swing_plane`; the checkpoint
names the body part instead of the geometry, which is the half the player can
move and the half the cue registry has always said -- DIP TRAIL SHOULDER."""


def a_rated_reply(
    *,
    cue: str,
    instruction: str = "Let the club come more from the inside.",
    root: str | None = "fault",
    confidence: float = 0.6,
    visual_confidence: float | None = None,
    metrics: dict[str, Any] | None = None,
    **checkpoints: Any,
) -> dict[str, Any]:
    """A reply that assesses the swing and then coaches whatever it is told to.

    `cue` is deliberately free: the hardware failure needed a reply whose cue
    was *measured* while its rating was a fault, and that combination is the
    one no earlier test had. `visual_confidence` stays None by default for the
    same reason -- a model choosing a measured cue has no reason to send one,
    and the bug was CaddieJ assuming otherwise.
    """

    assessment = a_swing()
    assessment[ROOT.value] = (
        None
        if root is None
        else a_checkpoint(
            ROOT,
            35 if root == "fault" else 85,
            confidence=confidence,
            deviation="out" if root == "fault" else None,
        )
    )
    assessment.update(checkpoints)
    return {
        "metrics": THE_HARDWARE_SHOT if metrics is None else metrics,
        "checkpoints": assessment,
        "coach": {
            "kind": "correction",
            "cue": cue,
            "instruction": instruction,
            "observation": None,
            "reinforcement": None,
            "visual_confidence": visual_confidence,
        },
    }


async def coached(tmp_path, reply: dict[str, Any]) -> Coach:
    coach = Coach(ScriptedProvider(replies=(reply,)))
    await coach.run(tmp_path, supports=True)
    return coach


def the_advice(coach: Coach):
    completed = coach.bus.of(AnalysisCompleted)
    assert len(completed) == 1, "exactly one line per shot"
    return completed[0].advice


# --- 1: a seen fault can never come out as praise ---------------------------


class TestAPromotedFaultIsNeverPraise:
    """The hardware bug, from the player's side.

    Stated as a property rather than as the one path that failed, because the
    contradiction lived in the diagnosis and every path that read it was
    capable of printing it. What the player must never see is the pairing: a
    corrective cue beside a display that says the shot was good.
    """

    async def test_a_measured_cue_with_a_seen_fault_is_coached_as_the_fault(
        self, tmp_path
    ) -> None:
        coach = await coached(tmp_path, a_rated_reply(cue=CueId.PATH_OUT_TO_IN.value))

        advice = the_advice(coach)
        assert advice.cue is CueId.TRAIL_SHOULDER_OUT
        assert advice.kind is AdviceKind.CORRECTION
        assert advice.display == "DIP TRAIL SHOULDER"
        assert advice.observation == "UPPER BODY OUT"

    async def test_and_the_floor_never_says_good_path(self, tmp_path) -> None:
        coach = await coached(tmp_path, a_rated_reply(cue=CueId.PATH_OUT_TO_IN.value))

        assert the_advice(coach).display not in GOOD_DISPLAYS

    async def test_nor_when_the_words_are_refused_as_well(self, tmp_path) -> None:
        # The exact hardware path: cue substituted, wording unusable, fallback.
        coach = await coached(
            tmp_path,
            a_rated_reply(cue=CueId.PATH_OUT_TO_IN.value, instruction=OFF_FOCUS),
        )

        advice = the_advice(coach)
        assert advice.display not in GOOD_DISPLAYS
        assert advice.cue is CueId.TRAIL_SHOULDER_OUT

    async def test_nor_when_the_model_declines_to_coach_at_all(self, tmp_path) -> None:
        # A rating is a fault and the model offers nothing. CaddieJ's own
        # lesson is the promoted one, not the reinforcement it displaced.
        reply = a_rated_reply(cue=CueId.PATH_OUT_TO_IN.value)
        reply["coach"] = {
            "kind": "none",
            "cue": None,
            "instruction": None,
            "observation": None,
            "reinforcement": None,
            "visual_confidence": None,
        }
        coach = await coached(tmp_path, reply)

        advice = the_advice(coach)
        assert advice.cue is CueId.TRAIL_SHOULDER_OUT
        assert advice.display not in GOOD_DISPLAYS

    async def test_a_corrective_cue_and_a_good_display_never_pair_up(
        self, tmp_path
    ) -> None:
        # The property itself, over every line this file's replies produce.
        for cue in (CueId.PATH_OUT_TO_IN, CueId.FACE_OPEN, CueId.STRIKE_TOE):
            coach = await coached(tmp_path, a_rated_reply(cue=cue.value))
            advice = the_advice(coach)
            if advice.kind is AdviceKind.CORRECTION:
                assert advice.display not in GOOD_DISPLAYS


# --- 2, 3: the reviewed lesson, and George saying it ------------------------


class TestRefusedWordingStillCoaches:
    """A sound cue with unusable words is CaddieJ's lesson, spoken.

    This was already the intent -- `_protee_fallback` exists to say the cue's
    reviewed sentence -- and it silently did not work for a promoted visual
    cue, because `from_cue` ran the same confidence check and returned None.
    Falling back to "no reviewed cue line" after the model had answered
    perfectly well about the cue is the failure mode worth pinning.
    """

    async def test_the_reviewed_corrective_line_is_what_the_floor_shows(
        self, tmp_path
    ) -> None:
        coach = await coached(
            tmp_path,
            a_rated_reply(cue=CueId.PATH_OUT_TO_IN.value, instruction=OFF_FOCUS),
        )

        advice = the_advice(coach)
        assert advice.display == "DIP TRAIL SHOULDER"
        assert advice.kind is AdviceKind.CORRECTION

    async def test_and_george_says_the_cues_own_sentence(self, tmp_path) -> None:
        coach = await coached(
            tmp_path,
            a_rated_reply(cue=CueId.PATH_OUT_TO_IN.value, instruction=OFF_FOCUS),
        )

        assert the_advice(coach).speech == cue_speech(CueId.TRAIL_SHOULDER_OUT)

    async def test_rather_than_the_rejected_words(self, tmp_path) -> None:
        coach = await coached(
            tmp_path,
            a_rated_reply(cue=CueId.PATH_OUT_TO_IN.value, instruction=OFF_FOCUS),
        )

        assert the_advice(coach).speech != OFF_FOCUS
        assert "weight" not in (the_advice(coach).speech or "").lower()

    async def test_the_evidence_is_the_frame_the_rating_cited(self, tmp_path) -> None:
        # Not the All Values grid, which is where a measured cue would point,
        # and not a frame chosen for some other fault.
        coach = await coached(
            tmp_path,
            a_rated_reply(cue=CueId.PATH_OUT_TO_IN.value, instruction=OFF_FOCUS),
        )

        assert the_advice(coach).evidence_frame_id == "swing_behind_01"

    @pytest.mark.parametrize("cue", sorted(CUE_SPEECH, key=lambda cue: cue.value))
    def test_every_promotable_visual_cue_has_something_to_say(self, cue) -> None:
        # Six visual cues had no reviewed sentence at all, so a promotion to
        # any of them reached the floor in silence. A cue a picture can promote
        # has to be speakable, or the promotion is only half a lesson.
        spec = CUES_BY_ID[cue]
        if spec.grade is not EvidenceGrade.VISUAL:
            return
        assert cue_speech(cue)


# --- 4: the rating's confidence is the cue's confidence ---------------------


class TestTheRatingsConfidenceSurvives:
    """Why `visual_confidence` was missing, and why supplying it is not a relaxation.

    The number was never absent from the reply. The model sent it with the
    rating, in the same request, already quantized and ceilinged on the way in
    by `cap_visual_confidence`. It was absent only from the payload the cue was
    substituted into, because that payload was written about a different cue.
    Carrying it keeps every check that refused the shot in the first place.
    """

    async def test_the_promoted_line_is_as_sure_as_the_rating_was(
        self, tmp_path
    ) -> None:
        coach = await coached(
            tmp_path,
            a_rated_reply(cue=CueId.PATH_OUT_TO_IN.value, instruction=OFF_FOCUS),
        )

        advice = the_advice(coach)
        assert advice.confidence == pytest.approx(0.6)
        assert advice.confidence_source is ConfidenceSource.MODEL_CAPPED

    async def test_and_its_severity_is_coarse_because_nothing_measured_it(
        self, tmp_path
    ) -> None:
        coach = await coached(
            tmp_path,
            a_rated_reply(cue=CueId.PATH_OUT_TO_IN.value, instruction=OFF_FOCUS),
        )

        advice = the_advice(coach)
        assert advice.severity_source is SeveritySource.OBSERVED
        assert advice.severity in (0.25, 0.5, 0.75)

    async def test_a_ceilinged_rating_is_not_inflated_by_the_promotion(
        self, tmp_path
    ) -> None:
        # The model said it was certain. The ceiling says a picture is not.
        coach = await coached(
            tmp_path,
            a_rated_reply(
                cue=CueId.PATH_OUT_TO_IN.value,
                instruction=OFF_FOCUS,
                confidence=1.0,
            ),
        )

        assert the_advice(coach).confidence <= 0.60


# --- 5: a face fault on a sound-looking swing stays about the face ----------


class TestAGoodLookingSwingLeavesTheFaceInCharge:
    """The player's own rule, and the limit on the one above it.

    A clearly open face with four good visual ratings is a face lesson. There
    is no credible seen cause to prefer, and inventing one because the ball
    finished badly would be exactly the thing the brief forbids.
    """

    async def test_an_open_face_with_a_clean_looking_swing_is_a_face_lesson(
        self, tmp_path
    ) -> None:
        coach = await coached(
            tmp_path,
            a_rated_reply(
                cue=CueId.FACE_OPEN.value,
                instruction="Let the toe pass through the ball a fraction sooner.",
                root="good",
                metrics=an_open_face_grid(),
            ),
        )

        advice = the_advice(coach)
        assert advice.cue is CueId.FACE_OPEN
        assert advice.observation == "FACE OPEN"

    async def test_and_the_action_is_physical_rather_than_a_restatement(
        self, tmp_path
    ) -> None:
        coach = await coached(
            tmp_path,
            a_rated_reply(
                cue=CueId.FACE_OPEN.value,
                instruction="Let the toe pass through the ball a fraction sooner.",
                root="good",
                metrics=an_open_face_grid(),
            ),
        )

        # "CLOSE THE FACE" was the display the hardware showed, and it is a
        # restatement of FACE OPEN rather than something to do about it.
        assert the_advice(coach).display == "PASS THE TOE EARLY"

    async def test_a_credible_seen_cause_outranks_even_a_badly_open_face(
        self, tmp_path
    ) -> None:
        # Swing Score V4 reverses shot three. ProTee measures what the club
        # did at the ball; a credible mechanical cause seen in the stills is
        # what produced it, and is the lesson. ProTee keeps the lesson only
        # when no credible cause was seen, which the two tests above pin.
        coach = await coached(
            tmp_path,
            a_rated_reply(
                cue=CueId.FACE_OPEN.value,
                instruction="Let the toe pass through the ball a fraction sooner.",
                metrics=an_open_face_grid(),
            ),
        )

        assert the_advice(coach).cue is CueId.TRAIL_SHOULDER_OUT


# --- 6: the registry is audited, cue by cue --------------------------------


# Every verb a corrective action is allowed to start with. A closed list on
# purpose: the brief asks for coaching, not for prose, and the way to keep a
# finite vocabulary finite is to make adding to it a deliberate edit.
ACTION_VERBS = frozenset(
    {
        "CATCH",
        "DROP",
        "EASE",
        "GIVE",
        "HIT",
        "DIP",
        "HOLD",
        "KEEP",
        "LOAD",
        "PASS",
        "QUIET",
        "RELEASE",
        "REPEAT",
        "ROCK",
        "SLOW",
        "SQUARE",
        "STAND",
        "STAY",
        "STEADY",
        "SWING",
        "TAKE",
        "TURN",
        # Added with the Swing Score V3 criterion cues. Each is an imperative
        # a player can act on standing over a ball: tilt from the hips, move
        # back as one, wind up further, start from the ground.
        "MOVE",
        "START",
        "TILT",
        "WIND",
        # Added with the Swing Score V4 fault library: let the arms hang or
        # fall, lift or lay the club, extend, and lead with the hands.
        "EXTEND",
        "LAY",
        "LEAD",
        "LET",
        "LIFT",
    }
)

# Words that carry no meaning of their own, so sharing one between the
# observation and the action is not a restatement.
FILLER = frozenset({"A", "AN", "IT", "THE", "YOUR"})

CORRECTIVE = tuple(
    sorted(
        (cue for cue, spec in CUES_BY_ID.items() if spec.kind is AdviceKind.CORRECTION),
        key=lambda cue: cue.value,
    )
)


class TestEveryCorrectiveCueGivesSomethingToDo:
    """What the player asked for, as a rule over the whole registry.

    "FACE OPEN / CLOSE THE FACE" tells somebody standing over a ball what the
    machine measured and then tells them to measure differently. Twelve cues
    were like that, and the pattern is mechanical enough to test: an action
    that only repeats the observation's words is not an action, and an action
    that does not start with a verb is not something to do.

    The rule is deliberately crude. It cannot tell good coaching from bad, and
    it is not trying to -- it catches the specific failure of writing the fault
    twice, which is the one that reached the mat.
    """

    @pytest.mark.parametrize("cue", CORRECTIVE)
    def test_it_names_what_was_wrong(self, cue) -> None:
        assert CUES_BY_ID[cue].observed

    @pytest.mark.parametrize("cue", CORRECTIVE)
    def test_and_one_thing_to_do_about_it(self, cue) -> None:
        display = CUES_BY_ID[cue].display
        assert display
        assert len(display) <= MAX_DISPLAY_CHARACTERS
        assert display.split()[0] in ACTION_VERBS

    @pytest.mark.parametrize("cue", CORRECTIVE)
    def test_and_the_two_are_not_the_same_sentence_twice(self, cue) -> None:
        spec = CUES_BY_ID[cue]
        observed = {word.strip("-") for word in spec.observed.split()} - FILLER
        action = {word.strip("-") for word in spec.display.split()} - FILLER

        assert not observed & action, f"{cue.value} restates its own fault"

    @pytest.mark.parametrize("cue", CORRECTIVE)
    def test_and_never_reads_as_praise(self, cue) -> None:
        assert CUES_BY_ID[cue].display not in GOOD_DISPLAYS


# --- 7 to 10, 20: how long a lesson lives ----------------------------------


def a_coached(strike_id, display: str = "DIP TRAIL SHOULDER") -> AnalysisCompleted:
    return AnalysisCompleted(
        shot_id=strike_id, advice=an_advice(strike_id, display=display)
    )


class TestTheLessonLastsUntilTheNextBall:
    """The V3 decision the hardware reversed, and the three ways it can go wrong.

    Coach V3 removed the display timeout -- which was right, a lesson should
    not expire on a clock -- and then held the line all the way to the next
    strike, which is too long. The floor has one other job between shots:
    telling the player how far it is and what to hit. Those cannot share the
    space, and the player is finished with the lesson by the time they have put
    another ball down.

    So the boundary is the next ball, and it has to be the *ball* rather than
    anything else the device says. ProTee spends the time between shots in
    `FINDING BALL`, publishing clears and readiness changes the whole while; a
    lesson that disappeared on any of those would be a lesson nobody ever got
    to read.
    """

    async def test_it_stays_while_there_is_no_new_ball(self) -> None:
        floor = FloorSpy()
        subject = flow(floor)
        strike = ShotDetected(trigger_id="16")
        await subject.on_shot_detected(strike)

        await subject.on_advice_ready(a_coached(strike.event_id))

        assert floor.showing_advice
        assert floor.advice[-1][0] == "DIP TRAIL SHOULDER"
        await subject.aclose()

    async def test_a_ball_positively_found_ends_it(self) -> None:
        floor = FloorSpy()
        subject = flow(floor)
        strike = ShotDetected(trigger_id="16")
        await subject.on_shot_detected(strike)
        await subject.on_advice_ready(a_coached(strike.event_id))

        await subject.on_ball_detected(BallDetected())

        assert not floor.showing_advice
        await subject.aclose()

    async def test_the_device_hunting_for_one_does_not(self) -> None:
        floor = FloorSpy()
        subject = flow(floor)
        strike = ShotDetected(trigger_id="16")
        await subject.on_shot_detected(strike)
        await subject.on_advice_ready(a_coached(strike.event_id))

        for _ in range(3):
            await subject.on_ball_cleared(BallCleared())
            await subject.on_readiness_changed(
                LaunchMonitorReadinessChanged(ready=False)
            )
            await subject.on_readiness_changed(
                LaunchMonitorReadinessChanged(ready=True)
            )

        assert floor.showing_advice
        await subject.aclose()

    async def test_nor_does_the_same_ball_being_reported_again(self) -> None:
        # ProTee writes a relative ball position repeatedly while the ball sits
        # there. Only the transition is a placement.
        floor = FloorSpy()
        subject = flow(floor)
        strike = ShotDetected(trigger_id="16")
        await subject.on_shot_detected(strike)
        await subject.on_ball_detected(BallDetected())
        await subject.on_advice_ready(a_coached(strike.event_id))
        assert not floor.showing_advice

        floor.showing_advice = True
        for _ in range(3):
            await subject.on_ball_detected(BallDetected())

        assert floor.showing_advice
        await subject.aclose()

    async def test_a_late_answer_about_the_last_ball_never_comes_back(self) -> None:
        # The one that needs the retired-id set rather than just a clear. The
        # coach is still mid-request when the next ball goes down, and
        # `_strike_id` still names the previous strike, so without this the
        # answer would be judged current and land on top of the address
        # information for a ball it knows nothing about.
        floor = FloorSpy()
        subject = flow(floor)
        strike = ShotDetected(trigger_id="16")
        await subject.on_shot_detected(strike)
        await subject.on_advice_ready(a_coached(strike.event_id))
        await subject.on_ball_detected(BallDetected())
        assert not floor.showing_advice

        await subject.on_advice_ready(a_coached(strike.event_id))

        assert not floor.showing_advice
        await subject.aclose()

    async def test_and_the_next_strikes_own_answer_is_shown_normally(self) -> None:
        # The retirement must not outlive the ball it was about, or the next
        # shot would be coached into a floor that refuses to show it.
        floor = FloorSpy()
        subject = flow(floor, announcements=Announcements())
        first = ShotDetected(trigger_id="16")
        await subject.on_shot_detected(first)
        await subject.on_advice_ready(a_coached(first.event_id))
        await subject.on_ball_detected(BallDetected())

        second = ShotDetected(trigger_id="17")
        await subject.on_shot_detected(second)
        await subject.on_advice_ready(
            a_coached(second.event_id, display="STAND CLOSER")
        )

        assert floor.showing_advice
        assert floor.advice[-1][0] == "STAND CLOSER"
        await subject.aclose()

    async def test_newest_shot_still_wins_over_an_older_one(self) -> None:
        floor = FloorSpy()
        subject = flow(floor, announcements=Announcements())
        first = ShotDetected(trigger_id="16")
        await subject.on_shot_detected(first)
        second = ShotDetected(trigger_id="17")
        await subject.on_shot_detected(second)

        await subject.on_advice_ready(a_coached(first.event_id, display="HIT DOWN"))

        assert not floor.showing_advice
        await subject.aclose()

    async def test_the_first_ball_of_the_evening_clears_nothing(self) -> None:
        # Nothing has been struck, so there is no lesson to retire and no id
        # to remember. A placement before the first swing is just a placement.
        floor = FloorSpy()
        subject = flow(floor)

        await subject.on_ball_detected(BallDetected())

        strike = ShotDetected(trigger_id="16")
        await subject.on_shot_detected(strike)
        await subject.on_advice_ready(a_coached(strike.event_id))

        assert floor.showing_advice
        await subject.aclose()
