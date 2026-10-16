"""Three things the first hardware session shipped that it should not have.

Small, independent and unrelated to each other. They are together because they
share a provenance: each one was visible in the four stored shots of the
2026-10-05 session, and none of them was a disagreement about coaching -- they
are a measurement thrown away, a sentence contradicting its own heading, and a
word that is wrong for half the players who could hear it.
"""

from __future__ import annotations

import pytest

from caddiej.coaching.score import score_swing_v2 as score_swing
from caddiej.coaching.validate import SpeechRejected, _speech
from caddiej.coaching.vx import coach_payload
from caddiej.domain.coaching import AdviceKind
from caddiej.domain.shot import ClubKind, Handedness
from caddiej.domain.vx import MetricDirection, MetricUnit, VxMetric, VxMetrics

# --- 1: a strike that was measured and then not scored ----------------------


def a_grid(
    width: float, direction: str | None, **extra: tuple[float, str, str | None]
) -> VxMetrics:
    """Shot 2 of the session, with its strike reading under our control.

    The rest has to be here for the score to exist at all: a single component
    is below the minimum weight CaddieJ is willing to grade a shot from, and a
    test built on one reading compares None with None and passes for no
    reason.
    """

    readings: dict[str, tuple[float, str, str | None]] = {
        "club_speed": (103.0, "mph", None),
        "ball_speed": (142.0, "mph", None),
        "face_angle": (1.5, "deg", "closed"),
        "swing_path": (1.5, "deg", "out_to_in"),
        "impact_width": (width, "mm", direction),
    }
    readings.update(extra)
    return metrics(**readings)


def metrics(**readings: tuple[float, str, str | None]) -> VxMetrics:
    return VxMetrics(
        values={
            name: VxMetric(
                value=value,
                unit=MetricUnit(unit),
                direction=None if direction is None else MetricDirection(direction),
            )
            for name, (value, unit, direction) in readings.items()
        }
    )


class TestAStrikeIsScoredFromHowFarOffCentreItWas:
    def test_a_miss_with_no_toe_or_heel_word_is_still_a_miss(self) -> None:
        """What shot 2 of the session scored 98 on.

        ProTee measured the strike 14.5 mm off centre and printed the number
        without the Toe or Heel qualifier. `signed` answered None, because
        which side of the face a bare sign means is not something to guess, and
        the strike component went unscored -- so the shot was graded as though
        the ball had been struck in the middle of the face.

        The ramp only ever reads the magnitude. The qualifier was needed for
        nothing here, and is still required where it is actually used.

        Scored through v2, which is a delivery-only rubric: this class is
        about the strike ramp, and under v3 a grid with no swing behind it
        has no Swing Score at all.
        """

        qualified = score_swing(
            a_grid(14.5, "toe"), club=ClubKind.IRON, handedness=Handedness.RIGHT
        )
        bare = score_swing(
            a_grid(14.5, None), club=ClubKind.IRON, handedness=Handedness.RIGHT
        )
        centred = score_swing(
            a_grid(0.5, None), club=ClubKind.IRON, handedness=Handedness.RIGHT
        )

        assert qualified.value is not None
        assert bare.value == qualified.value
        # And it is a real penalty rather than two equal nulls, which is what
        # this assertion compared before the strike was scored at all.
        assert bare.value < centred.value

    def test_a_centred_strike_still_scores_as_one(self) -> None:
        centred = score_swing(
            a_grid(1.0, None), club=ClubKind.IRON, handedness=Handedness.RIGHT
        )
        missed = score_swing(
            a_grid(20.0, None), club=ClubKind.IRON, handedness=Handedness.RIGHT
        )

        assert centred.value is not None and missed.value is not None
        assert centred.value > missed.value

    def test_the_sign_of_a_bare_reading_is_not_read_as_a_better_strike(self) -> None:
        """A negative bare reading is a miss of the same size, not a bonus."""

        negative = score_swing(
            a_grid(-14.5, None), club=ClubKind.IRON, handedness=Handedness.RIGHT
        )
        positive = score_swing(
            a_grid(14.5, None), club=ClubKind.IRON, handedness=Handedness.RIGHT
        )

        assert negative.value is not None
        assert negative.value == positive.value


# --- 2: a reinforcement that asked for a change -----------------------------


class TestAReinforcementDoesNotAlsoCorrect:
    def test_it_does_not_borrow_the_instruction_when_it_has_no_praise(self) -> None:
        """What shot 2 said under the heading GOOD FACE.

        "Your clubface pointed right at impact. Turn the trail forearm over
        through impact." Told it was good, and told to fix it, on the best
        strike of the four.
        """

        joined = coach_payload(
            {
                "coach": {
                    "kind": AdviceKind.REINFORCEMENT.value,
                    "observation": "Your clubface was square at impact.",
                    "instruction": "Turn the trail forearm over through impact.",
                    "reinforcement": None,
                }
            }
        )
        assert joined is not None

        assert joined["speech"] == "Your clubface was square at impact."

    def test_praise_is_said_when_there_is_some(self) -> None:
        joined = coach_payload(
            {
                "coach": {
                    "kind": AdviceKind.REINFORCEMENT.value,
                    "observation": "Your clubface was square at impact.",
                    "instruction": "Turn the trail forearm over through impact.",
                    "reinforcement": "Keep that release.",
                }
            }
        )
        assert joined is not None

        assert joined["speech"] == (
            "Your clubface was square at impact. Keep that release."
        )

    def test_a_correction_still_carries_its_instruction(self) -> None:
        joined = coach_payload(
            {
                "coach": {
                    "kind": AdviceKind.CORRECTION.value,
                    "observation": "Your clubface was open at impact.",
                    "instruction": "Turn the trail forearm over through impact.",
                    "reinforcement": None,
                }
            }
        )
        assert joined is not None

        assert joined["speech"] == (
            "Your clubface was open at impact. "
            "Turn the trail forearm over through impact."
        )


# --- 3: a word that is only true for one player -----------------------------


class TestSpeechNeverNamesASideOfTheBody:
    @pytest.mark.parametrize(
        "text",
        [
            "Your clubface pointed right at impact. Turn the trail forearm over.",
            "Shift your weight onto your left foot a little earlier.",
            "Let the Right shoulder work downwards instead.",
        ],
    )
    def test_a_handed_word_is_refused(self, text: str) -> None:
        """The sentence shot 2 actually said is the first of these.

        The face was one and a half degrees *closed*. "Right" was wrong for a
        right-hander and wrong the other way for a left-hander, from one word.
        Nothing in the validator knows which way the face was pointing, so the
        word is refused rather than inspected, and the cue's own reviewed line
        -- handedness-neutral by construction -- is said instead.
        """

        with pytest.raises(SpeechRejected, match="instead of lead or trail"):
            _speech({"speech": text})

    def test_lead_and_trail_are_the_words_that_work(self) -> None:
        text, _words = _speech(
            {"speech": "Let your trail shoulder work downwards towards the ball."}
        )

        assert "trail" in text

    @pytest.mark.parametrize(
        "text",
        [
            "Keep the shaft a little less upright through the ball.",
            "Turn through the ball rightly and hold the finish.",
        ],
    )
    def test_a_word_that_merely_contains_one_is_not_a_side(self, text: str) -> None:
        """ "Upright" is a shaft lie and "rightly" is an adverb.

        Which is why this is a check on whole words rather than a substring
        scan: `"right" in "upright"` is true and means nothing.
        """

        _speech({"speech": text})
